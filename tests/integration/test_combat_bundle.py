"""combat_bundle 的两遍执行、缩放基准收敛与 death 豁免闭环。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from pixel_asset_forge.cli import EXIT_OK, app
from pixel_asset_forge.config import Config
from pixel_asset_forge.models import AssetManifest, load_pack
from pixel_asset_forge.models.job import JobKind, JobStatus
from pixel_asset_forge.models.manifest import ScaleProfileInfo
from pixel_asset_forge.pipelines import asset_pack
from pixel_asset_forge.pipelines.asset_pack import (
    AWAITING_APPROVAL_MESSAGE,
    SCALE_PROFILE_REPROCESS_MESSAGE,
)
from pixel_asset_forge.storage import ArtifactStore

runner = CliRunner()


def _write_config(path: Path) -> Path:
    path.write_text(
        "provider: mock\n"
        "model: mock-image\n"
        "output_dir: outputs\n"
        "cache_dir: cache\n"
        "max_concurrency: 1\n",
        encoding="utf-8",
    )
    return path


def _config(tmp_path: Path) -> Config:
    return Config(
        provider="mock",
        model="mock-image",
        output_dir=tmp_path / "outputs",
        cache_dir=tmp_path / "cache",
        max_concurrency=1,
    )


def _run_first_pass_and_approve(
    pack_path: Path,
    config_path: Path,
    *,
    asset_id: str,
    approval_action: str = "attack",
) -> tuple[dict[str, object], str]:
    planned = runner.invoke(
        app,
        ["plan", str(pack_path), "--config", str(config_path), "--save"],
    )
    assert planned.exit_code == EXIT_OK

    first = runner.invoke(
        app,
        ["create-asset-pack", str(pack_path), "--config", str(config_path), "--json"],
    )
    assert first.exit_code == EXIT_OK
    first_payload = json.loads(first.stdout)
    assert first_payload["counts"]["awaiting_approval"] == 1
    assert first_payload["assets"][0]["error"] == AWAITING_APPROVAL_MESSAGE

    approved = runner.invoke(
        app,
        [
            "create-animation",
            "--asset",
            asset_id,
            "--action",
            approval_action,
            "--direction",
            "down",
            "--approve-seed",
            "--config",
            str(config_path),
        ],
    )
    assert approved.exit_code == EXIT_OK
    assert "seed 已批准" in approved.stdout
    return first_payload, planned.stdout


def test_combat_bundle_cli_converges_superseded_profile_and_exports(
    examples_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """退出门槛 1–3：完整 CLI 链、自动 process、summary 与 death 豁免。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PIXEL_ASSET_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    async def inline_to_thread(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)
    config_path = _write_config(tmp_path / "mock.yaml")
    pack_path = examples_dir / "combat_bundle.yaml"
    pack = load_pack(pack_path)
    asset_id = pack.assets[0].asset_id

    _first_payload, plan_stdout = _run_first_pass_and_approve(
        pack_path,
        config_path,
        asset_id=asset_id,
    )
    assert "预计 seed API 调用 1" in plan_stdout
    assert "预计动画 API 调用 12" in plan_stdout

    store = ArtifactStore.for_asset(tmp_path / "outputs", asset_id)
    manifest = AssetManifest.load(store.manifest_path)
    assert manifest.asset_type == "character"
    assert manifest.scale_profile is not None
    assert manifest.scale_profile.needs_reprocess is False

    # 构造一次真实的“已有旧基准”续跑：下一动作会顶替它，并把本次按旧基准
    # 生成的图留给协调器统一重处理。
    manifest.scale_profile = ScaleProfileInfo(
        reference=manifest.scale_profile.reference,
        subject_ratio=0.01,
        canvas_fraction=manifest.scale_profile.canvas_fraction,
    )
    manifest.save(store.manifest_path)

    process_calls: list[Path] = []
    real_run_process = asset_pack.run_process

    def tracking_run_process(asset_dir: str | Path, *, only: str | None = None):
        process_calls.append(Path(asset_dir))
        return real_run_process(asset_dir, only=only)

    monkeypatch.setattr(asset_pack, "run_process", tracking_run_process)
    second = runner.invoke(
        app,
        ["create-asset-pack", str(pack_path), "--config", str(config_path), "--json"],
    )
    assert second.exit_code == EXIT_OK
    second_payload = json.loads(second.stdout)
    outcome = second_payload["assets"][0]

    assert second_payload["counts"]["exported"] == 1
    assert second_payload["counts"]["provider_failed"] == 0
    assert second_payload["counts"]["processing_failed"] == 0
    assert [path.resolve() for path in process_calls] == [store.root.resolve()]
    assert outcome["scale_profile_reprocessed"] is True
    assert outcome["processing_notes"] == [SCALE_PROFILE_REPROCESS_MESSAGE]
    assert {item["action"] for item in outcome["validation_exemptions"]} == {"death"}
    assert all(
        item["skip_reason"] == "action_exempt"
        for item in outcome["validation_exemptions"]
    )
    assert all(
        item["checks"] == ["anchor_drift", "height_variation", "silhouette_variation"]
        for item in outcome["validation_exemptions"]
    )

    manifest = AssetManifest.load(store.manifest_path)
    table = store.load_job_table()
    assert manifest.status == "exported"
    assert manifest.scale_profile is not None
    assert manifest.scale_profile.needs_reprocess is False
    assert set(manifest.animations) == {
        f"{action}_{direction}"
        for action in ("attack", "hurt", "death")
        for direction in ("down", "left", "right", "up")
    }
    assert table is not None
    assert table.seed_job is not None
    assert table.seed_job.status is JobStatus.APPROVED
    assert all(
        job.status is JobStatus.EXPORTED
        for job in table.of_kind(JobKind.ANIMATION)
    )
    assert len(json.loads(store.generation_log_path.read_text(encoding="utf-8"))) == 13

    report = json.loads(store.validation_report_path.read_text(encoding="utf-8"))
    death_exemptions = [
        check
        for check in report["checks"]
        if check.get("action") == "death"
        and check.get("skip_reason") == "action_exempt"
    ]
    assert len(death_exemptions) == 12

    summary_path = tmp_path / "outputs" / "_packs" / pack.pack_id / "pack-summary.json"
    persisted = json.loads(summary_path.read_text(encoding="utf-8"))
    assert persisted["assets"][0]["processing_notes"] == [
        SCALE_PROFILE_REPROCESS_MESSAGE
    ]

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


def test_combat_bundle_without_supersession_does_not_run_extra_process(
    examples_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """退出门槛 2：基准未顶替时不额外跑全量处理。"""
    monkeypatch.chdir(tmp_path)

    async def inline_to_thread(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)
    data = yaml.safe_load(
        (examples_dir / "combat_bundle.yaml").read_text(encoding="utf-8")
    )
    data["pack_id"] = "combat_no_supersession"
    data["assets"][0]["asset_id"] = "knight_no_supersession"
    data["shared"]["style"]["target_size"] = [32, 32]
    animations = data["shared"]["animations"]
    data["shared"]["animations"] = sorted(
        animations,
        key=lambda animation: ("death", "attack", "hurt").index(animation["name"]),
    )
    for animation in data["shared"]["animations"]:
        animation["directions"] = ["down"]
    pack_path = tmp_path / "combat-no-supersession.yaml"
    pack_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    config_path = _write_config(tmp_path / "mock.yaml")
    asset_id = data["assets"][0]["asset_id"]

    _run_first_pass_and_approve(
        pack_path,
        config_path,
        asset_id=asset_id,
        approval_action="death",
    )

    store = ArtifactStore.for_asset(tmp_path / "outputs", asset_id)

    process_calls: list[Path] = []

    def unexpected_run_process(asset_dir: str | Path, *, only: str | None = None):
        process_calls.append(Path(asset_dir))
        raise AssertionError("基准未顶替时不应调用全量 process")

    monkeypatch.setattr(asset_pack, "run_process", unexpected_run_process)
    second = runner.invoke(
        app,
        ["create-asset-pack", str(pack_path), "--config", str(config_path), "--json"],
    )
    assert second.exit_code == EXIT_OK
    payload = json.loads(second.stdout)
    outcome = payload["assets"][0]

    assert payload["counts"]["exported"] == 1
    assert process_calls == []
    assert outcome["scale_profile_reprocessed"] is False
    assert outcome["processing_notes"] == []
    assert outcome["validation_exemptions"] == [
        {
            "target": "death_down",
            "action": "death",
            "checks": ["anchor_drift", "height_variation", "silhouette_variation"],
            "skip_reason": "action_exempt",
        }
    ]

    manifest = AssetManifest.load(store.manifest_path)
    assert manifest.scale_profile is not None
    assert manifest.scale_profile.needs_reprocess is False
