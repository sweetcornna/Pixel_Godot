"""spell_bundle 的 seed 人审断点、续跑、成本与逐资产导出闭环。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from pixel_asset_forge.cli import EXIT_OK, app
from pixel_asset_forge.config import Config
from pixel_asset_forge.errors import ProcessingError
from pixel_asset_forge.models import AssetManifest, load_pack
from pixel_asset_forge.models.job import JobKind, JobStatus
from pixel_asset_forge.pipelines import approve_seed, asset_pack
from pixel_asset_forge.pipelines.asset_pack import (
    AWAITING_APPROVAL_MESSAGE,
    run_asset_pack,
)
from pixel_asset_forge.pipelines.static_asset import create_static_asset
from pixel_asset_forge.planning import plan_pack
from pixel_asset_forge.providers import MockImageProvider
from pixel_asset_forge.storage import ArtifactStore

runner = CliRunner()


def _write_config(path: Path, *, max_concurrency: int = 2) -> Path:
    path.write_text(
        "provider: mock\n"
        "model: mock-image\n"
        "output_dir: outputs\n"
        "cache_dir: cache\n"
        f"max_concurrency: {max_concurrency}\n",
        encoding="utf-8",
    )
    return path


def _config(tmp_path: Path, *, max_concurrency: int = 2) -> Config:
    return Config(
        provider="mock",
        model="mock-image",
        output_dir=tmp_path / "outputs",
        cache_dir=tmp_path / "cache",
        max_concurrency=max_concurrency,
    )


def _save_plan(pack_path: Path, config: Config) -> None:
    result = plan_pack(
        load_pack(pack_path),
        provider=config.provider,
        model=config.model,
    )
    for asset in result.assets:
        ArtifactStore.for_asset(
            config.output_dir, asset.request.asset_id
        ).ensure().save_job_table(asset.jobs)


def test_spell_bundle_cli_runs_seed_approval_resume_and_export_each_asset(
    examples_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """退出门槛 1 + 2：完整 CLI 链路，并核对 plan 的拆分成本。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PIXEL_ASSET_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    async def inline_to_thread(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)
    config_path = _write_config(tmp_path / "mock.yaml")
    pack_path = examples_dir / "spell_bundle.yaml"
    pack = load_pack(pack_path)
    asset_ids = [asset.asset_id for asset in pack.assets]

    planned = runner.invoke(
        app,
        ["plan", str(pack_path), "--config", str(config_path), "--save"],
    )
    assert planned.exit_code == EXIT_OK
    assert "预计 seed API 调用 3" in planned.stdout
    assert "预计动画 API 调用 12" in planned.stdout

    first = runner.invoke(
        app,
        ["create-asset-pack", str(pack_path), "--config", str(config_path), "--json"],
    )
    assert first.exit_code == EXIT_OK
    first_payload = json.loads(first.stdout)
    assert first_payload["counts"]["awaiting_approval"] == 3
    assert first_payload["counts"]["provider_failed"] == 0
    assert first_payload["counts"]["processing_failed"] == 0
    assert all(
        asset["error"] == AWAITING_APPROVAL_MESSAGE
        for asset in first_payload["assets"]
    )

    for asset_id in asset_ids:
        store = ArtifactStore.for_asset(tmp_path / "outputs", asset_id)
        manifest = AssetManifest.load(store.manifest_path)
        table = store.load_job_table()
        assert manifest.asset_type == "spell"
        assert manifest.status == "awaiting_approval"
        assert manifest.palette.colors == list(pack.shared.palette.colors)
        assert (store.previews / "contact-sheet.png").exists()
        assert table is not None
        assert table.seed_job is not None
        assert table.seed_job.status is JobStatus.AWAITING_APPROVAL
        assert all(
            job.status is JobStatus.PLANNED
            for job in table.of_kind(JobKind.ANIMATION)
        )
        generation_log = json.loads(
            store.generation_log_path.read_text(encoding="utf-8")
        )
        assert len(generation_log) == 1

        approved = runner.invoke(
            app,
            [
                "create-animation",
                "--asset",
                asset_id,
                "--action",
                "cast",
                "--direction",
                "down",
                "--approve-seed",
                "--config",
                str(config_path),
            ],
        )
        assert approved.exit_code == EXIT_OK
        assert "seed 已批准" in approved.stdout

    second = runner.invoke(
        app,
        ["create-asset-pack", str(pack_path), "--config", str(config_path), "--json"],
    )
    assert second.exit_code == EXIT_OK
    second_payload = json.loads(second.stdout)
    assert second_payload["counts"]["exported"] == 3
    assert second_payload["counts"]["awaiting_approval"] == 0
    assert second_payload["counts"]["provider_failed"] == 0
    assert second_payload["counts"]["processing_failed"] == 0

    for asset_id in asset_ids:
        store = ArtifactStore.for_asset(tmp_path / "outputs", asset_id)
        manifest = AssetManifest.load(store.manifest_path)
        table = store.load_job_table()
        assert manifest.status == "exported"
        assert set(manifest.animations) == {
            "cast_down",
            "cast_left",
            "cast_right",
            "cast_up",
        }
        assert table is not None
        assert table.seed_job is not None
        assert table.seed_job.status is JobStatus.APPROVED
        assert all(
            job.status is JobStatus.EXPORTED
            for job in table.of_kind(JobKind.ANIMATION)
        )
        generation_log = json.loads(
            store.generation_log_path.read_text(encoding="utf-8")
        )
        assert len(generation_log) == 5

        exported = runner.invoke(
            app,
            [
                "export",
                asset_id,
                "--config",
                str(config_path),
                "--no-contact-sheet",
            ],
        )
        assert exported.exit_code == EXIT_OK
        assert (store.exports / "generic-json" / f"{asset_id}.json").exists()
        assert (store.exports / "godot" / f"{asset_id}_frames.tres").exists()


def test_unapproved_assets_are_not_failures_and_consume_no_animation_calls(
    examples_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """退出门槛 3：第二遍只推进已批准资产，等待中的资产零动画调用。"""
    data = yaml.safe_load(
        (examples_dir / "spell_bundle.yaml").read_text(encoding="utf-8")
    )
    data["pack_id"] = "partial_spell_approval"
    data["assets"] = data["assets"][:2]
    pack_path = tmp_path / "partial-spell-bundle.yaml"
    pack_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    config = _config(tmp_path)

    async def inline_to_thread(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)
    _save_plan(pack_path, config)

    first = asyncio.run(run_asset_pack(pack_path, config))
    assert first.counts["awaiting_approval"] == 2

    approved_id = data["assets"][0]["asset_id"]
    waiting_id = data["assets"][1]["asset_id"]
    approve_seed(config.asset_dir(approved_id))
    waiting_store = ArtifactStore.for_asset(config.output_dir, waiting_id)
    before = json.loads(waiting_store.generation_log_path.read_text(encoding="utf-8"))

    second = asyncio.run(run_asset_pack(pack_path, config))

    assert second.counts["exported"] == 1
    assert second.counts["awaiting_approval"] == 1
    assert second.counts["provider_failed"] == 0
    assert second.counts["processing_failed"] == 0
    waiting = next(asset for asset in second.assets if asset.asset_id == waiting_id)
    assert waiting.outcome == "awaiting_approval"
    assert waiting.error == AWAITING_APPROVAL_MESSAGE
    after = json.loads(waiting_store.generation_log_path.read_text(encoding="utf-8"))
    assert after == before
    assert len(after) == 1
    waiting_table = waiting_store.load_job_table()
    assert waiting_table is not None
    assert all(
        job.status is JobStatus.PLANNED
        for job in waiting_table.of_kind(JobKind.ANIMATION)
    )


def test_static_pipeline_still_rejects_expanded_spell_request(
    examples_dir: Path,
    tmp_path: Path,
) -> None:
    """退出门槛 4：spell 即使来自合法 bundle，也不能进入静态路径。"""
    request = load_pack(examples_dir / "spell_bundle.yaml").expand_requests()[0]
    request_path = tmp_path / "spell-request.yaml"
    request_path.write_text(
        yaml.safe_dump(
            request.model_dump(mode="json", exclude_none=True),
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ProcessingError,
        match=r"无 animations 的资产类型：environment_object, pickup, prop, ui_icon, weapon",
    ):
        create_static_asset(request_path, _config(tmp_path, max_concurrency=1))


class _BadSeedBytesProvider(MockImageProvider):
    def _generate(
        self, prompt: str, size: tuple[int, int], model: str
    ) -> tuple[bytes, str | None]:
        return b"provider-success-but-not-an-image", "req-bad-spell-seed"


def test_retry_failed_reclaims_failed_spell_seed(
    examples_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """7.1–7.3 继承项：动画 pack 的 failed 也只在显式重试时复位。"""
    data = yaml.safe_load(
        (examples_dir / "spell_bundle.yaml").read_text(encoding="utf-8")
    )
    data["pack_id"] = "retry_spell_seed"
    data["assets"] = data["assets"][:1]
    pack_path = tmp_path / "retry-spell-seed.yaml"
    pack_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    config = _config(tmp_path, max_concurrency=1)
    _save_plan(pack_path, config)
    monkeypatch.setattr(
        asset_pack,
        "get_provider",
        lambda *_args, **_kwargs: _BadSeedBytesProvider(config.model),
    )

    failed = asyncio.run(run_asset_pack(pack_path, config))
    assert failed.counts["provider_failed"] == 1
    assert failed.assets[0].request_id == "req-bad-spell-seed"
    store = ArtifactStore.for_asset(config.output_dir, data["assets"][0]["asset_id"])
    table = store.load_job_table()
    assert table is not None
    assert table.seed_job is not None
    assert table.seed_job.status is JobStatus.FAILED

    persisted = asyncio.run(run_asset_pack(pack_path, config))
    assert persisted.counts["processing_failed"] == 1
    assert persisted.assets[0].error_code == "persisted_failure"

    monkeypatch.setattr(
        asset_pack,
        "get_provider",
        lambda *_args, **_kwargs: MockImageProvider(config.model),
    )
    retried = asyncio.run(run_asset_pack(pack_path, config, retry_failed=True))

    assert retried.counts["awaiting_approval"] == 1
    assert retried.counts["provider_failed"] == 0
    assert retried.assets[0].resumed is True
    recovered = store.load_job_table()
    assert recovered is not None
    assert recovered.seed_job is not None
    assert recovered.seed_job.status is JobStatus.AWAITING_APPROVAL
