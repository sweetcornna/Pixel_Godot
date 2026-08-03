"""Tiled 导出：TSX / TMX / TSJ / TMJ（PLAN §8.4）。

**"能打开"不是判据。** Tiled 的地图是一串 GID（``firstgid + 行主序局部 id``，
``0`` 表示空格），而这条链上每一步都能悄悄写错、写错的文件照样能打开：
``firstgid`` 差 1 是整张地图错位一格，行主序写成列主序是非方形图集上全乱，
CSV 按列输出是整张地图转置。所以判据只能是**往回解**。

**而往回解要有判别力，地图必须至少用到两种 tile，且图集不能是方阵。**
单一材质的地图上四种错误全都能解回同一块 tile —— 又是一条永真的检查。
而 `grass_field` 的邻接表是对角矩阵，铺出来正是单色地图。

所以这里自己造一套 tileset：三块**同材质、不同实例**的噪点 tile（同底色、
不同随机种子），彼此判为相容 → 邻接表不是对角阵 → 地图真的用上多种 tile。
合成输入做反例是本仓库的既定做法（§8.1 的渐变 / 边框 / 暗角三张反例同理）。
"""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
import pytest
from PIL import Image

from pixel_asset_forge.exporters import get_exporter
from pixel_asset_forge.exporters.base import tile_views
from pixel_asset_forge.exporters.generic_json import build_tile_atlas, load_tiles
from pixel_asset_forge.exporters.tiled import (
    FIRST_GID,
    gid_for,
    tile_id_for_gid,
)
from pixel_asset_forge.models.manifest import (
    AssetManifest,
    BackgroundInfo,
    CanvasInfo,
    PaletteInfo,
    ProviderInfo,
    TileAdjacency,
    TileEntry,
    TileMapEntry,
    TilesetInfo,
)
from pixel_asset_forge.pipelines.tilemap import create_map
from pixel_asset_forge.storage.hashes import hash_file
from pixel_asset_forge.validation.adjacency import derive_adjacency

TILE_SIZE = (32, 32)
#: 三块 tile 摆成 2×2 图集 —— **刻意不是方阵**（有一格是空的），
#: 这样行主序写成列主序才会被抓到。
TILE_IDS = ("plain_a", "plain_b", "plain_c")


def _noise(seed: int) -> np.ndarray:
    """同底色、不同随机种子 —— 三块互相判为相容的同材质 tile。"""
    rng = np.random.default_rng(seed)
    out = np.zeros((*TILE_SIZE, 4), np.uint8)
    out[:, :, 3] = 255
    for channel, base in enumerate((90, 130, 70)):
        out[:, :, channel] = np.clip(base + rng.integers(-28, 28, TILE_SIZE), 0, 255)
    return out


@pytest.fixture
def synthetic_tileset(tmp_path: Path) -> Path:
    """一套手工造的 tileset 资产目录，带一张多 tile 地图。

    绕过生成链是刻意的：mock 给每块 tile 一个随机底色，推出来的邻接表永远是
    对角阵，铺不出多 tile 地图 —— 那样这个文件里的判据全部恒真。
    """
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
            source_image=relative, image=relative,
            source_hash=hash_file(path), processed_hash=hash_file(path),
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
                right=table.right, down=table.down,
            ),
        ),
        status="validated",
    )
    manifest.save(root / "asset-manifest.json")
    return root


@pytest.fixture
def with_map(synthetic_tileset: Path) -> Path:
    """铺一张 7×4 的地图。**非正方形**，转置错误才会被尺寸抓到。"""
    create_map(synthetic_tileset, name="overworld", width=7, height=4, seed=20260802)
    return synthetic_tileset


def _atlas(root: Path) -> tuple[dict[str, tuple[int, int]], int]:
    manifest = AssetManifest.load(root / "asset-manifest.json")
    views = tile_views(manifest)
    _atlas_image, coords, (columns, _rows) = build_tile_atlas(
        views, load_tiles(root, views)
    )
    return coords, columns


def _export(root: Path) -> Path:
    out = root / "exports" / "tiled"
    get_exporter("tiled").export(
        AssetManifest.load(root / "asset-manifest.json"), root, out
    )
    return out


# -- 前提：夹具本身必须真的产出多 tile 地图 --------------------------------


def test_the_fixture_actually_produces_a_multi_tile_map(with_map: Path) -> None:
    """否则本文件的每一条往回解断言都是恒真的 —— 前提失效必须当场发现。"""
    manifest = AssetManifest.load(with_map / "asset-manifest.json")
    assert manifest.tileset is not None
    assert len(manifest.tileset.maps["overworld"].tiles_used) >= 2

    _coords, columns = _atlas(with_map)
    assert columns * columns != len(TILE_IDS), "图集是方阵，列/行主序搞反了抓不到"


# -- 核心判据：往回解 -------------------------------------------------------


def _decode_tmx(path: Path, coords, columns):  # type: ignore[no-untyped-def]
    root = ET.fromstring(path.read_text(encoding="utf-8"))
    layer = root.find("layer")
    assert layer is not None
    data = layer.find("data")
    assert data is not None and data.get("encoding") == "csv"
    rows = [
        [tile_id_for_gid(int(cell), coords, columns) for cell in line.split(",") if cell.strip()]
        for line in (data.text or "").strip().splitlines()
    ]
    return rows, (int(root.get("width", "0")), int(root.get("height", "0")))


def _decode_tmj(path: Path, coords, columns):  # type: ignore[no-untyped-def]
    payload = json.loads(path.read_text(encoding="utf-8"))
    layer = payload["layers"][0]
    width, height = layer["width"], layer["height"]
    flat = [tile_id_for_gid(gid, coords, columns) for gid in layer["data"]]
    rows = [flat[y * width : (y + 1) * width] for y in range(height)]
    return rows, (payload["width"], payload["height"])


@pytest.mark.parametrize("decode", [_decode_tmx, _decode_tmj])
def test_the_exported_map_decodes_back_to_the_source_cell_for_cell(
    with_map: Path, decode
) -> None:  # type: ignore[no-untyped-def]
    """写出去再读回来，逐格必须等于源地图。四种典型写错法都在这一步露馅。"""
    out = _export(with_map)
    coords, columns = _atlas(with_map)
    source = json.loads((with_map / "maps" / "overworld.json").read_text("utf-8"))["rows"]

    suffix = "tmx" if decode is _decode_tmx else "tmj"
    rows, size = decode(out / f"overworld.{suffix}", coords, columns)
    assert size == (7, 4)
    assert rows == source


# -- 反例：证明往回解**抓得住**那两种错 --------------------------------------
#
# 只验"正确的能解回来"没有判别力：一个恒等的解码器也能通过。必须证明错误的
# 映射会解出不同的结果 —— 否则这条判据可能只是在自说自话。


def _decoded_or_none(gid: int, coords, columns) -> str | None:  # type: ignore[no-untyped-def]
    """解不出来（落在图集空格上）返回 None —— 那同样是"被抓住"的一种形态。"""
    from pixel_asset_forge.errors import ExportError

    try:
        return tile_id_for_gid(gid, coords, columns)
    except ExportError:
        return None


def test_an_off_by_one_firstgid_decodes_to_a_different_map(with_map: Path) -> None:
    """firstgid 差 1 → 整张地图错位一格。文件照样能打开，内容全错。

    断言的是**没有任何一格**能悄悄解回原样：偏移后的 GID 要么解出别的 tile，
    要么落在图集空格上直接抛错。只要有一格能解回去，这条判据在那一格上就是瞎的。
    """
    coords, columns = _atlas(with_map)
    source = json.loads((with_map / "maps" / "overworld.json").read_text("utf-8"))["rows"]

    survivors = [
        tile_id
        for row in source
        for tile_id in row
        if _decoded_or_none(gid_for(tile_id, coords, columns) + 1, coords, columns)
        == tile_id
    ]
    assert not survivors, f"这些格子在 firstgid 偏移后仍解回原样：{set(survivors)}"


def test_column_major_ids_decode_to_a_different_map(with_map: Path) -> None:
    """行主序写成列主序 → 非方形图集上 tile 全乱。"""
    _export(with_map)
    coords, columns = _atlas(with_map)
    _manifest = AssetManifest.load(with_map / "asset-manifest.json")
    rows_count = max(row for _col, row in coords.values()) + 1

    source = json.loads((with_map / "maps" / "overworld.json").read_text("utf-8"))["rows"]
    # 列主序：local = col * rows + row，而解码器按行主序解
    wrong = [
        [FIRST_GID + coords[t][0] * rows_count + coords[t][1] for t in row]
        for row in source
    ]
    decoded = [
        [tile_id_for_gid(gid, coords, columns) for gid in row] for row in wrong
    ]
    assert decoded != source


# -- 结构 ------------------------------------------------------------------


def test_the_tsx_matches_the_manifest(with_map: Path) -> None:
    out = _export(with_map)
    tileset = ET.fromstring((out / "plains.tsx").read_text(encoding="utf-8"))
    _coords, columns = _atlas(with_map)

    assert tileset.get("tilewidth") == "32"
    assert tileset.get("tileheight") == "32"
    assert tileset.get("columns") == str(columns)
    image = tileset.find("image")
    assert image is not None
    assert image.get("source") == "plains.png"
    # 图集不一定填满：3 块 tile 摆成 2×2 空一格。tilecount 报的是图片能切出的格数，
    # 因为 Tiled 就是按图片尺寸自己算的 —— 报别的数只会当场对不上。
    assert int(tileset.get("tilecount", "0")) == columns * (
        int(image.get("height", "0")) // 32
    )


def test_a_tileset_without_maps_exports_no_tmx(synthetic_tileset: Path) -> None:
    """产一个 0×0 的空 .tmx 只会让人以为地图丢了。"""
    out = _export(synthetic_tileset)
    assert (out / "plains.tsx").is_file()
    assert not list(out.glob("*.tmx"))
    assert not list(out.glob("*.tmj"))


def test_the_export_says_it_was_never_opened_in_tiled(with_map: Path) -> None:
    """本机没有 Tiled 也没有 TMX 解析库 —— 保证止于结构与往回解，要如实说。"""
    manifest = AssetManifest.load(with_map / "asset-manifest.json")
    result = get_exporter("tiled").export(
        manifest, with_map, with_map / "exports" / "tiled"
    )
    assert any("尚未被 Tiled 真机打开验证" in note for note in result.notes)


def test_tiled_is_no_longer_a_dangling_export_target() -> None:
    """它此前在 ExportTarget 里占了一格却被 get_exporter 明确拒收。"""
    from pixel_asset_forge.models.request import ExportTarget

    assert "tiled" in ExportTarget.__args__  # type: ignore[attr-defined]
    assert get_exporter("tiled").target == "tiled"


def test_a_non_tileset_asset_is_refused(tmp_path: Path) -> None:
    """角色资产走 Tiled 导出没有意义，报错好过产出一堆空文件。"""
    from pixel_asset_forge.errors import ExportError

    manifest = AssetManifest(
        asset_id="knight_01",
        asset_type="character",
        provider=ProviderInfo(name="mock", model="mock-image"),
        canvas=CanvasInfo(width=96, height=96),
        background=BackgroundInfo(
            mode="chroma_key",
            color_requested="#FF00FF",
            color_used="#FF00FF",
            fallback_stage="tolerant_key",
        ),
        palette=PaletteInfo(max_colors=24, colors=[]),
        status="validated",
    )
    with pytest.raises(ExportError, match="不是 tileset"):
        get_exporter("tiled").export(manifest, tmp_path, tmp_path / "out")


def test_unknown_targets_no_longer_blame_sprint_8() -> None:
    from pixel_asset_forge.errors import ExportError

    with pytest.raises(ExportError) as excinfo:
        get_exporter("unity")
    assert "tiled" in str(excinfo.value)  # 现在它是可选项之一
    assert "Sprint 8" not in str(excinfo.value)


def test_map_entry_hash_still_matches_after_export(with_map: Path) -> None:
    """导出不该动源地图 —— 它是产物的输入，不是产物。"""
    manifest = AssetManifest.load(with_map / "asset-manifest.json")
    assert manifest.tileset is not None
    entry: TileMapEntry = manifest.tileset.maps["overworld"]
    _export(with_map)
    assert hash_file(with_map / entry.path) == entry.hash
