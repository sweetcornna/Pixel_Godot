"""Despill —— 去掉紧邻背景的前景像素上的键控色溢色（ADR-004）。

为什么这一步不能省：像素画的调色板通常只有 16–24 色，
角色轮廓上一圈洋红边会直接污染量化结果，表现为"莫名其妙多出几个紫色"。

**必须在量化之前执行。** 顺序反了就白做 —— 量化会把溢色固化成调色板里的一档。

实测溢色像素约占全图 0.76%（Sprint 0 / A-5），绝对量不大，
但它们全部集中在轮廓线上，正是视觉上最敏感的位置。
"""

from __future__ import annotations

import numpy as np

from ..logging_utils import get_logger

logger = get_logger("processing.despill")


def spill_amount(rgb: np.ndarray, key: tuple[int, int, int]) -> np.ndarray:
    """每个像素上的键控色溢出量。

    判据是**键控色的所有满值通道必须同时高于参考通道**，取其中最小的超出量。
    这一条是整个模块的关键，写错了会毁掉整张图的配色：

    洋红 ``#FF00FF`` 的满值通道是 R 与 B，参考通道是 G。
    若只要求"R 高于 G"就判为溢色，褐色 ``(139, 90, 43)`` 会被判成溢色并压成
    ``(90, 90, 43)`` 橄榄绿 —— 皮甲、皮肤、木头、红色全部报废。
    而褐色的 B=43 远低于 G=90，取 ``min(R, B) - G`` 就是负数，正确地判为无溢色。

    真正的洋红边 ``(200, 50, 200)`` 则得到 ``min(200,200) - 50 = 150``，被正确识别。
    """
    key_arr = np.asarray(key, dtype=np.int16)
    high, low = key_arr >= 128, key_arr < 128
    if not high.any() or not low.any():
        return np.zeros(rgb.shape[:2], dtype=np.int16)

    signed = rgb.astype(np.int16)
    reference = signed[:, :, low].max(axis=-1)
    excess = signed[:, :, high].min(axis=-1) - reference
    return np.asarray(np.maximum(excess, 0))


def despill(
    rgba: np.ndarray,
    key: tuple[int, int, int],
    *,
    strength: float = 1.0,
    edge_only: bool = True,
    edge_width: int = 2,
) -> np.ndarray:
    """抑制前景像素中的键控色溢出。

    两道保险，缺一不可：

    1. :func:`spill_amount` 只在**所有满值通道同时超标**时判为溢色 ——
       保护褐色、肤色、红色这些"只有一个通道高"的正常颜色。
    2. ``edge_only`` 只处理紧邻透明区的像素带。溢色本来就只发生在轮廓上，
       对内部像素动手没有收益，只有风险（角色身上真有洋红配饰时会被削掉）。

    只处理不透明像素；透明区已经是 (0,0,0,0)，动它没有意义。
    """
    if rgba.shape[2] != 4:
        raise ValueError(f"despill 需要 RGBA，收到 {rgba.shape}")

    opaque = rgba[:, :, 3] > 0
    if not opaque.any():
        return rgba.copy()

    key_arr = np.asarray(key, dtype=np.int16)
    high = key_arr >= 128
    if not high.any() or not (~high).any():
        # 纯黑或纯白之类的键控色没有可利用的通道差异，跳过。
        logger.debug("键控色 %s 无通道差异，跳过 despill", key)
        return rgba.copy()

    amount = spill_amount(rgba[:, :, :3], key)
    target = opaque & (amount > 0)
    if edge_only:
        target &= _edge_band(opaque, edge_width)

    if not target.any():
        return rgba.copy()

    out = rgba.astype(np.int16)
    reduction = (amount * strength).astype(np.int16)
    for channel in np.nonzero(high)[0]:
        out[:, :, channel] = np.where(
            target, out[:, :, channel] - reduction, out[:, :, channel]
        )

    out[:, :, :3] = np.clip(out[:, :, :3], 0, 255)
    result = out.astype(np.uint8)
    result[~opaque] = 0  # 保持透明像素 RGB 归零
    return result


def _edge_band(opaque: np.ndarray, width: int) -> np.ndarray:
    """紧邻透明区的前景像素带。溢色只发生在这里。"""
    from scipy.ndimage import binary_erosion

    if width < 1:
        return opaque
    interior = binary_erosion(
        opaque, np.ones((2 * width + 1, 2 * width + 1), dtype=bool), border_value=0
    )
    return opaque & ~interior


def spill_ratio(rgba: np.ndarray, key: tuple[int, int, int], *, margin: int = 24) -> float:
    """估算仍带溢色的前景像素比例。供验证与回归对比使用。"""
    if rgba.shape[2] != 4:
        raise ValueError("需要 RGBA")
    opaque = rgba[:, :, 3] > 0
    if not opaque.any():
        return 0.0
    return float((opaque & (spill_amount(rgba[:, :, :3], key) > margin)).sum() / opaque.sum())
