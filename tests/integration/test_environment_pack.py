"""environment_pack 的 CLI 规划、生成、验证与逐资产导出闭环。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pixel_asset_forge.cli import EXIT_OK, app
from pixel_asset_forge.models import AssetManifest, load_pack
from pixel_asset_forge.models.job import JobKind, JobStatus
from pixel_asset_forge.storage import ArtifactStore

runner = CliRunner()


def test_environment_pack_plan_create_and_export_each_asset_by_id(
    examples_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PIXEL_ASSET_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    async def inline_to_thread(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    # WSL 下只在测试进程内规避 asyncio worker 唤醒假死。
    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)

    config = tmp_path / "mock.yaml"
    config.write_text(
        "provider: mock\n"
        "model: mock-image\n"
        "output_dir: outputs\n"
        "cache_dir: cache\n"
        "max_concurrency: 2\n",
        encoding="utf-8",
    )
    pack_path = examples_dir / "environment_pack.yaml"
    pack = load_pack(pack_path)
    asset_ids = [asset.asset_id for asset in pack.assets]

    planned = runner.invoke(
        app,
        ["plan", str(pack_path), "--config", str(config), "--save"],
    )
    assert planned.exit_code == EXIT_OK
    assert all(asset_id in planned.stdout for asset_id in asset_ids)

    created = runner.invoke(
        app,
        ["create-asset-pack", str(pack_path), "--config", str(config), "--json"],
    )
    assert created.exit_code == EXIT_OK
    payload = json.loads(created.stdout)
    assert payload["pack_type"] == "environment_pack"
    assert payload["counts"]["exported"] == 3
    assert [asset["asset_id"] for asset in payload["assets"]] == asset_ids

    for asset_id in asset_ids:
        store = ArtifactStore.for_asset(tmp_path / "outputs", asset_id)
        manifest = AssetManifest.load(store.manifest_path)
        table = store.load_job_table()
        assert manifest.asset_type == "environment_object"
        assert manifest.status == "exported"
        assert manifest.static_image is not None
        assert table is not None
        jobs = tuple(table)
        assert len(jobs) == 1
        assert jobs[0].kind is JobKind.STATIC
        assert jobs[0].status is JobStatus.EXPORTED

        exported = runner.invoke(
            app,
            ["export", asset_id, "--config", str(config), "--no-contact-sheet"],
        )
        assert exported.exit_code == EXIT_OK
        assert (store.exports / "generic-json" / f"{asset_id}.json").exists()
        assert (store.exports / "godot" / f"{asset_id}.png").exists()
