"""用 PyTMX 独立互证 Tiled XML 导出。

这里复制 ``test_tiled_export.py`` 合成夹具的最小必要部分，而不从该模块 import：
后者会连带加载仓库自己的 GID 逆函数，模糊这条测试的独立边界。少量夹具重复换来一个
更明确的判据：GID 只交给第三方 PyTMX 解释，本文件不实现任何 GID 逆运算。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
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

pytmx = pytest.importorskip("pytmx")

TILE_SIZE = (32, 32)
TILE_IDS = ("plain_a", "plain_b", "plain_c")
# 三块 tile 按稳定的名字顺序排入 2x2 图集，右下角刻意留空。
EXPECTED_ATLAS_COORDS = {
    "plain_a": (0, 0),
    "plain_b": (1, 0),
    "plain_c": (0, 1),
}


def _noise(seed: int) -> np.ndarray:
    """同底色、不同随机种子，让邻接表非对角并真正铺出多 tile 地图。"""
    rng = np.random.default_rng(seed)
    out = np.zeros((*TILE_SIZE, 4), np.uint8)
    out[:, :, 3] = 255
    for channel, base in enumerate((90, 130, 70)):
        out[:, :, channel] = np.clip(base + rng.integers(-28, 28, TILE_SIZE), 0, 255)
    return out


@pytest.fixture
def exported_map(tmp_path: Path) -> tuple[Path, list[list[str]]]:
    """造三块同材质 tile，铺 7x4 多 tile 地图，再导出 Tiled XML。"""
    root = tmp_path / "plains"
    tiles_dir = root / "frames" / "tiles"
    tiles_dir.mkdir(parents=True)

    images: dict[str, np.ndarray] = {}
    entries: dict[str, TileEntry] = {}
    for index, tile_id in enumerate(TILE_IDS):
        image = _noise(index + 1)
        images[tile_id] = image
        path = tiles_dir / f"{tile_id}.png"
        Image.fromarray(image, mode="RGBA").save(path)
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
    source = json.loads((root / "maps" / "overworld.json").read_text("utf-8"))["rows"]
    assert len({tile_id for row in source for tile_id in row}) >= 2
    return out / "overworld.tmx", source


def _atlas_coord(tiled_map: Any, x: int, y: int) -> tuple[int, int] | None:
    """让 PyTMX 从格子 GID 找到图片矩形；这里只把像素矩形换成图集坐标。"""
    gid = tiled_map.get_tile_gid(x, y, 0)
    image = tiled_map.get_tile_image_by_gid(gid)
    if image is None or image[1] is None:
        return None
    rect = image[1]
    return rect[0] // tiled_map.tilewidth, rect[1] // tiled_map.tileheight


def _decoded_coords(tiled_map: Any) -> list[list[tuple[int, int] | None]]:
    return [
        [_atlas_coord(tiled_map, x, y) for x in range(tiled_map.width)]
        for y in range(tiled_map.height)
    ]


def _expected_coords(source: list[list[str]]) -> list[list[tuple[int, int]]]:
    return [[EXPECTED_ATLAS_COORDS[tile_id] for tile_id in row] for row in source]


def test_pytmx_loads_external_tsx_and_decodes_every_cell(
    exported_map: tuple[Path, list[list[str]]],
) -> None:
    map_path, source = exported_map
    tiled_map = pytmx.TiledMap(str(map_path))

    assert (tiled_map.width, tiled_map.height) == (7, 4)
    assert (tiled_map.tilewidth, tiled_map.tileheight) == TILE_SIZE

    assert len(tiled_map.tilesets) == 1
    tileset = tiled_map.tilesets[0]
    assert tileset.firstgid == 1
    # columns/tilecount 只存在外部 TSX 中；能读到它们证明 PyTMX 跟随了 TMX 引用。
    assert tileset.columns == 2
    assert tileset.tilecount == 4

    decoded = _decoded_coords(tiled_map)
    assert sum(len(row) for row in decoded) == 28
    assert decoded == _expected_coords(source)


def test_pytmx_detects_one_tampered_csv_gid(
    exported_map: tuple[Path, list[list[str]]],
) -> None:
    map_path, source = exported_map
    tree = ET.parse(map_path)
    data = tree.getroot().find("./layer/data")
    assert data is not None and data.text is not None
    gids = [int(value) for value in data.text.replace("\n", "").split(",") if value]
    gids[0] += 1
    data.text = ",".join(str(gid) for gid in gids)
    tree.write(map_path, encoding="utf-8", xml_declaration=True)

    tiled_map = pytmx.TiledMap(str(map_path))
    assert _atlas_coord(tiled_map, 0, 0) != EXPECTED_ATLAS_COORDS[source[0][0]]


def test_pytmx_detects_a_shifted_firstgid_across_the_map(
    exported_map: tuple[Path, list[list[str]]],
) -> None:
    map_path, source = exported_map
    tree = ET.parse(map_path)
    tileset = tree.getroot().find("tileset")
    assert tileset is not None
    tileset.set("firstgid", str(int(tileset.get("firstgid", "0")) + 1))
    tree.write(map_path, encoding="utf-8", xml_declaration=True)

    tiled_map = pytmx.TiledMap(str(map_path))
    decoded = _decoded_coords(tiled_map)
    expected = _expected_coords(source)
    mismatches = [
        (x, y)
        for y, row in enumerate(decoded)
        for x, coord in enumerate(row)
        if coord != expected[y][x]
    ]
    assert len(mismatches) == tiled_map.width * tiled_map.height
