"""Generic JSON 导出 —— 引擎无关的交付格式。

产出一张角色总 spritesheet + 一份描述所有动作与帧区域的 JSON。
任何引擎都能靠这两个文件把动画装起来，不需要理解本项目的目录结构。

区域用**绝对像素坐标**而非行列号：行列号要求读取方知道网格布局，
绝对坐标只要求它会裁矩形。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from ..errors import ExportError
from ..models.manifest import AssetManifest
from ..processing.spritesheet import save_png
from .base import (
    AnimationView,
    Exporter,
    ExportResult,
    TileView,
    animation_views,
    load_frames,
    load_static_image,
    load_tiles,
    tile_views,
)

SCHEMA = "pixel-asset-forge.generic.v1"


def build_atlas(
    views: list[AnimationView], frames_by_key: dict[str, list[np.ndarray]]
) -> tuple[np.ndarray, dict[str, list[tuple[int, int, int, int]]]]:
    """把所有动作拼成一张总图：一行一个动作。

    一行一个动作而不是紧凑打包 —— 打包能省几个像素，但会让 JSON 里的区域
    与"第几个动作的第几帧"失去肉眼可读的对应关系，排障时很难受。
    """
    if not views:
        raise ValueError("没有可导出的动作")

    height, width = frames_by_key[views[0].key][0].shape[:2]
    cols = max(v.frame_count for v in views)
    atlas = np.zeros((height * len(views), width * cols, 4), dtype=np.uint8)

    regions: dict[str, list[tuple[int, int, int, int]]] = {}
    for row, view in enumerate(views):
        boxes = []
        for col, frame in enumerate(frames_by_key[view.key]):
            x0, y0 = col * width, row * height
            atlas[y0 : y0 + height, x0 : x0 + width] = frame
            boxes.append((x0, y0, width, height))
        regions[view.key] = boxes
    return atlas, regions


def build_tile_atlas(
    views: list[TileView], images: list[np.ndarray]
) -> tuple[np.ndarray, dict[str, tuple[int, int]], tuple[int, int]]:
    """把整套 tile 拼成一张近方形的图集，返回 ``(图集, tile→格坐标, (列, 行))``。

    近方形而不是一行排开：tileset 的图集会被引擎按固定网格切，一行 20 个 tile
    在 32px 下就是 640×32 的长条，编辑器里根本没法看。

    格坐标（列、行）而不是像素矩形：Godot 的 ``TileSetAtlasSource`` 本来就按
    ``列:行`` 寻址，给像素矩形反而要调用方自己再除一遍。
    """
    if not views:
        raise ValueError("没有可导出的 tile")
    height, width = images[0].shape[:2]
    sizes = {image.shape[:2] for image in images}
    if len(sizes) > 1:
        raise ExportError(f"tile 尺寸不一致，无法拼图集：{sorted(sizes)}")

    columns = math.ceil(math.sqrt(len(views)))
    rows = math.ceil(len(views) / columns)
    atlas = np.zeros((height * rows, width * columns, 4), dtype=np.uint8)

    coords: dict[str, tuple[int, int]] = {}
    for index, (view, image) in enumerate(zip(views, images, strict=True)):
        col, row = index % columns, index // columns
        atlas[row * height : (row + 1) * height, col * width : (col + 1) * width] = image
        coords[view.tile_id] = (col, row)
    return atlas, coords, (columns, rows)


class GenericJsonExporter(Exporter):
    target = "generic-json"

    def _export_tileset(
        self, manifest: AssetManifest, root: Path, out_dir: Path
    ) -> ExportResult:
        assert manifest.tileset is not None
        result = ExportResult(target=self.target)
        self.ensure_dir(out_dir)

        views = tile_views(manifest)
        atlas, coords, (columns, rows) = build_tile_atlas(views, load_tiles(root, views))
        texture_name = f"{manifest.asset_id}.png"
        result.files.append(save_png(atlas, out_dir / texture_name))

        width, height = manifest.tileset.tile_size
        payload: dict[str, Any] = {
            "schema": SCHEMA,
            "asset_id": manifest.asset_id,
            "asset_type": manifest.asset_type,
            "pipeline_version": manifest.pipeline_version,
            "palette": manifest.palette.colors,
            "tile_size": {"width": width, "height": height},
            "atlas": {
                "image": texture_name,
                "columns": columns,
                "rows": rows,
            },
            "tiles": {
                view.tile_id: {
                    "column": coords[view.tile_id][0],
                    "row": coords[view.tile_id][1],
                    "x": coords[view.tile_id][0] * width,
                    "y": coords[view.tile_id][1] * height,
                    "width": width,
                    "height": height,
                }
                for view in views
            },
        }

        adjacency = manifest.tileset.adjacency
        if adjacency is not None:
            # 四个方向都写出来，与 Manifest 只存两个方向**不矛盾**：Manifest 是
            # 事实的存放处，一个事实只该有一份；导出物是给下游消费的，地图生成器
            # 要按"这一格的左边能放什么"查表，让它自己转置只会让每个消费者
            # 各写一遍转置逻辑。四份由同一份事实现算而来，不会各自漂移。
            payload["adjacency"] = {
                "seam_ratio_max": adjacency.seam_ratio_max,
                "edge_color_gap_max": adjacency.edge_color_gap_max,
                "calibrated": adjacency.calibrated,
                **{
                    direction: {
                        view.tile_id: adjacency.neighbours(view.tile_id, direction)
                        for view in views
                    }
                    for direction in ("right", "down", "left", "up")
                },
            }

        path = out_dir / f"{manifest.asset_id}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        result.files.append(path)
        result.notes.append(
            f"{len(views)} 块 tile 排成 {columns}×{rows} 图集；"
            "tiles[*] 同时给了格坐标与像素坐标，按引擎习惯取其一。"
        )
        if adjacency is not None:
            result.notes.append(
                "adjacency 给出四个方向各自允许的邻居，可直接喂给地图生成器。"
                f"阈值 seam≤{adjacency.seam_ratio_max}、gap≤{adjacency.edge_color_gap_max}，"
                + ("已校准。" if adjacency.calibrated else "**未用真实 tile 校准**。")
                + "只列了基础地面 tile 之间的关系 —— 两种材质判为不相容是正确结果，"
                "说明中间还缺一类过渡 tile。"
            )
        return result

    def export(self, manifest: AssetManifest, root: Path, out_dir: Path) -> ExportResult:
        if manifest.tileset is not None:
            return self._export_tileset(manifest, root, out_dir)

        views = animation_views(manifest, root)
        result = ExportResult(target=self.target)

        self.ensure_dir(out_dir)
        frames_by_key = {v.key: load_frames(root, v) for v in views}

        payload: dict[str, Any] = {
            "schema": SCHEMA,
            "asset_id": manifest.asset_id,
            "asset_type": manifest.asset_type,
            "pipeline_version": manifest.pipeline_version,
            "canvas": {"width": manifest.canvas.width, "height": manifest.canvas.height},
            "anchor": {
                "type": manifest.anchor.type,
                "x": manifest.anchor.x,
                "y": manifest.anchor.y,
            },
            "palette": manifest.palette.colors,
            "animations": {},
        }
        if manifest.scale_profile is not None:
            payload["scale_profile"] = {
                "reference": manifest.scale_profile.reference,
                "subject_ratio": manifest.scale_profile.subject_ratio,
                "canvas_fraction": manifest.scale_profile.canvas_fraction,
            }

        if manifest.static_image is not None:
            image = load_static_image(manifest, root)
            image_path = save_png(image, out_dir / f"{manifest.asset_id}.png")
            result.files.append(image_path)
            payload["image"] = {
                "file": image_path.name,
                "width": int(image.shape[1]),
                "height": int(image.shape[0]),
            }

        if views:
            atlas, regions = build_atlas(views, frames_by_key)
            atlas_path = save_png(atlas, out_dir / f"{manifest.asset_id}.png")
            result.files.append(atlas_path)
            payload["atlas"] = {
                "image": atlas_path.name,
                "width": int(atlas.shape[1]),
                "height": int(atlas.shape[0]),
            }
            for view in views:
                payload["animations"][view.key] = {
                    "fps": view.fps,
                    "loop": view.loop,
                    "frame_count": view.frame_count,
                    "duration_seconds": round(view.duration, 4),
                    "derived_from": view.derived_from,
                    "frames": [
                        {"x": x, "y": y, "width": w, "height": h}
                        for x, y, w, h in regions[view.key]
                    ],
                }

        json_path = out_dir / f"{manifest.asset_id}.json"
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        result.files.append(json_path)
        return result
