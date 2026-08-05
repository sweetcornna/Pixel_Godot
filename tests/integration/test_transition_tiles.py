"""Sprint 8.5 纵切：请求 → prompt → 像素实测 → Manifest → validate → 导出。"""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import pytest
import yaml
from PIL import Image
from typer.testing import CliRunner

from pixel_asset_forge.cli import EXIT_OK, app
from pixel_asset_forge.config import Config
from pixel_asset_forge.exporters import get_exporter
from pixel_asset_forge.exporters.godot import build_tileset
from pixel_asset_forge.models.manifest import AssetManifest
from pixel_asset_forge.models.request import parse_request
from pixel_asset_forge.models.validation import CheckResult
from pixel_asset_forge.pipelines.tileset import create_tileset
from pixel_asset_forge.prompts.compiler import compile_tile_prompt
from pixel_asset_forge.providers.mock import MockImageProvider
from pixel_asset_forge.storage import ArtifactStore
from pixel_asset_forge.validation.terrain import CORNER_NAMES, TERRAIN_DISTANCE_MAX

runner = CliRunner()
ASSET_ID = "transition_tiles"


class CornerOrderIgnoringMock(MockImageProvider):
    """模拟模型忽略写反的 corners，仍画出原来的上草下土像素。"""

    def _generate(
        self, prompt: str, size: tuple[int, int], model: str
    ) -> tuple[bytes, str | None]:
        prompt = prompt.replace(
            "Terrain corner layout: top-left = dirt; top-right = dirt; "
            "bottom-left = grass; bottom-right = grass.",
            "Terrain corner layout: top-left = grass; top-right = grass; "
            "bottom-left = dirt; bottom-right = dirt.",
        )
        return super()._generate(prompt, size, model)


@pytest.fixture
def transition_env(
    examples_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, ArtifactStore]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PIXEL_ASSET_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    request_path = tmp_path / "transition_tiles.yaml"
    request_path.write_bytes((examples_dir / "transition_tiles.yaml").read_bytes())
    config_path = tmp_path / "mock.yaml"
    config_path.write_text(
        "provider: mock\n"
        "model: mock-image\n"
        "output_dir: outputs\n"
        "cache_dir: cache\n"
        "max_concurrency: 1\n",
        encoding="utf-8",
    )
    return request_path, config_path, ArtifactStore.for_asset(tmp_path / "outputs", ASSET_ID)


def create(request_path: Path, config_path: Path):  # type: ignore[no-untyped-def]
    result = runner.invoke(
        app, ["create-tileset", str(request_path), "--config", str(config_path)]
    )
    assert result.exit_code == EXIT_OK, result.stdout
    return result


def validate(store: ArtifactStore, config_path: Path):  # type: ignore[no-untyped-def]
    return runner.invoke(app, ["validate", str(store.root), "--config", str(config_path)])


def terrain_checks(store: ArtifactStore) -> list[dict]:
    report = json.loads(store.validation_report_path.read_text(encoding="utf-8"))
    return [check for check in report["checks"] if check["id"] == "tile_terrain"]


def checks_for(store: ArtifactStore, check_id: str) -> list[dict]:
    report = json.loads(store.validation_report_path.read_text(encoding="utf-8"))
    return [check for check in report["checks"] if check["id"] == check_id]


def test_transition_tiles_complete_the_offline_vertical_slice(
    transition_env: tuple[Path, Path, ArtifactStore],
) -> None:
    request_path, config_path, store = transition_env
    create(request_path, config_path)

    manifest = AssetManifest.load(store.manifest_path)
    assert manifest.tileset is not None and manifest.tileset.terrain is not None
    assert manifest.tileset.terrain.distance_max == TERRAIN_DISTANCE_MAX
    assert manifest.tileset.terrain.calibrated is False
    transition = manifest.tileset.tiles["grass_dirt_corner"].terrain
    assert transition is not None
    assert transition.declared_corners == ("grass", "grass", "dirt", "dirt")
    assert transition.measured_corners == transition.declared_corners
    assert transition.distances == (0.0, 0.0, 0.0, 0.0)

    log_before = store.generation_log_path.read_bytes()
    validated = validate(store, config_path)
    assert validated.exit_code == EXIT_OK, validated.stdout
    checks = terrain_checks(store)
    assert len(checks) == 12
    assert all(check["result"] == CheckResult.PASS.value for check in checks)
    seams = checks_for(store, "tile_seam")
    horizontal = next(
        check for check in seams
        if check["target"] == "grass_dirt_corner/horizontal"
    )
    vertical = next(
        check for check in seams
        if check["target"] == "grass_dirt_corner/vertical"
    )
    assert horizontal["result"] == "pass" and horizontal["measured"] <= 3.0
    assert vertical["result"] == "skip"
    assert "跳过垂直自接缝" in vertical["message"]
    border = next(
        check for check in checks_for(store, "tile_border")
        if check["target"] == "grass_dirt_corner"
    )
    assert border["result"] == "pass" and border["measured"] <= 4.0

    exported = runner.invoke(
        app, ["export", str(store.root), "--config", str(config_path)]
    )
    assert exported.exit_code == EXIT_OK, exported.stdout
    payload = json.loads(
        (store.exports / "generic-json" / f"{ASSET_ID}.json").read_text(encoding="utf-8")
    )
    terrain = payload["terrain"]
    assert terrain["corner_order"] == list(CORNER_NAMES)
    assert terrain["distance_max"] == TERRAIN_DISTANCE_MAX
    assert terrain["calibrated"] is False
    assert terrain["tiles"]["grass_dirt_corner"] == {
        "declared_corners": ["grass", "grass", "dirt", "dirt"],
        "measured_corners": ["grass", "grass", "dirt", "dirt"],
        "distances": [0.0, 0.0, 0.0, 0.0],
    }

    # 推导、复算、导出都只读像素；整条后半链没有新增 provider 调用。
    assert store.generation_log_path.read_bytes() == log_before


def test_godot_terrain_set_and_peering_bits_complete_the_export_slice(
    transition_env: tuple[Path, Path, ArtifactStore],
) -> None:
    request_path, config_path, store = transition_env
    create(request_path, config_path)
    assert validate(store, config_path).exit_code == EXIT_OK

    exported = runner.invoke(
        app,
        ["export", str(store.root), "-t", "godot", "--config", str(config_path)],
    )
    assert exported.exit_code == EXIT_OK, exported.stdout
    text = (store.exports / "godot" / f"{ASSET_ID}_tileset.tres").read_text(
        encoding="utf-8"
    )

    # Godot 4.7.1 探针读回：1 是只需要四角的 Corners 模式。
    assert "terrain_set_0/mode = 1" in text
    assert 'terrain_set_0/terrain_0/name = "dirt"' in text
    assert 'terrain_set_0/terrain_1/name = "grass"' in text

    # 图集按 tile_id 排序：dirt_base=(0,0)，grass_base=(1,0)，transition=(0,1)。
    prefix = "0:1/0"
    assert f"{prefix}/terrain_set = 0" in text
    assert f"{prefix}/terrain = 1" in text
    assert f"{prefix}/terrains_peering_bit/top_left_corner = 1" in text
    assert f"{prefix}/terrains_peering_bit/top_right_corner = 1" in text
    assert f"{prefix}/terrains_peering_bit/bottom_left_corner = 0" in text
    assert f"{prefix}/terrains_peering_bit/bottom_right_corner = 0" in text


def test_godot_peering_bits_use_measured_corners_not_declared_corners(
    transition_env: tuple[Path, Path, ArtifactStore],
) -> None:
    request_path, config_path, store = transition_env
    create(request_path, config_path)
    manifest = AssetManifest.load(store.manifest_path)
    raw = manifest.model_dump(mode="json")
    transition = raw["tileset"]["tiles"]["grass_dirt_corner"]["terrain"]
    transition["declared_corners"] = ["stone", "stone", "stone", "stone"]
    mismatched = AssetManifest.model_validate(raw)

    built = build_tileset(
        mismatched,
        {"dirt_base": (0, 0), "grass_base": (1, 0), "grass_dirt_corner": (0, 1)},
        f"{ASSET_ID}.png",
        (32, 32),
    )

    assert "stone" not in built.text
    assert 'terrain_set_0/terrain_0/name = "dirt"' in built.text
    assert 'terrain_set_0/terrain_1/name = "grass"' in built.text
    assert "0:1/0/terrains_peering_bit/top_left_corner = 1" in built.text
    assert "0:1/0/terrains_peering_bit/top_right_corner = 1" in built.text
    assert "0:1/0/terrains_peering_bit/bottom_left_corner = 0" in built.text
    assert "0:1/0/terrains_peering_bit/bottom_right_corner = 0" in built.text


def test_godot_skips_the_whole_terrain_annotation_when_one_corner_is_unknown(
    transition_env: tuple[Path, Path, ArtifactStore],
) -> None:
    request_path, config_path, store = transition_env
    create(request_path, config_path)
    manifest = AssetManifest.load(store.manifest_path)
    raw = manifest.model_dump(mode="json")
    transition = raw["tileset"]["tiles"]["grass_dirt_corner"]["terrain"]
    transition["measured_corners"][0] = None
    unknown = AssetManifest.model_validate(raw)

    out = store.exports / "godot-unknown"
    result = get_exporter("godot").export(unknown, store.root, out)
    text = (out / f"{ASSET_ID}_tileset.tres").read_text(encoding="utf-8")

    # 普通图集格保留，但整格没有 terrain_set / terrain / peering bit；不能把 None
    # 偷偷映射为 dirt(0)、grass(1) 或 Godot 的 empty(-1) 角后继续声称它已标注。
    assert "0:1/0 = 0" in text
    assert "0:1/0/terrain_set" not in text
    assert "0:1/0/terrain =" not in text
    assert "0:1/0/terrains_peering_bit" not in text
    assert any(
        "measured_corners 含 unknown" in note and "grass_dirt_corner" in note
        for note in result.notes
    )
    handoff = (out / "GODOT-README.md").read_text(encoding="utf-8")
    assert "grass_dirt_corner" in handoff and "unknown" in handoff


def test_slanted_transition_is_caught_by_the_required_horizontal_seam(
    transition_env: tuple[Path, Path, ArtifactStore],
) -> None:
    request_path, config_path, store = transition_env
    create(request_path, config_path)
    manifest = AssetManifest.load(store.manifest_path)
    assert manifest.tileset is not None
    transition_path = store.root / manifest.tileset.tiles["grass_dirt_corner"].image

    size = 32
    def material(base: tuple[int, int, int], seed: int) -> np.ndarray:
        rng = np.random.default_rng(seed)
        rgb = np.clip(
            np.array(base)[None, None, :]
            + rng.integers(-8, 9, (size, size, 1)),
            0,
            255,
        ).astype(np.uint8)
        return np.dstack([rgb, np.full((size, size), 255, dtype=np.uint8)])

    grass = material((82, 132, 68), 1)
    dirt = material((146, 92, 48), 2)
    slanted = np.empty_like(grass)
    for x in range(size):
        boundary = 8 + (16 * x) // (size - 1)
        slanted[:boundary, x] = grass[:boundary, x]
        slanted[boundary:, x] = dirt[boundary:, x]
    Image.fromarray(slanted, "RGBA").save(transition_path)

    assert validate(store, config_path).exit_code != EXIT_OK
    seams = checks_for(store, "tile_seam")
    horizontal = next(
        check for check in seams
        if check["target"] == "grass_dirt_corner/horizontal"
    )
    vertical = next(
        check for check in seams
        if check["target"] == "grass_dirt_corner/vertical"
    )
    assert horizontal["result"] == "fail"
    assert horizontal["measured"] == 3.933
    assert horizontal["threshold"] == 3.0
    assert vertical["result"] == "skip"


def test_counterexample_a_replacing_one_pixel_quadrant_names_the_corner(
    transition_env: tuple[Path, Path, ArtifactStore],
) -> None:
    request_path, config_path, store = transition_env
    create(request_path, config_path)
    assert validate(store, config_path).exit_code == EXIT_OK
    before = next(
        check for check in terrain_checks(store)
        if check["target"] == "grass_dirt_corner/top_left"
    )
    assert before["result"] == "pass" and before["measured"] == 0.0

    manifest = AssetManifest.load(store.manifest_path)
    assert manifest.tileset is not None
    transition_path = store.root / manifest.tileset.tiles["grass_dirt_corner"].image
    dirt_path = store.root / manifest.tileset.tiles["dirt_base"].image
    transition = np.array(Image.open(transition_path).convert("RGBA"))
    dirt = np.array(Image.open(dirt_path).convert("RGBA"))
    transition[:16, :16] = dirt[:16, :16]
    Image.fromarray(transition, "RGBA").save(transition_path)

    failed = validate(store, config_path)
    assert failed.exit_code != EXIT_OK
    after = next(
        check for check in terrain_checks(store)
        if check["target"] == "grass_dirt_corner/top_left"
    )
    assert after["result"] == "fail"
    assert after["measured"] == 0.0 and after["threshold"] == TERRAIN_DISTANCE_MAX
    assert "声明 grass，像素实测 dirt" in after["message"]


def test_counterexample_b_reversed_request_order_fails_against_unchanged_pixels(
    transition_env: tuple[Path, Path, ArtifactStore],
) -> None:
    request_path, config_path, store = transition_env
    data = yaml.safe_load(request_path.read_text(encoding="utf-8"))
    data["tileset"]["tiles"][2]["terrain"]["corners"] = [
        "dirt", "dirt", "grass", "grass"
    ]
    request_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    config = Config(
        provider="mock",
        model="mock-image",
        output_dir=store.root.parent,
        cache_dir=store.root.parent.parent / "cache",
    )
    create_tileset(request_path, config, provider=CornerOrderIgnoringMock())
    manifest = AssetManifest.load(store.manifest_path)
    assert manifest.tileset is not None
    terrain = manifest.tileset.tiles["grass_dirt_corner"].terrain
    assert terrain is not None
    assert terrain.declared_corners == ("dirt", "dirt", "grass", "grass")
    assert terrain.measured_corners == ("grass", "grass", "dirt", "dirt")
    failed = validate(store, config_path)
    assert failed.exit_code != EXIT_OK
    transition = [
        check for check in terrain_checks(store)
        if check["target"].startswith("grass_dirt_corner/")
    ]
    assert {check["target"] for check in transition if check["result"] == "fail"} == {
        f"grass_dirt_corner/{corner}" for corner in CORNER_NAMES
    }
    top_left = next(check for check in transition if check["target"].endswith("top_left"))
    assert top_left["measured"] == 0.0
    assert "声明 dirt，像素实测 grass" in top_left["message"]


def test_counterexample_c_outlier_is_reported_as_unknown(
    transition_env: tuple[Path, Path, ArtifactStore],
) -> None:
    request_path, config_path, store = transition_env
    create(request_path, config_path)
    manifest = AssetManifest.load(store.manifest_path)
    assert manifest.tileset is not None
    path = store.root / manifest.tileset.tiles["grass_dirt_corner"].image
    transition = np.array(Image.open(path).convert("RGBA"))
    rng = np.random.default_rng(20260804)
    transition[:16, :16, :3] = rng.integers(
        220, 256, (16, 16, 3), dtype=np.uint8
    )
    transition[:16, :16, 3] = 255
    Image.fromarray(transition, "RGBA").save(path)

    failed = validate(store, config_path)
    assert failed.exit_code != EXIT_OK
    unknown = next(
        check for check in terrain_checks(store)
        if check["target"] == "grass_dirt_corner/top_left"
    )
    assert unknown["result"] == "fail"
    assert unknown["measured"] > unknown["threshold"] == TERRAIN_DISTANCE_MAX
    assert "像素实测 unknown" in unknown["message"]


def test_mock_transition_generation_survives_a_fresh_provider_instance(
    transition_env: tuple[Path, Path, ArtifactStore],
) -> None:
    request_path, _config_path, _store = transition_env
    request = yaml.safe_load(request_path.read_text(encoding="utf-8"))
    parsed = parse_request(request)
    prompts = {
        tile.tile_id: compile_tile_prompt(parsed, tile)
        for tile in parsed.tile_list
    }
    bases = MockImageProvider()
    grass = np.array(
        Image.open(io.BytesIO(bases.generate(prompts["grass_base"].text, size=(1024, 1024)).image))
    )
    dirt = np.array(
        Image.open(io.BytesIO(bases.generate(prompts["dirt_base"].text, size=(1024, 1024)).image))
    )
    resumed = MockImageProvider()
    transition = np.array(
        Image.open(
            io.BytesIO(
                resumed.generate(
                    prompts["grass_dirt_corner"].text, size=(1024, 1024)
                ).image
            )
        )
    )

    assert np.array_equal(transition[:512, :512], grass[:512, :512])
    assert np.array_equal(transition[512:, 512:], dirt[512:, 512:])

def test_tile_weight_travels_from_request_through_manifest_into_map_generation(
    transition_env: tuple[Path, Path, ArtifactStore],
) -> None:
    """权重必须走完请求 → Manifest → create-map 这条链（PLAN §8.7）。

    create-map 读的是 Manifest 而不是请求，所以权重不写进 Manifest 就等于地图
    生成拿不到它 —— 那种断链会表现成"我调了 weight 却没反应"，很难查。
    """
    request_path, config_path, store = transition_env
    data = yaml.safe_load(request_path.read_text(encoding="utf-8"))
    for tile in data["tileset"]["tiles"]:
        # 抬高过渡块、压低两块 base：单材质地图的出现率应当被压下去。
        tile["weight"] = 8.0 if tile["tile_id"] == "grass_dirt_corner" else 0.25
    request_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    create(request_path, config_path)

    manifest = AssetManifest.load(store.manifest_path)
    assert manifest.tileset is not None
    weights = {
        tile_id: entry.weight for tile_id, entry in manifest.tileset.tiles.items()
    }
    assert weights == {"grass_base": 0.25, "dirt_base": 0.25, "grass_dirt_corner": 8.0}

    result = runner.invoke(
        app,
        ["create-map", str(store.root), "--width", "8", "--height", "6",
         "--seed", "3", "--config", str(config_path)],
    )
    assert result.exit_code == EXIT_OK, result.stdout
    used = manifest.__class__.load(store.manifest_path).tileset.maps["overworld"]  # type: ignore[union-attr]
    assert "grass_dirt_corner" in used.tiles_used


def test_single_material_advice_distinguishes_seed_luck_from_missing_tiles(
    transition_env: tuple[Path, Path, ArtifactStore],
) -> None:
    """这套 tile 接得上别的材质，所以单材质时不能再叫用户去补过渡 tile。

    8.5 之前只有"对角矩阵"一种成因，旧提示一律说"缺过渡 tile"。现在过渡块可用了，
    仍会因为均匀抽落进平凡解 —— 那时把人指向补已经存在的东西就是指错方向。
    """
    request_path, config_path, store = transition_env
    # 示例请求给过渡块设了 weight，那正是为了避免单材质结果。这条测试要的恰好是
    # 单材质那一刻，所以自己把权重清成等权 —— 不依赖示例的默认值，否则示例一调
    # 参数这条测试就会莫名失败（本次真的发生过）。
    data = yaml.safe_load(request_path.read_text(encoding="utf-8"))
    for tile in data["tileset"]["tiles"]:
        tile["weight"] = 1.0
    request_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    create(request_path, config_path)
    # 等权下 seed 1 在这套 tile 上实测铺出全草（见 §8.7 的调查记录）。
    result = runner.invoke(
        app,
        ["create-map", str(store.root), "--width", "6", "--height", "4",
         "--seed", "1", "--config", str(config_path)],
    )
    assert result.exit_code == EXIT_OK, result.stdout
    compact = "".join(result.stdout.split())
    assert "接得上别的材质" in compact
    assert "对角矩阵" not in compact
    assert "weight" in compact
