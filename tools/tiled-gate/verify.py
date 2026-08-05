#!/usr/bin/env python3
"""用 Tiled 官方渲染器验证 TMX / TSX / 图集与源地图逐像素一致。

期望图只读取源 tile 图和地图里的逐格 ``tile_id``；这里刻意不 import
``pixel_asset_forge``，也不实现或调用项目自己的 GID 编解码函数。TMX 中 GID 的含义
完全交给 tmxrasterizer（libtiled）解释，两条路径最终只在 RGBA 像素上相遇。

用法：
    python3 tools/tiled-gate/verify.py <资产目录>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from PIL import Image


class GateError(RuntimeError):
    """门槛输入或官方渲染过程无法完成。"""


@dataclass(frozen=True)
class MapInputs:
    """一张地图的官方渲染输入与独立期望输入。"""

    name: str
    tmx_path: Path
    source_map_path: Path
    tile_size: tuple[int, int]
    tile_paths: dict[str, Path]


@dataclass(frozen=True)
class PixelComparison:
    """两张图逐像素比对的结果。"""

    equal: bool
    differing_pixels: int
    first_difference: tuple[int, int] | None
    expected_size: tuple[int, int]
    actual_size: tuple[int, int]


def _load_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"读不懂 JSON：{path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise GateError(f"JSON 顶层不是对象：{path}")
    return payload


def _pair(value: object, *, label: str) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2 or not all(
        isinstance(item, int) for item in value
    ):
        raise GateError(f"{label} 不是两个整数：{value!r}")
    return value[0], value[1]


def _resolve_inputs(asset_root: Path, map_name: str | None) -> tuple[Path, list[MapInputs]]:
    manifest_path = asset_root / "asset-manifest.json"
    manifest = _load_object(manifest_path)
    tileset = manifest.get("tileset")
    if not isinstance(tileset, dict):
        raise GateError(f"{manifest_path} 里没有 tileset")

    tile_size = _pair(tileset.get("tile_size"), label="tileset.tile_size")
    tiles = tileset.get("tiles")
    if not isinstance(tiles, dict) or not tiles:
        raise GateError(f"{manifest_path} 里没有 tileset.tiles")
    tile_paths: dict[str, Path] = {}
    for tile_id, entry in tiles.items():
        if not isinstance(tile_id, str) or not isinstance(entry, dict):
            raise GateError("tileset.tiles 的 tile_id 或记录类型不合法")
        image = entry.get("image")
        if not isinstance(image, str):
            raise GateError(f"tile {tile_id} 没有 image 路径")
        path = asset_root / image
        if not path.is_file():
            raise GateError(f"tile {tile_id} 的源图不存在：{path}")
        tile_paths[tile_id] = path

    maps = tileset.get("maps")
    if not isinstance(maps, dict) or not maps:
        raise GateError(f"{manifest_path} 里没有地图；先运行 create-map")
    if map_name is not None:
        if map_name not in maps:
            raise GateError(f"地图 {map_name!r} 不在 Manifest 中；可选：{sorted(maps)}")
        selected = [(map_name, maps[map_name])]
    else:
        selected = sorted(maps.items())

    export_dir = asset_root / "exports" / "tiled"
    inputs: list[MapInputs] = []
    for name, entry in selected:
        if not isinstance(name, str) or not isinstance(entry, dict):
            raise GateError("tileset.maps 的地图名或记录类型不合法")
        source_path = entry.get("path")
        if not isinstance(source_path, str):
            raise GateError(f"地图 {name} 没有源记录路径")
        tmx_path = export_dir / f"{name}.tmx"
        if not tmx_path.is_file():
            raise GateError(f"缺少导出的 TMX：{tmx_path}")
        _check_export_chain(tmx_path, export_dir)
        inputs.append(
            MapInputs(
                name=name,
                tmx_path=tmx_path,
                source_map_path=asset_root / source_path,
                tile_size=tile_size,
                tile_paths=tile_paths,
            )
        )
    return export_dir, inputs


def _check_export_chain(tmx_path: Path, export_dir: Path) -> None:
    """确认交给官方渲染器的是同目录内实际导出的 TMX / TSX / 图集。"""
    try:
        root = ET.parse(tmx_path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise GateError(f"读不懂 TMX：{tmx_path}（{exc}）") from exc
    tileset = root.find("tileset")
    source = tileset.get("source") if tileset is not None else None
    if not source:
        raise GateError(f"{tmx_path} 没有外部 TSX 引用")
    tsx_path = (tmx_path.parent / source).resolve()
    if tsx_path.parent != export_dir.resolve() or not tsx_path.is_file():
        raise GateError(f"TMX 引用的 TSX 不在导出目录或不存在：{tsx_path}")
    try:
        tsx_root = ET.parse(tsx_path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise GateError(f"读不懂 TSX：{tsx_path}（{exc}）") from exc
    image = tsx_root.find("image")
    atlas_source = image.get("source") if image is not None else None
    if not atlas_source:
        raise GateError(f"{tsx_path} 没有图集引用")
    atlas_path = (tsx_path.parent / atlas_source).resolve()
    if atlas_path.parent != export_dir.resolve() or not atlas_path.is_file():
        raise GateError(f"TSX 引用的图集不在导出目录或不存在：{atlas_path}")


def _compose_expected(inputs: MapInputs) -> Image.Image:
    """只按源地图的 tile_id 逐格贴源 tile，不看 TMX、TSX、图集或 GID。"""
    payload = _load_object(inputs.source_map_path)
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise GateError(f"{inputs.source_map_path} 里没有非空 rows")
    width = len(rows[0]) if isinstance(rows[0], list) else 0
    if width == 0 or any(not isinstance(row, list) or len(row) != width for row in rows):
        raise GateError(f"{inputs.source_map_path} 的 rows 不是等宽非空网格")

    tile_width, tile_height = inputs.tile_size
    expected = Image.new("RGBA", (width * tile_width, len(rows) * tile_height), (0, 0, 0, 0))
    loaded: dict[str, Image.Image] = {}
    for tile_id, path in inputs.tile_paths.items():
        with Image.open(path) as source:
            tile = source.convert("RGBA")
        if tile.size != inputs.tile_size:
            raise GateError(f"源 tile {tile_id} 尺寸 {tile.size}，期望 {inputs.tile_size}")
        loaded[tile_id] = tile

    for y, row in enumerate(rows):
        for x, tile_id in enumerate(row):
            if not isinstance(tile_id, str) or tile_id not in loaded:
                raise GateError(f"地图 ({x}, {y}) 引用了未知 tile：{tile_id!r}")
            expected.alpha_composite(loaded[tile_id], (x * tile_width, y * tile_height))
    return expected


def _render(rasterizer: Path, tmx_path: Path, output_path: Path) -> None:
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [str(rasterizer), str(tmx_path), str(output_path)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise GateError(
            f"tmxrasterizer 渲染失败（退出码 {result.returncode}）：{tmx_path}\n{details}"
        )
    if not output_path.is_file():
        raise GateError(f"tmxrasterizer 返回成功但没有产出 PNG：{output_path}")


def _compare(expected: Image.Image, actual_path: Path) -> PixelComparison:
    with Image.open(actual_path) as source:
        actual = source.convert("RGBA")
    expected_rgba = expected.convert("RGBA")
    if actual.size != expected_rgba.size:
        return PixelComparison(False, -1, None, expected_rgba.size, actual.size)

    differing = 0
    first: tuple[int, int] | None = None
    width, height = expected_rgba.size
    # Pillow 的 load() 标成 PixelAccess | None。实践中不会是 None，但既然进了
    # mypy 检查范围，就把它判掉而不是压掉 —— 真为 None 时下面会静默按 0 个差异
    # 通过，那正是这个门槛最不该出现的失败形态。
    expected_pixels = expected_rgba.load()
    actual_pixels = actual.load()
    if expected_pixels is None or actual_pixels is None:
        raise GateError("GATE-FAIL 无法读取像素数据（Pillow load() 返回 None）")
    for y in range(height):
        for x in range(width):
            if expected_pixels[x, y] != actual_pixels[x, y]:
                differing += 1
                if first is None:
                    first = x, y
    return PixelComparison(differing == 0, differing, first, expected_rgba.size, actual.size)


def _describe(comparison: PixelComparison) -> str:
    if comparison.expected_size != comparison.actual_size:
        return f"尺寸不同：期望 {comparison.expected_size}，官方渲染 {comparison.actual_size}"
    if comparison.equal:
        width, height = comparison.expected_size
        return f"{width * height} 个 RGBA 像素完全相等"
    return (
        f"{comparison.differing_pixels} 个像素不同，首个差异在 {comparison.first_difference}"
    )


def _tamper_all_gids(path: Path) -> int:
    tree = ET.parse(path)
    changed = 0
    for data in tree.getroot().findall("./layer/data"):
        if data.get("encoding") != "csv" or data.text is None:
            raise GateError(f"{path} 的 tile layer 不是 CSV，无法构造 GID +1 反例")
        gids = [int(value.strip()) for value in data.text.split(",") if value.strip()]
        changed += len(gids)
        data.text = "\n" + ",".join(str(gid + 1) for gid in gids) + "\n"
    if changed == 0:
        raise GateError(f"{path} 没有可篡改的 GID")
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return changed


def _tamper_firstgid(path: Path) -> tuple[int, int]:
    tree = ET.parse(path)
    tileset = tree.getroot().find("tileset")
    if tileset is None or tileset.get("firstgid") is None:
        raise GateError(f"{path} 没有 firstgid")
    before = int(tileset.get("firstgid", "0"))
    after = before - 1
    tileset.set("firstgid", str(after))
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return before, after


def _run_map(
    rasterizer: Path, export_dir: Path, inputs: MapInputs, work_dir: Path
) -> list[str]:
    messages: list[str] = []
    expected = _compose_expected(inputs)
    expected_path = work_dir / f"{inputs.name}-expected.png"
    expected.save(expected_path)

    official_path = work_dir / f"{inputs.name}-official.png"
    _render(rasterizer, inputs.tmx_path, official_path)
    baseline = _compare(expected, official_path)
    if not baseline.equal:
        raise GateError(f"GATE-FAIL {inputs.name} 基线逐像素不一致：{_describe(baseline)}")
    messages.append(f"GATE-PASS {inputs.name} 基线：{_describe(baseline)}")

    gid_dir = work_dir / f"{inputs.name}-gid-plus-one"
    shutil.copytree(export_dir, gid_dir)
    gid_tmx = gid_dir / inputs.tmx_path.relative_to(export_dir)
    changed = _tamper_all_gids(gid_tmx)
    gid_output = work_dir / f"{inputs.name}-gid-plus-one.png"
    _render(rasterizer, gid_tmx, gid_output)
    gid_comparison = _compare(expected, gid_output)
    if gid_comparison.equal:
        raise GateError("GATE-FAIL 反例失效：地图层所有 GID +1 后仍与期望图完全相等")
    messages.append(
        f"GATE-FAIL（符合预期）GID 全体 +1：改动 {changed} 格；"
        f"{_describe(gid_comparison)}"
    )

    firstgid_dir = work_dir / f"{inputs.name}-firstgid-shift"
    shutil.copytree(export_dir, firstgid_dir)
    firstgid_tmx = firstgid_dir / inputs.tmx_path.relative_to(export_dir)
    before, after = _tamper_firstgid(firstgid_tmx)
    firstgid_output = work_dir / f"{inputs.name}-firstgid-shift.png"
    _render(rasterizer, firstgid_tmx, firstgid_output)
    firstgid_comparison = _compare(expected, firstgid_output)
    if firstgid_comparison.equal:
        raise GateError("GATE-FAIL 反例失效：firstgid 偏移后仍与期望图完全相等")
    messages.append(
        f"GATE-FAIL（符合预期）firstgid {before} -> {after}："
        f"{_describe(firstgid_comparison)}"
    )
    return messages


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="用 tmxrasterizer 与独立合成期望图逐像素验证 Tiled 导出"
    )
    parser.add_argument(
        "asset_dir", type=Path, help="含 Manifest、maps/ 与 exports/tiled/ 的资产目录"
    )
    parser.add_argument(
        "--map", dest="map_name", help="只验证指定地图；默认验证 Manifest 中全部地图"
    )
    parser.add_argument(
        "--tmxrasterizer",
        type=Path,
        help="tmxrasterizer 路径；默认从 PATH 查找",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    rasterizer = args.tmxrasterizer or shutil.which("tmxrasterizer")
    if rasterizer is None:
        print("GATE-ERROR 找不到 tmxrasterizer；请先安装 Tiled", file=sys.stderr)
        return 2
    rasterizer_path = Path(rasterizer).resolve()
    if not rasterizer_path.is_file():
        print(f"GATE-ERROR tmxrasterizer 不存在：{rasterizer_path}", file=sys.stderr)
        return 2

    try:
        asset_root = args.asset_dir.resolve()
        export_dir, maps = _resolve_inputs(asset_root, args.map_name)
        with tempfile.TemporaryDirectory(prefix="pixel-asset-tiled-gate-") as temporary:
            work_dir = Path(temporary)
            messages = [
                f"GATE 使用官方渲染器：{rasterizer_path}",
                "GATE 期望侧：源 tile 图 + 地图逐格 tile_id（不读取 GID）",
            ]
            for inputs in maps:
                messages.extend(_run_map(rasterizer_path, export_dir, inputs, work_dir))
        for message in messages:
            print(message)
        print(f"GATE-OK {len(maps)} 张地图基线通过，两条篡改反例均被逐像素比对抓出")
        return 0
    except (GateError, OSError, ValueError, ET.ParseError) as exc:
        print(f"GATE-ERROR {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
