"""Generic JSON 导出 —— 引擎无关的交付格式。

产出一张角色总 spritesheet + 一份描述所有动作与帧区域的 JSON。
任何引擎都能靠这两个文件把动画装起来，不需要理解本项目的目录结构。

区域用**绝对像素坐标**而非行列号：行列号要求读取方知道网格布局，
绝对坐标只要求它会裁矩形。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from ..models.manifest import AssetManifest
from ..processing.spritesheet import save_png
from .base import AnimationView, Exporter, ExportResult, animation_views, load_frames

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


class GenericJsonExporter(Exporter):
    target = "generic-json"

    def export(self, manifest: AssetManifest, root: Path, out_dir: Path) -> ExportResult:
        views = animation_views(manifest, root)
        result = ExportResult(target=self.target)
        if not views:
            result.notes.append("没有任何动作，只导出元数据")

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
