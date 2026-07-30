"""内容边界检测与裁剪。

裁到内容边界之后再缩放，是 [ADR-003](../../docs/adr/ADR-003-fixed-grid.md) 里
那条"整数倍下采样"论证被推翻后的替代路径：单元格实际尺寸不可预知
（444×444 / 384×512 都出现过），但**裁剪后的内容尺寸本来就是任意的**，
所以这条路不依赖已被推翻的前提。

关键约束：**整组帧必须用同一个裁剪框**。逐帧各裁各的会让角色在帧间跳动 ——
每帧的内容边界不同，单独裁剪等于给每帧施加了不同的平移。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..errors import ProcessingError


@dataclass(frozen=True, slots=True)
class ContentBox:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    def padded(self, amount: int, limit: tuple[int, int]) -> ContentBox:
        width, height = limit
        return ContentBox(
            max(0, self.left - amount),
            max(0, self.top - amount),
            min(width, self.right + amount),
            min(height, self.bottom + amount),
        )

    def union(self, other: ContentBox) -> ContentBox:
        return ContentBox(
            min(self.left, other.left),
            min(self.top, other.top),
            max(self.right, other.right),
            max(self.bottom, other.bottom),
        )


def content_bounds(rgba: np.ndarray) -> ContentBox:
    """按 alpha 通道求内容边界（右下为开区间）。"""
    if rgba.ndim != 3 or rgba.shape[2] != 4:
        raise ProcessingError(f"content_bounds 需要 RGBA，收到 {rgba.shape}")

    ys, xs = np.nonzero(rgba[:, :, 3])
    if xs.size == 0:
        return ContentBox(0, 0, 0, 0)
    return ContentBox(int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)


def union_bounds(frames: list[np.ndarray]) -> ContentBox:
    """整组帧的公共裁剪框。

    这是"角色不在帧间跳动"的保证：所有帧共用一个框，帧间的相对位移
    就完全由角色自身的动作决定，而不是由裁剪引入。
    """
    boxes = [content_bounds(f) for f in frames]
    non_empty = [b for b in boxes if not b.empty]
    if not non_empty:
        raise ProcessingError("整组帧都是空白，无法确定裁剪框")

    result = non_empty[0]
    for box in non_empty[1:]:
        result = result.union(box)
    return result


def crop(rgba: np.ndarray, box: ContentBox) -> np.ndarray:
    if box.empty:
        raise ProcessingError("裁剪框为空")
    return np.ascontiguousarray(rgba[box.top : box.bottom, box.left : box.right])


def crop_all(frames: list[np.ndarray], *, padding: int = 0) -> tuple[list[np.ndarray], ContentBox]:
    """用公共裁剪框裁剪整组帧。返回 ``(帧列表, 实际使用的框)``。"""
    if not frames:
        raise ProcessingError("帧列表为空")

    height, width = frames[0].shape[:2]
    box = union_bounds(frames)
    if padding:
        box = box.padded(padding, (width, height))
    return [crop(f, box) for f in frames], box
