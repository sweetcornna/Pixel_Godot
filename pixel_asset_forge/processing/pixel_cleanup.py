"""孤立像素清理。

下采样之后常会剩下单个悬空像素：原图里一根细线（剑尖、飘带、发丝）
缩到 32×32 时只剩一两个点，看起来像脏点而不是内容。

清理必须**保守**：宁可留下几个可疑像素，也不要削掉剑尖。
所以只删真正四面无邻的孤点，不做任何形态学开运算。
"""

from __future__ import annotations

import numpy as np
from scipy.ndimage import convolve

from ..errors import ProcessingError

#: 八邻域。像素画里对角相连也算相连 —— 45° 的线本来就是靠对角像素画出来的。
_NEIGHBOURHOOD = np.array([[1, 1, 1], [1, 0, 1], [1, 1, 1]], dtype=np.int16)


def neighbour_counts(alpha: np.ndarray) -> np.ndarray:
    return convolve((alpha > 0).astype(np.int16), _NEIGHBOURHOOD, mode="constant", cval=0)


def remove_isolated_pixels(rgba: np.ndarray, *, min_neighbours: int = 1) -> np.ndarray:
    """删除邻居数少于 ``min_neighbours`` 的不透明像素。

    默认 ``min_neighbours=1`` 只删完全孤立的点。调到 2 会开始削掉细线的端点，
    那正是剑尖所在的位置 —— 所以默认值不要随手调大。
    """
    if rgba.ndim != 3 or rgba.shape[2] != 4:
        raise ProcessingError(f"remove_isolated_pixels 需要 RGBA，收到 {rgba.shape}")

    alpha = rgba[:, :, 3]
    counts = neighbour_counts(alpha)
    isolated = (alpha > 0) & (counts < min_neighbours)

    out = rgba.copy()
    out[isolated] = 0
    return out


def cleanup_frames(frames: list[np.ndarray], *, min_neighbours: int = 1) -> list[np.ndarray]:
    return [remove_isolated_pixels(f, min_neighbours=min_neighbours) for f in frames]


def count_isolated(rgba: np.ndarray, *, min_neighbours: int = 1) -> int:
    alpha = rgba[:, :, 3]
    return int(((alpha > 0) & (neighbour_counts(alpha) < min_neighbours)).sum())
