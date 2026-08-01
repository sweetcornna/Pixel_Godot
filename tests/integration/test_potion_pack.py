"""potion_pack 并发、失败隔离与恢复。"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest
import yaml

from pixel_asset_forge.config import Config
from pixel_asset_forge.models import AssetManifest
from pixel_asset_forge.models.job import JobStatus
from pixel_asset_forge.models.pack import load_pack
from pixel_asset_forge.pipelines import asset_pack
from pixel_asset_forge.pipelines.asset_pack import PackRunControl, run_asset_pack
from pixel_asset_forge.planning import plan_pack
from pixel_asset_forge.storage import ArtifactStore


def config(tmp_path: Path) -> Config:
    return Config(
        provider="mock",
        model="mock-image",
        output_dir=tmp_path / "outputs",
        cache_dir=tmp_path / "cache",
        max_concurrency=2,
    )


def save_plan(pack_path: Path, cfg: Config) -> None:
    result = plan_pack(
        load_pack(pack_path), provider=cfg.provider, model=cfg.model
    )
    for asset in result.assets:
        ArtifactStore.for_asset(cfg.output_dir, asset.request.asset_id).ensure().save_job_table(
            asset.jobs
        )


def test_three_potions_complete_and_resume_by_skipping(
    tmp_path: Path, examples_dir: Path
) -> None:
    cfg = config(tmp_path)
    pack_path = examples_dir / "potion_pack.yaml"
    save_plan(pack_path, cfg)

    first = asyncio.run(run_asset_pack(pack_path, cfg))
    assert first.worker_count == 2
    assert first.counts["exported"] == 3
    assert first.counts["provider_failed"] == 0
    assert first.provider == "mock"
    assert first.model == "mock-image"

    palettes = []
    for outcome in first.assets:
        store = ArtifactStore.for_asset(cfg.output_dir, outcome.asset_id)
        manifest = AssetManifest.load(store.manifest_path)
        palettes.append(manifest.palette.colors)
        assert manifest.provider.name == "mock"
        assert manifest.provider.model == "mock-image"
        log = json.loads(store.generation_log_path.read_text(encoding="utf-8"))
        assert len(log) == 1
        assert log[0]["provider"] == "mock"
        assert log[0]["model"] == "mock-image"
    assert palettes[0] == palettes[1] == palettes[2]

    second = asyncio.run(run_asset_pack(pack_path, cfg))
    assert second.counts["skipped"] == 3
    assert second.counts["exported"] == 0
    for outcome in second.assets:
        store = ArtifactStore.for_asset(cfg.output_dir, outcome.asset_id)
        log = json.loads(store.generation_log_path.read_text(encoding="utf-8"))
        assert len(log) == 1


def test_one_provider_failure_does_not_cancel_siblings(
    tmp_path: Path, examples_dir: Path
) -> None:
    cfg = config(tmp_path)
    data = yaml.safe_load((examples_dir / "potion_pack.yaml").read_text(encoding="utf-8"))
    data["pack_id"] = "partial_potions"
    data["assets"][1]["description"] = (
        "A mutilated gore mana potion description used to trigger mock moderation."
    )
    path = tmp_path / "partial.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    save_plan(path, cfg)

    summary = asyncio.run(run_asset_pack(path, cfg))
    assert summary.counts["provider_failed"] == 1
    assert summary.counts["exported"] == 2
    failed = next(asset for asset in summary.assets if asset.outcome == "provider_failed")
    assert failed.asset_id == "mana_potion"
    assert failed.error_code == "provider_moderation_blocked"
    assert "sk-" not in (failed.error or "")
    assert (cfg.output_dir / "_packs" / "partial_potions" / "pack-summary.json").exists()


def test_summary_write_failure_does_not_kill_worker_or_deadlock(
    tmp_path: Path,
    examples_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = config(tmp_path)
    data = yaml.safe_load((examples_dir / "potion_pack.yaml").read_text(encoding="utf-8"))
    data["pack_id"] = "summary_failure"
    data["assets"] = data["assets"][:1]
    path = tmp_path / "summary-failure.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    save_plan(path, cfg)

    original_persist = asset_pack._persist_summary
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated summary write failure")
        return original_persist(*args, **kwargs)

    async def inline_to_thread(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asset_pack, "_persist_summary", fail_once)
    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)
    caplog.set_level(logging.ERROR, logger="pixel_asset_forge.pipeline.asset_pack")

    summary = asyncio.run(run_asset_pack(path, cfg))

    assert summary.counts["exported"] == 1
    assert calls >= 2
    assert "worker 将继续运行" in caplog.text


def test_retry_failed_resets_and_reclaims_asset(
    tmp_path: Path,
    examples_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = config(tmp_path)
    data = yaml.safe_load((examples_dir / "potion_pack.yaml").read_text(encoding="utf-8"))
    data["pack_id"] = "retry_failed"
    data["assets"] = data["assets"][:1]
    path = tmp_path / "retry-failed.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    save_plan(path, cfg)

    store = ArtifactStore.for_asset(cfg.output_dir, "health_potion")
    table = store.load_job_table()
    assert table is not None
    job = next(iter(table))
    job.status = JobStatus.FAILED
    job.error = "simulated previous failure"
    store.save_job_table(table)

    async def inline_to_thread(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)

    unchanged = asyncio.run(run_asset_pack(path, cfg))
    assert unchanged.assets[0].error_code == "persisted_failure"
    assert not store.generation_log_path.exists()

    retried = asyncio.run(run_asset_pack(path, cfg, retry_failed=True))
    assert retried.counts["exported"] == 1
    assert retried.assets[0].resumed is True
    log = json.loads(store.generation_log_path.read_text(encoding="utf-8"))
    assert len(log) == 1


def test_request_stop_pauses_undispatched_assets_and_resume_completes(
    tmp_path: Path,
    examples_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = config(tmp_path)
    pack_path = examples_dir / "potion_pack.yaml"
    save_plan(pack_path, cfg)
    control = PackRunControl()
    stop_requested = False

    async def stop_after_dispatch(function, /, *args, **kwargs):
        nonlocal stop_requested
        if not stop_requested:
            stop_requested = True
            control.request_stop()
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", stop_after_dispatch)
    paused = asyncio.run(run_asset_pack(pack_path, cfg, control=control))

    assert stop_requested is True
    assert paused.interrupted is True
    assert paused.counts["paused"] == 3
    assert paused.counts["exported"] == 0
    assert paused.assets[0].job_status == "generated"
    assert all(asset.outcome == "paused" for asset in paused.assets)
    assert all(asset.job_status == "planned" for asset in paused.assets[1:])
    assert all(
        asset.error == "批次已暂停，资产尚未派发" for asset in paused.assets[1:]
    )

    async def inline_to_thread(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)
    resumed = asyncio.run(run_asset_pack(pack_path, cfg))

    assert resumed.interrupted is False
    assert resumed.counts["exported"] == 3
    assert resumed.counts["paused"] == 0
    assert resumed.assets[0].resumed is True
    for outcome in resumed.assets:
        store = ArtifactStore.for_asset(cfg.output_dir, outcome.asset_id)
        log = json.loads(store.generation_log_path.read_text(encoding="utf-8"))
        assert len(log) == 1
