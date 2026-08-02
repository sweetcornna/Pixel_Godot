"""tileset 全链路：plan → create-tileset → validate → export。

8.1 的退出门槛在这里一次性验完。重点不在"命令能跑通"，而在几条只有 tileset
才有的硬性质：尺寸必须精确、整套共用一份调色板、没验过不许导出、
以及续跑不重复计费。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from PIL import Image
from typer.testing import CliRunner

from pixel_asset_forge.cli import EXIT_OK, app
from pixel_asset_forge.models import AssetManifest
from pixel_asset_forge.models.job import JobKind, JobStatus
from pixel_asset_forge.models.validation import ALL_CHECK_IDS, CheckResult
from pixel_asset_forge.storage import ArtifactStore

runner = CliRunner()

ASSET_ID = "grass_field"


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


@pytest.fixture
def tileset_env(
    examples_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, ArtifactStore]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PIXEL_ASSET_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config_path = _write_config(tmp_path / "mock.yaml")
    request_path = examples_dir / "grass_field.yaml"
    store = ArtifactStore.for_asset(tmp_path / "outputs", ASSET_ID)
    return request_path, config_path, store


def _create(request_path: Path, config_path: Path):
    result = runner.invoke(
        app, ["create-tileset", str(request_path), "--config", str(config_path)]
    )
    assert result.exit_code == EXIT_OK, result.stdout
    return result


def _validate(store: ArtifactStore, config_path: Path):
    return runner.invoke(
        app, ["validate", str(store.root), "--config", str(config_path)]
    )


def test_full_chain_produces_exact_tiles_one_palette_and_engine_files(
    tileset_env: tuple[Path, Path, ArtifactStore],
) -> None:
    """退出门槛 1 + 2 + 4：全链路、尺寸精确、整套共享调色板。"""
    request_path, config_path, store = tileset_env

    planned = runner.invoke(
        app, ["plan", str(request_path), "--config", str(config_path)]
    )
    assert planned.exit_code == EXIT_OK
    # 每块 tile 各要一次调用；报 0 次会让人以为这条链不花钱。
    assert "预计 API 调用 3 次" in planned.stdout
    assert "create-tileset" in planned.stdout

    _create(request_path, config_path)

    manifest = AssetManifest.load(store.manifest_path)
    assert manifest.asset_type == "tileset"
    assert manifest.tileset is not None
    assert manifest.tileset.tile_size == (32, 32)
    assert set(manifest.tileset.tiles) == {"grass_base", "dirt_path", "shallow_water"}
    # tile 满幅不透明，去背景那一步从未执行 —— 如实记 opaque，不填占位色。
    assert manifest.background.mode == "opaque"
    assert manifest.background.color_used is None

    for entry in manifest.tileset.tiles.values():
        image = Image.open(store.root / entry.image)
        assert image.size == (32, 32)

    assert len(manifest.palette.colors) <= 16
    for entry in manifest.tileset.tiles.values():
        rgb = Image.open(store.root / entry.image).convert("RGB")
        used = {"#{:02X}{:02X}{:02X}".format(*c) for _n, c in rgb.getcolors(4096)}
        assert used <= set(manifest.palette.colors)

    validated = _validate(store, config_path)
    assert validated.exit_code == EXIT_OK, validated.stdout
    report = json.loads(store.validation_report_path.read_text(encoding="utf-8"))
    assert report["passed"] is True

    exported = runner.invoke(
        app, ["export", str(store.root), "--config", str(config_path)]
    )
    assert exported.exit_code == EXIT_OK, exported.stdout

    tres = (store.exports / "godot" / f"{ASSET_ID}_tileset.tres").read_text(
        encoding="utf-8"
    )
    assert 'type="TileSet"' in tres
    # 两个尺寸都要写且一致：只写其一时 Godot 会用默认 16×16 去切图集。
    assert "texture_region_size = Vector2i(32, 32)" in tres
    assert "tile_size = Vector2i(32, 32)" in tres
    # 每块 tile 都必须有 `列:行/0 = 0`，少一行那一格在编辑器里就是空的。
    assert tres.count("/0 = 0") == 3
    assert (store.exports / "godot" / f"{ASSET_ID}.png").exists()

    payload = json.loads(
        (store.exports / "generic-json" / f"{ASSET_ID}.json").read_text(encoding="utf-8")
    )
    assert payload["tile_size"] == {"width": 32, "height": 32}
    assert set(payload["tiles"]) == set(manifest.tileset.tiles)
    for tile in payload["tiles"].values():
        assert tile["x"] == tile["column"] * 32
        assert tile["y"] == tile["row"] * 32

    assert (store.previews / "contact-sheet.png").exists()


def test_seamless_checks_run_and_report_lists_every_defence(
    tileset_env: tuple[Path, Path, ArtifactStore],
) -> None:
    """退出门槛 3 的通过侧 + 报告完整性。

    失败侧由 tests/unit/test_seamless.py 的三个反例守着（渐变 / 带边框 / 暗角）——
    那里能精确构造失败形态，这里只能拿 mock 的产出，验不了失败。
    """
    request_path, config_path, store = tileset_env
    _create(request_path, config_path)
    _validate(store, config_path)

    report = json.loads(store.validation_report_path.read_text(encoding="utf-8"))
    by_id: dict[str, list[dict]] = {}
    for check in report["checks"]:
        by_id.setdefault(check["id"], []).append(check)

    # 两条无缝判据都必须真的跑过，且逐块 tile 各跑一次。
    for check_id in ("tile_seam", "tile_border"):
        ran = by_id[check_id]
        assert len(ran) == 3
        assert {c["target"] for c in ran} == {"grass_base", "dirt_path", "shallow_water"}
        assert all(c["result"] == CheckResult.PASS.value for c in ran)
        assert all(c["measured"] is not None for c in ran)

    # 报告必须列全防线 —— 不能把"没运行"呈现成"零跳过"（同静态家族口径）。
    assert set(by_id) == set(ALL_CHECK_IDS)
    not_applicable = {
        check["id"]
        for checks in by_id.values()
        for check in checks
        if check.get("skip_reason") == "not_applicable"
    }
    assert "anchor_drift" in not_applicable
    assert "key_color_residue" in not_applicable


def test_export_is_refused_before_validation(
    tileset_env: tuple[Path, Path, ArtifactStore],
) -> None:
    """没验过不许交付 —— tileset 与静态资产共用这条硬闸。"""
    request_path, config_path, store = tileset_env
    _create(request_path, config_path)
    assert AssetManifest.load(store.manifest_path).status == "processed"

    refused = runner.invoke(
        app, ["export", str(store.root), "--config", str(config_path)]
    )
    assert refused.exit_code != EXIT_OK
    assert not (store.exports / "godot").exists()


def test_rerun_reuses_sources_and_spends_no_new_calls(
    tileset_env: tuple[Path, Path, ArtifactStore],
) -> None:
    """续跑不重复计费：已取回的 tile 不再调用 API。"""
    request_path, config_path, store = tileset_env
    first = _create(request_path, config_path)
    assert "本次调用" in first.stdout

    log_before = store.generation_log_path.read_bytes()
    sources_before = {
        path.name: path.read_bytes() for path in sorted(store.source.glob("*.png"))
    }
    assert len(sources_before) == 3

    again = _create(request_path, config_path)
    assert "本次调用" in again.stdout

    assert store.generation_log_path.read_bytes() == log_before
    assert {
        path.name: path.read_bytes() for path in sorted(store.source.glob("*.png"))
    } == sources_before

    table = store.load_job_table()
    assert table is not None
    tiles = table.of_kind(JobKind.TILE)
    assert len(tiles) == 3
    assert all(job.status is JobStatus.PROCESSED for job in tiles)


def test_tile_ids_map_to_stable_atlas_cells(
    tileset_env: tuple[Path, Path, ArtifactStore],
) -> None:
    """图集坐标必须可复现 —— 否则重导一次，地图里摆好的 tile 会集体错位。"""
    request_path, config_path, store = tileset_env
    _create(request_path, config_path)
    _validate(store, config_path)
    runner.invoke(app, ["export", str(store.root), "--config", str(config_path)])
    first = json.loads(
        (store.exports / "generic-json" / f"{ASSET_ID}.json").read_text(encoding="utf-8")
    )["tiles"]

    runner.invoke(app, ["export", str(store.root), "--config", str(config_path)])
    second = json.loads(
        (store.exports / "generic-json" / f"{ASSET_ID}.json").read_text(encoding="utf-8")
    )["tiles"]
    assert first == second
    # 按 tile_id 字典序铺格：dirt_path < grass_base < shallow_water
    assert (first["dirt_path"]["column"], first["dirt_path"]["row"]) == (0, 0)
    assert (first["shallow_water"]["column"], first["shallow_water"]["row"]) == (0, 1)


def test_static_request_is_rejected_by_create_tileset(
    examples_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """走错命令要说清楚该走哪条，而不是含糊报错。"""
    monkeypatch.chdir(tmp_path)
    config_path = _write_config(tmp_path / "mock.yaml")
    data = yaml.safe_load(
        (examples_dir / "potion_pack.yaml").read_text(encoding="utf-8")
    )
    request = {
        "schema_version": "1.0",
        "asset_id": "lone_potion",
        "asset_type": "pickup",
        "description": data["assets"][0]["description"],
        "style": data["shared"]["style"],
        "export": {"targets": ["generic-json"]},
    }
    path = tmp_path / "potion.yaml"
    path.write_text(yaml.safe_dump(request, sort_keys=False), encoding="utf-8")

    result = runner.invoke(
        app, ["create-tileset", str(path), "--config", str(config_path)]
    )
    assert result.exit_code != EXIT_OK
    assert "create-asset" in result.stderr
