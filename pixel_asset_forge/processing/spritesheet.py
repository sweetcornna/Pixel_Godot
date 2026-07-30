"""Spritesheet 重组与预览。

Spritesheet 的布局必须**可由 Manifest 完整重建**（ADR-001）：
帧数、每帧尺寸、列数三者确定后，第 N 帧的位置就是纯计算，不需要额外元数据。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from ..errors import ProcessingError


@dataclass(frozen=True, slots=True)
class SheetLayout:
    frame_width: int
    frame_height: int
    cols: int
    rows: int
    count: int

    def frame_box(self, index: int) -> tuple[int, int, int, int]:
        if not 0 <= index < self.count:
            raise ProcessingError(f"帧下标 {index} 超出范围 0..{self.count - 1}")
        col, row = index % self.cols, index // self.cols
        return (
            col * self.frame_width,
            row * self.frame_height,
            (col + 1) * self.frame_width,
            (row + 1) * self.frame_height,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "frame_width": self.frame_width,
            "frame_height": self.frame_height,
            "cols": self.cols,
            "rows": self.rows,
            "count": self.count,
        }


def compose_spritesheet(
    frames: list[np.ndarray], *, cols: int | None = None
) -> tuple[np.ndarray, SheetLayout]:
    """把帧序列拼成一张 spritesheet。

    默认单行排列 —— 引擎侧读取最简单，且不会因为换行导致帧序歧义。
    """
    if not frames:
        raise ProcessingError("帧列表为空")

    sizes = {f.shape[:2] for f in frames}
    if len(sizes) > 1:
        raise ProcessingError(f"spritesheet 要求所有帧尺寸一致，收到 {sorted(sizes)}")

    height, width = frames[0].shape[:2]
    count = len(frames)
    columns = cols or count
    rows = (count + columns - 1) // columns

    sheet = np.zeros((rows * height, columns * width, 4), dtype=np.uint8)
    layout = SheetLayout(width, height, columns, rows, count)

    for index, frame in enumerate(frames):
        x0, y0, x1, y1 = layout.frame_box(index)
        sheet[y0:y1, x0:x1] = frame

    return sheet, layout


def save_png(rgba: np.ndarray, path: str | Path) -> Path:
    """写 RGBA PNG。写盘前再确认一次透明像素的 RGB 为零。"""
    if rgba.ndim != 3 or rgba.shape[2] != 4:
        raise ProcessingError(f"save_png 需要 RGBA，收到 {rgba.shape}")

    cleaned = rgba.copy()
    cleaned[cleaned[:, :, 3] == 0] = 0

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(cleaned, mode="RGBA").save(target, format="PNG", optimize=True)
    return target


def save_frames(frames: list[np.ndarray], directory: str | Path, *, stem: str) -> list[Path]:
    """逐帧写盘，文件名形如 ``walk_down_00.png``。"""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    return [save_png(f, target / f"{stem}_{i:02d}.png") for i, f in enumerate(frames)]


def save_gif(
    frames: list[np.ndarray], path: str | Path, *, fps: int = 10, loop: bool = True
) -> Path:
    """写动画预览 GIF。

    GIF 只支持 1 bit 透明度，因此**仅作预览**，绝不是交付格式 ——
    交付走逐帧 PNG 与 spritesheet。
    """
    if not frames:
        raise ProcessingError("帧列表为空")

    images = [Image.fromarray(f, mode="RGBA").convert("P", palette=Image.Palette.ADAPTIVE)
              for f in frames]
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(
        target,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=max(1, round(1000 / max(1, fps))),
        loop=0 if loop else 1,
        disposal=2,
        transparency=0,
    )
    return target


def contact_sheet(
    groups: dict[str, list[np.ndarray]], *, background: tuple[int, int, int] = (32, 32, 40)
) -> np.ndarray:
    """把多个动作拼成一张总览图，供一次性人工审核（PLAN §11）。

    人工闸门看的就是这张图 —— 一屏看完所有动作，比逐个点开文件快得多。
    """
    if not groups:
        raise ProcessingError("没有可供拼图的动作")

    frame_h = max(f.shape[0] for frames in groups.values() for f in frames)
    frame_w = max(f.shape[1] for frames in groups.values() for f in frames)
    cols = max(len(frames) for frames in groups.values())
    rows = len(groups)

    sheet = np.zeros((rows * frame_h, cols * frame_w, 4), dtype=np.uint8)
    sheet[:, :, :3] = background
    sheet[:, :, 3] = 255

    for row, frames in enumerate(groups.values()):
        for col, frame in enumerate(frames):
            h, w = frame.shape[:2]
            y0 = row * frame_h + (frame_h - h) // 2
            x0 = col * frame_w + (frame_w - w) // 2
            region = sheet[y0 : y0 + h, x0 : x0 + w]
            opaque = frame[:, :, 3] > 0
            region[opaque] = frame[opaque]

    return sheet
