"""用 Tiled 官方 tmxrasterizer 对导出地图做逐像素互证。

这里复制 ``test_tiled_pytmx.py`` 合成夹具的最小必要部分，不 import 那个测试模块。
期望图只按源地图的 ``tile_id`` 逐格贴源 tile；不调用项目自己的 GID 编解码函数。
TMX / TSX / 图集的解释全部交给 libtiled，二者只在最终 RGBA 像素上比较。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
import pytest
from PIL import Image

from pixel_asset_forge.exporters import get_exporter
from pixel_asset_forge.models.manifest import (
    AssetManifest,
    BackgroundInfo,
    CanvasInfo,
    PaletteInfo,
    ProviderInfo,
    TileAdjacency,
    TileEntry,
    TilesetInfo,
)
from pixel_asset_forge.pipelines.tilemap import create_map
from pixel_asset_forge.storage.hashes import hash_file
from pixel_asset_forge.validation.adjacency import derive_adjacency

TMXRASTERIZER = shutil.which("tmxrasterizer")
if TMXRASTERIZER is None:
    pytest.skip("未安装 tmxrasterizer，跳过 Tiled 官方渲染器互证", allow_module_level=True)

TILE_SIZE = (32, 32)
TILE_IDS = ("plain_a", "plain_b", "plain_c")


@dataclass(frozen=True)
class ExportedMap:
    map_path: Path
    source_rows: list[list[str]]
    tile_paths: dict[str, Path]


def _noise(seed: int) -> np.ndarray:
    """同底色、不同随机种子，让邻接表非对角并真正铺出多 tile 地图。"""
    rng = np.random.default_rng(seed)
    out = np.zeros((*TILE_SIZE, 4), np.uint8)
    out[:, :, 3] = 255
    for channel, base in enumerate((90, 130, 70)):
        out[:, :, channel] = np.clip(base + rng.integers(-28, 28, TILE_SIZE), 0, 255)
    return out


@pytest.fixture
def exported_map(tmp_path: Path) -> ExportedMap:
    """造三块同材质 tile，铺 7x4 多 tile 地图，再导出 Tiled XML。"""
    root = tmp_path / "plains"
    tiles_dir = root / "frames" / "tiles"
    tiles_dir.mkdir(parents=True)

    images: dict[str, np.ndarray] = {}
    entries: dict[str, TileEntry] = {}
    tile_paths: dict[str, Path] = {}
    for index, tile_id in enumerate(TILE_IDS):
        image = _noise(index + 1)
        images[tile_id] = image
        path = tiles_dir / f"{tile_id}.png"
        Image.fromarray(image, mode="RGBA").save(path)
        tile_paths[tile_id] = path
        relative = str(path.relative_to(root))
        entries[tile_id] = TileEntry(
            source_image=relative,
            image=relative,
            source_hash=hash_file(path),
            processed_hash=hash_file(path),
        )

    table = derive_adjacency(images)
    manifest = AssetManifest(
        asset_id="plains",
        asset_type="tileset",
        provider=ProviderInfo(name="mock", model="mock-image"),
        canvas=CanvasInfo(width=TILE_SIZE[0], height=TILE_SIZE[1]),
        background=BackgroundInfo(mode="opaque"),
        palette=PaletteInfo(max_colors=64, colors=[]),
        tileset=TilesetInfo(
            tile_size=TILE_SIZE,
            tiles=entries,
            adjacency=TileAdjacency(
                seam_ratio_max=table.seam_ratio_max,
                edge_color_gap_max=table.edge_color_gap_max,
                right=table.right,
                down=table.down,
            ),
        ),
        status="validated",
    )
    manifest.save(root / "asset-manifest.json")
    create_map(root, name="overworld", width=7, height=4, seed=20260802)

    out = root / "exports" / "tiled"
    get_exporter("tiled").export(AssetManifest.load(root / "asset-manifest.json"), root, out)
    source_rows = json.loads((root / "maps" / "overworld.json").read_text("utf-8"))["rows"]
    assert len({tile_id for row in source_rows for tile_id in row}) >= 2
    return ExportedMap(out / "overworld.tmx", source_rows, tile_paths)


def _compose_expected(exported: ExportedMap) -> Image.Image:
    """独立期望侧：只认源 tile 图和地图逐格 tile_id，不读任何 GID。"""
    width = len(exported.source_rows[0])
    expected = Image.new(
        "RGBA", (width * TILE_SIZE[0], len(exported.source_rows) * TILE_SIZE[1]), (0, 0, 0, 0)
    )
    tiles: dict[str, Image.Image] = {}
    for tile_id, path in exported.tile_paths.items():
        with Image.open(path) as source:
            tiles[tile_id] = source.convert("RGBA")
    for y, row in enumerate(exported.source_rows):
        for x, tile_id in enumerate(row):
            expected.alpha_composite(tiles[tile_id], (x * TILE_SIZE[0], y * TILE_SIZE[1]))
    return expected


def _rasterize(map_path: Path, output_path: Path) -> Image.Image:
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [TMXRASTERIZER, str(map_path), str(output_path)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert output_path.is_file()
    with Image.open(output_path) as source:
        return source.convert("RGBA")


def _pixels(image: Image.Image) -> bytes:
    return image.convert("RGBA").tobytes()


def _tamper_all_gids(map_path: Path) -> int:
    tree = ET.parse(map_path)
    data = tree.getroot().find("./layer/data")
    assert data is not None and data.get("encoding") == "csv" and data.text is not None
    gids = [int(value.strip()) for value in data.text.split(",") if value.strip()]
    data.text = "\n" + ",".join(str(gid + 1) for gid in gids) + "\n"
    tree.write(map_path, encoding="utf-8", xml_declaration=True)
    return len(gids)


def _tamper_firstgid(map_path: Path) -> tuple[int, int]:
    tree = ET.parse(map_path)
    tileset = tree.getroot().find("tileset")
    assert tileset is not None and tileset.get("firstgid") is not None
    before = int(tileset.get("firstgid", "0"))
    after = before - 1
    tileset.set("firstgid", str(after))
    tree.write(map_path, encoding="utf-8", xml_declaration=True)
    return before, after


def test_official_rasterizer_matches_the_independent_composite_pixel_for_pixel(
    exported_map: ExportedMap, tmp_path: Path
) -> None:
    expected = _compose_expected(exported_map)
    official = _rasterize(exported_map.map_path, tmp_path / "official.png")

    assert official.size == expected.size == (7 * 32, 4 * 32)
    assert _pixels(official) == _pixels(expected)


def test_official_rasterizer_exposes_all_gids_shifted_by_one(
    exported_map: ExportedMap, tmp_path: Path
) -> None:
    changed = _tamper_all_gids(exported_map.map_path)
    tampered = _rasterize(exported_map.map_path, tmp_path / "gid-plus-one.png")

    assert changed == 7 * 4
    assert tampered.size == _compose_expected(exported_map).size
    assert _pixels(tampered) != _pixels(_compose_expected(exported_map))


def test_official_rasterizer_exposes_a_shifted_firstgid(
    exported_map: ExportedMap, tmp_path: Path
) -> None:
    before, after = _tamper_firstgid(exported_map.map_path)
    tampered = _rasterize(exported_map.map_path, tmp_path / "firstgid-shift.png")

    assert (before, after) == (1, 0)
    assert tampered.size == _compose_expected(exported_map).size
    assert _pixels(tampered) != _pixels(_compose_expected(exported_map))
