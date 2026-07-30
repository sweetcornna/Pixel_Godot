"""验证用的纯测量函数。

全部是纯函数：同一批帧必得同一批数字。测量与**判定**严格分开 ——
判定要查 per-action 阈值，而阈值还没校准（PLAN §9.1）；
测量不受此影响，可以先稳定下来，等阈值校准了直接复用。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np

from ..processing.anchor import content_anchor


def opaque_mask(frame: np.ndarray) -> np.ndarray:
    return frame[:, :, 3] > 0


def is_blank(frame: np.ndarray) -> bool:
    return not opaque_mask(frame).any()


def content_box(frame: np.ndarray) -> tuple[int, int, int, int] | None:
    ys, xs = np.nonzero(opaque_mask(frame))
    if xs.size == 0:
        return None
    return (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)


def height_of(frame: np.ndarray) -> int:
    box = content_box(frame)
    return 0 if box is None else box[3] - box[1]


def silhouette_area(frame: np.ndarray) -> int:
    return int(opaque_mask(frame).sum())


def relative_spread(values: list[float]) -> float:
    """极差相对均值。0 表示完全一致。

    用极差而非标准差：验证关心的是"最差的那一帧偏了多少"，
    而不是整体离散程度 —— 一帧塌掉、其余正常时标准差会被稀释。
    """
    clean = [v for v in values if v > 0]
    if len(clean) < 2:
        return 0.0
    mean = sum(clean) / len(clean)
    return (max(clean) - min(clean)) / mean if mean else 0.0


def height_variation(frames: list[np.ndarray]) -> float:
    return relative_spread([float(height_of(f)) for f in frames])


def silhouette_variation(frames: list[np.ndarray]) -> float:
    return relative_spread([float(silhouette_area(f)) for f in frames])


def transparent_rgb_residue(frame: np.ndarray) -> int:
    """alpha=0 却带非零 RGB 的像素数。致命级检查项（PLAN §9.2）。"""
    transparent = frame[:, :, 3] == 0
    if not transparent.any():
        return 0
    return int((frame[transparent][:, :3] != 0).any(axis=-1).sum())


def frame_hash(frame: np.ndarray) -> bytes:
    """精确内容指纹，用于完全重复帧检测。"""
    return frame.tobytes()


def exact_duplicates(frames: list[np.ndarray]) -> list[tuple[int, int]]:
    seen: dict[bytes, int] = {}
    pairs: list[tuple[int, int]] = []
    for index, frame in enumerate(frames):
        key = frame_hash(frame)
        if key in seen:
            pairs.append((seen[key], index))
        else:
            seen[key] = index
    return pairs


def mask_difference(a: np.ndarray, b: np.ndarray) -> float:
    """两帧前景掩膜的异或面积占比。0 表示轮廓完全相同。"""
    ma, mb = opaque_mask(a), opaque_mask(b)
    if ma.shape != mb.shape:
        return 1.0
    return float((ma ^ mb).sum() / ma.size)


def pixel_difference(a: np.ndarray, b: np.ndarray) -> float:
    """两帧的像素差异占比（含颜色，不只是轮廓）。"""
    if a.shape != b.shape:
        return 1.0
    return float((a != b).any(axis=-1).mean())


def adjacent_differences(frames: list[np.ndarray], *, loop: bool) -> list[float]:
    """相邻帧差异序列。``loop=True`` 时首尾相接也算一对。"""
    if len(frames) < 2:
        return []
    pairs = list(itertools.pairwise(frames))
    if loop:
        pairs.append((frames[-1], frames[0]))
    return [mask_difference(a, b) for a, b in pairs]


@dataclass(frozen=True, slots=True)
class AnchorMeasurement:
    max_drift_px: float
    baseline_spread_px: float
    horizontal_spread_px: float


def anchor_measurement(frames: list[np.ndarray]) -> AnchorMeasurement:
    """对齐后的锚点偏差。

    分开报"脚底"与"水平中心"两个方向：脚底漂移会让角色上下抖，
    水平漂移会让角色左右滑，两者的成因不同，混成一个数字排障时没法用。

    水平位置用**轮廓质心**，与 ``processing.anchor.content_anchor`` 同一个定义。
    不能用包围盒中心：剑甩出去时包围盒边界跟着动、中心跟着偏，可身体压根没动 ——
    那样量出来的"漂移"是假的，而真正的身体位移反被同一个剑的摆动抵消掉。
    对齐层瞄准质心、校验层却量包围盒，两边永远对不上账。
    """
    if not frames:
        return AnchorMeasurement(0.0, 0.0, 0.0)

    height, width = frames[0].shape[:2]
    target_x, target_y = width / 2.0, float(height)

    baselines: list[float] = []
    centres: list[float] = []
    for frame in frames:
        box = content_box(frame)
        if box is None:
            continue
        baselines.append(float(box[3]))
        position = content_anchor(frame)
        if position is not None:
            centres.append(position[0])

    if not baselines or not centres:
        return AnchorMeasurement(0.0, 0.0, 0.0)

    drift = max(
        max(abs(b - target_y) for b in baselines),
        max(abs(c - target_x) for c in centres),
    )
    return AnchorMeasurement(
        max_drift_px=drift,
        baseline_spread_px=max(baselines) - min(baselines),
        horizontal_spread_px=max(centres) - min(centres),
    )


def palette_of(frames: list[np.ndarray]) -> set[tuple[int, int, int]]:
    colors: set[tuple[int, int, int]] = set()
    for frame in frames:
        opaque = frame[opaque_mask(frame)]
        if opaque.size:
            colors.update(
                (int(c[0]), int(c[1]), int(c[2])) for c in np.unique(opaque[:, :3], axis=0)
            )
    return colors
