"""最近邻缩放。

**不得引入任何中间色**（Sprint 3 退出门槛）。像素资产的每个颜色都要能落在
调色板里；双线性/双三次插值会在边缘造出成百上千个过渡色，
把 24 色的调色板直接打成上千色。

透明区的处理同样重要：先把 alpha=0 的像素 RGB 清零再缩放，
否则最近邻虽然不插值，后续的 despill 与量化统计仍会被那些不可见的 RGB 干扰。
"""

from __future__ import annotations

import numpy as np
from PIL import Image

from ..errors import ProcessingError


def nearest_resize(rgba: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """最近邻缩放到 ``(width, height)``。"""
    if rgba.ndim != 3 or rgba.shape[2] != 4:
        raise ProcessingError(f"nearest_resize 需要 RGBA，收到 {rgba.shape}")

    width, height = size
    if width < 1 or height < 1:
        raise ProcessingError(f"目标尺寸非法：{size}")

    cleaned = rgba.copy()
    cleaned[cleaned[:, :, 3] == 0] = 0

    image = Image.fromarray(cleaned, mode="RGBA")
    resized = image.resize((width, height), Image.Resampling.NEAREST)
    out = np.array(resized)
    out[out[:, :, 3] == 0] = 0
    return out


def fit_within(source: tuple[int, int], target: tuple[int, int]) -> tuple[int, int]:
    """等比缩放到能放进 ``target`` 的最大尺寸。

    等比是必须的：非等比会把角色拉扁或拉长，而这种失真在 32×32 上
    肉眼立刻可见，却能通过全部几何类验证项。
    """
    src_w, src_h = source
    dst_w, dst_h = target
    if src_w <= 0 or src_h <= 0:
        raise ProcessingError(f"源尺寸非法：{source}")

    scale = min(dst_w / src_w, dst_h / src_h)
    return (max(1, int(src_w * scale)), max(1, int(src_h * scale)))


def resize_to_fit(rgba: np.ndarray, target: tuple[int, int]) -> np.ndarray:
    """等比缩放到能放进目标画布的最大尺寸（不做居中，居中交给 anchor）。"""
    height, width = rgba.shape[:2]
    return nearest_resize(rgba, fit_within((width, height), target))


def introduces_new_colors(before: np.ndarray, after: np.ndarray) -> set[tuple[int, ...]]:
    """返回缩放后新出现的颜色。最近邻应当返回空集 —— 这是回归测试的判据。"""

    def opaque_colors(arr: np.ndarray) -> set[tuple[int, ...]]:
        opaque = arr[arr[:, :, 3] > 0]
        return {tuple(int(v) for v in px[:3]) for px in opaque}

    return opaque_colors(after) - opaque_colors(before)
