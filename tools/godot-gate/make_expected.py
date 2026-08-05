"""从 Manifest + generic-json 导出物生成 tileset 门槛的 expected.json。

**期望值必须来自产物本身**，不能手抄：手抄会把"我以为导出的是什么"验成
"导出的确实是什么"，而门槛要抓的正是这两者的差。

用法：
    python3 tools/godot-gate/make_expected.py <资产目录> > expected.json
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def _representative(corners: list[str]) -> str:
    """与交付契约相同：众数，同票按固定角顺序首个。"""
    counts = Counter(corners)
    largest = max(counts.values())
    return next(corner for corner in corners if counts[corner] == largest)


def build(asset_dir: Path) -> dict[str, object]:
    manifest = json.loads((asset_dir / "asset-manifest.json").read_text(encoding="utf-8"))
    tileset = manifest.get("tileset")
    if tileset is None:
        raise SystemExit(f"{asset_dir} 不是 tileset 资产")

    exported = asset_dir / "exports" / "generic-json" / f"{manifest['asset_id']}.json"
    if not exported.is_file():
        raise SystemExit(f"缺少 generic-json 导出：先跑 export -t generic-json（{exported}）")
    payload = json.loads(exported.read_text(encoding="utf-8"))

    width, height = tileset["tile_size"]
    maps = payload.get("maps", {})
    # 有多张地图时取名字最小的那张 —— 门槛只需要一张，但要定死是哪一张。
    rows = maps[sorted(maps)[0]]["rows"] if maps else []

    expected_terrain: dict[str, object] | None = None
    if tileset.get("terrain") is not None:
        terrain_tiles: dict[str, dict[str, object]] = {}
        eligible: list[list[str]] = []
        for tile_id, tile in sorted(tileset["tiles"].items()):
            corners = tile["terrain"]["measured_corners"]
            skipped = any(corner is None for corner in corners)
            if not skipped:
                eligible.append(corners)
            terrain_tiles[tile_id] = {
                "measured_corners": corners,
                "terrain": None if skipped else _representative(corners),
                "skipped": skipped,
            }
        names = sorted({corner for corners in eligible for corner in corners})
        expected_terrain = {
            "sets_count": 1 if names else 0,
            # Godot 4.7.1 TERRAIN_MODE_MATCH_CORNERS，探针见 probe_terrain_format.gd。
            "mode": 1,
            "names": names,
            "tiles": terrain_tiles,
        }

    return {
        "asset_id": manifest["asset_id"],
        "tile_w": width,
        "tile_h": height,
        # 图集格坐标直接取自导出物，不是重算的 —— 重算等于把同一个假设写两遍。
        "coords": {
            tile_id: [entry["column"], entry["row"]]
            for tile_id, entry in payload["tiles"].items()
        },
        "rows": rows,
        "terrain": expected_terrain,
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    json.dump(build(Path(sys.argv[1])), sys.stdout, ensure_ascii=False, indent=2)
    print()
