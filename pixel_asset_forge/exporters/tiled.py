"""Tiled 导出：`.tsx` 外部 tileset + `.tmx` 地图，以及两者的 JSON 变体。

**"能打开"不是判据。** Tiled 的地图是一串 GID（``firstgid + 行主序局部 id``，
``0`` 表示空格），而这条链上每一步都能悄悄写错、写错的文件**照样能打开**：
``firstgid`` 差 1 是整张地图错位一格，行主序写成列主序是非方形图集上全乱，
CSV 按列输出是整张地图转置 —— Tiled 一个都不会报错。

所以这里把 GID 的正反两个方向都写出来（:func:`gid_for` / :func:`tile_id_for_gid`），
判据是**往回解**：把写出去的文件读回来逐格比对源地图。见 PLAN §8.4。

编码取 ``csv`` 而不是 base64+zlib：本机没有 Tiled 可验，压缩过的字节流写错了
肉眼看不出、往回解也只能靠自己那份解码器自证。CSV 是官方支持的编码，
读一眼就知道对不对。
"""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET

from ..errors import ExportError
from ..models.manifest import AssetManifest
from ..processing.spritesheet import save_png
from .base import Exporter, ExportResult, load_tiles, tile_views
from .generic_json import build_tile_atlas

#: Tiled 的格式版本。写死而不是跟着安装的 Tiled 走 —— 我们不依赖本机有 Tiled。
TILED_VERSION = "1.10"

#: 单个 tileset 的第一个 GID。Tiled 用 0 表示空格，所以从 1 起。
FIRST_GID = 1

UNVERIFIED_NOTE = (
    "这些 Tiled 文件**尚未被 Tiled 真机打开验证过** —— 本切的保证止于"
    "「结构符合文档所述、GID 能往回解回原 tile」，不等于 Tiled 一定能正常渲染。"
    "首次导入若报错请回报（PLAN §8.4）。"
)


def gid_for(tile_id: str, coords: dict[str, tuple[int, int]], columns: int) -> int:
    """``tile_id`` → GID。

    局部 id 是图集里的**行主序**下标 ``row × columns + col`` —— 与
    :func:`..exporters.generic_json.build_tile_atlas` 摆放 tile 的顺序同源。
    再加 ``FIRST_GID``，因为 Tiled 拿 ``0`` 表示空格：漏掉这一步，
    图集第一块 tile 会变成"这里没有东西"。
    """
    col, row = coords[tile_id]
    return FIRST_GID + row * columns + col


def tile_id_for_gid(
    gid: int, coords: dict[str, tuple[int, int]], columns: int
) -> str:
    """GID → ``tile_id``。:func:`gid_for` 的逆，判据靠它。

    单独写出来而不是让测试自己算：测试自己算一遍等于把同一个假设写两遍，
    两边一起错就一起判通过。这个函数只依赖 ``coords``，与写出去的字节无关。
    """
    local = gid - FIRST_GID
    if local < 0:
        raise ExportError(f"GID {gid} 小于 firstgid {FIRST_GID} —— 空格还是算错了？")
    target = (local % columns, local // columns)
    for tile_id, position in coords.items():
        if position == target:
            return tile_id
    raise ExportError(f"GID {gid} 落在图集格 {target}，那里没有 tile")


def _tsx(
    asset_id: str, texture: str, tile_size: tuple[int, int], columns: int, rows: int
) -> str:
    """外部 tileset。

    ``tilecount`` 取 ``columns × rows`` 而不是实际 tile 数：图集不一定填满
    （3 块 tile 摆成 2×2 就空一格），而 Tiled 是**按图片尺寸自己算**格数的。
    报一个和它算出来不一样的数，只会让它当场对不上。
    """
    width, height = tile_size
    tileset = ET.Element(
        "tileset",
        version=TILED_VERSION,
        tiledversion=TILED_VERSION,
        name=asset_id,
        tilewidth=str(width),
        tileheight=str(height),
        tilecount=str(columns * rows),
        columns=str(columns),
    )
    ET.SubElement(
        tileset,
        "image",
        source=texture,
        width=str(width * columns),
        height=str(height * rows),
    )
    ET.indent(tileset, space=" ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        tileset, encoding="unicode"
    )


def _csv_data(gids: list[list[int]]) -> str:
    """逐行、左到右、上到下 —— Tiled 的 renderorder 就是 right-down。

    按列输出会让整张地图转置，而转置后的文件一样能打开。
    """
    return "\n" + ",\n".join(",".join(str(gid) for gid in row) for row in gids) + "\n"


def _tmx(
    name: str, gids: list[list[int]], tile_size: tuple[int, int], tsx_name: str
) -> str:
    width, height = tile_size
    map_el = ET.Element(
        "map",
        version=TILED_VERSION,
        tiledversion=TILED_VERSION,
        orientation="orthogonal",
        renderorder="right-down",
        width=str(len(gids[0])),
        height=str(len(gids)),
        tilewidth=str(width),
        tileheight=str(height),
        infinite="0",
        nextlayerid="2",
        nextobjectid="1",
    )
    ET.SubElement(map_el, "tileset", firstgid=str(FIRST_GID), source=tsx_name)
    layer = ET.SubElement(
        map_el,
        "layer",
        id="1",
        name=name,
        width=str(len(gids[0])),
        height=str(len(gids)),
    )
    data = ET.SubElement(layer, "data", encoding="csv")
    data.text = _csv_data(gids)
    ET.indent(map_el, space=" ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        map_el, encoding="unicode"
    )


def _tmj(
    name: str, gids: list[list[int]], tile_size: tuple[int, int], tsx_json: str
) -> dict[str, object]:
    """JSON 变体。``data`` 是**摊平**的一维数组，行接行 —— 这是 Tiled 的约定，
    不是二维数组。摊错方向同样能打开，同样全错。
    """
    width, height = tile_size
    return {
        "type": "map",
        "version": TILED_VERSION,
        "tiledversion": TILED_VERSION,
        "orientation": "orthogonal",
        "renderorder": "right-down",
        "infinite": False,
        "width": len(gids[0]),
        "height": len(gids),
        "tilewidth": width,
        "tileheight": height,
        "nextlayerid": 2,
        "nextobjectid": 1,
        "tilesets": [{"firstgid": FIRST_GID, "source": tsx_json}],
        "layers": [
            {
                "type": "tilelayer",
                "id": 1,
                "name": name,
                "x": 0,
                "y": 0,
                "width": len(gids[0]),
                "height": len(gids),
                "visible": True,
                "opacity": 1,
                "data": [gid for row in gids for gid in row],
            }
        ],
    }


def _tsj(
    asset_id: str, texture: str, tile_size: tuple[int, int], columns: int, rows: int
) -> dict[str, object]:
    width, height = tile_size
    return {
        "type": "tileset",
        "version": TILED_VERSION,
        "tiledversion": TILED_VERSION,
        "name": asset_id,
        "image": texture,
        "imagewidth": width * columns,
        "imageheight": height * rows,
        "tilewidth": width,
        "tileheight": height,
        "tilecount": columns * rows,
        "columns": columns,
        "margin": 0,
        "spacing": 0,
    }


class TiledExporter(Exporter):
    target = "tiled"

    def export(
        self, manifest: AssetManifest, root: Path, out_dir: Path
    ) -> ExportResult:
        if manifest.tileset is None:
            raise ExportError(
                f"{manifest.asset_id} 不是 tileset —— Tiled 导出只处理 tileset 与地图。"
                "角色与静态资产请用 godot 或 generic-json。"
            )

        result = ExportResult(target=self.target)
        self.ensure_dir(out_dir)

        views = tile_views(manifest)
        atlas, coords, (columns, rows) = build_tile_atlas(views, load_tiles(root, views))
        texture = f"{manifest.asset_id}.png"
        result.files.append(save_png(atlas, out_dir / texture))

        tile_size = manifest.tileset.tile_size
        tsx_name = f"{manifest.asset_id}.tsx"
        tsj_name = f"{manifest.asset_id}.tsj"
        (out_dir / tsx_name).write_text(
            _tsx(manifest.asset_id, texture, tile_size, columns, rows), encoding="utf-8"
        )
        (out_dir / tsj_name).write_text(
            json.dumps(_tsj(manifest.asset_id, texture, tile_size, columns, rows),
                       ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        result.files.extend([out_dir / tsx_name, out_dir / tsj_name])

        # 没有地图就只出 tileset —— 产一个 0×0 的空 .tmx 只会让人以为地图丢了。
        for name, entry in sorted(manifest.tileset.maps.items()):
            gids = [
                [gid_for(tile_id, coords, columns) for tile_id in row]
                for row in entry.load_rows(root)
            ]
            (out_dir / f"{name}.tmx").write_text(
                _tmx(name, gids, tile_size, tsx_name), encoding="utf-8"
            )
            (out_dir / f"{name}.tmj").write_text(
                json.dumps(_tmj(name, gids, tile_size, tsj_name),
                           ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            result.files.extend([out_dir / f"{name}.tmx", out_dir / f"{name}.tmj"])

        listing = "、".join(sorted(manifest.tileset.maps)) or "（暂无地图，只导出了 tileset）"
        result.notes.extend(
            [
                f"把 {out_dir.name}/ 整个目录复制进 Tiled 项目，打开 .tmx（或 .tmj）即可。"
                f"地图：{listing}。",
                f"图集 {columns}×{rows}，GID = {FIRST_GID} + 行主序下标；"
                f"0 表示空格。地图数据用 csv 编码，不压缩 —— 写错了肉眼就能看出来。",
                UNVERIFIED_NOTE,
            ]
        )
        return result
