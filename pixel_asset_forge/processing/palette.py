"""调色板量化。

**整组帧必须共用一个调色板。** 逐帧各自量化会让同一块布料在相邻帧里
取到不同的颜色，播放起来整个角色在闪烁 —— 而这能通过全部几何类验证项。

量化前必须已经做过 despill（ADR-004）：一圈没去掉的洋红边会占掉
16~24 色调色板里好几个格子，表现为"莫名其妙多出几个紫色"。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image

from ..errors import ProcessingError


@dataclass(frozen=True, slots=True)
class PaletteResult:
    frames: list[np.ndarray]
    colors: list[str]

    quantization_error_ratio: float
    """量化后与原色差异显著（任一通道 >32）的像素比例。

    这是**质量信息，不是合规判据**。它衡量的是"为了压进 N 色损失了多少"，
    与色数强相关：实测同一组帧在 8 / 16 / 24 / 32 / 64 色下分别是
    21.3% / 9.5% / 8.8% / 6.7% / 1.7%。

    别拿它去套 PLAN §9.2 的"调色板越界率 ≤ 2%"—— 那一条问的是另一件事，
    见 :func:`palette_overflow_ratio`。
    """

    @property
    def color_count(self) -> int:
        return len(self.colors)


def palette_overflow_ratio(frames: list[np.ndarray], palette: list[str]) -> float:
    """用了声明调色板之外的颜色的像素占比（PLAN §9.2，阈值 ≤ 2%）。

    我们自己量化出来的帧，这个值必然是 0 —— 这正是它的用处：
    它是一道**回归守卫**，专门抓"量化之后又有步骤引入了新颜色"的情况
    （量化后才做的 despill、带插值的翻转、手工改图都会触发）。
    """
    allowed = {c.upper() for c in palette}
    total = 0
    outside = 0
    for frame in frames:
        opaque = frame[frame[:, :, 3] > 0]
        if not opaque.size:
            continue
        total += len(opaque)
        colors, counts = np.unique(opaque[:, :3], axis=0, return_counts=True)
        for color, count in zip(colors, counts, strict=True):
            if _to_hex(tuple(int(v) for v in color)) not in allowed:
                outside += int(count)
    return outside / total if total else 0.0


def _to_hex(rgb: tuple[int, ...]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb[:3])


def extract_palette(frames: list[np.ndarray]) -> list[str]:
    """列出整组帧中出现过的不透明颜色。"""
    seen: set[tuple[int, ...]] = set()
    for frame in frames:
        opaque = frame[frame[:, :, 3] > 0]
        if opaque.size:
            seen.update(tuple(int(v) for v in px[:3]) for px in np.unique(opaque, axis=0))
    return sorted(_to_hex(c) for c in seen)


def quantize_frames(
    frames: list[np.ndarray],
    max_colors: int,
    *,
    dither: bool = False,
) -> PaletteResult:
    """把整组帧量化到至多 ``max_colors`` 色，共用一个调色板。

    实现方式是把所有帧横向拼成一张长图后一次性量化 —— 这是"共用调色板"
    最直接的保证，不需要自己实现 median cut 再分发。

    ``dither`` 默认关闭：抖动会在像素画上制造噪点纹理，与硬边缘风格冲突。
    """
    if not frames:
        raise ProcessingError("帧列表为空")
    if max_colors < 2:
        raise ProcessingError(f"max_colors 至少为 2，收到 {max_colors}")

    sizes = {f.shape[:2] for f in frames}
    if len(sizes) > 1:
        raise ProcessingError(f"量化要求所有帧尺寸一致，收到 {sorted(sizes)}")

    height, width = frames[0].shape[:2]
    strip = np.concatenate(frames, axis=1)

    alpha = strip[:, :, 3]
    rgb = strip[:, :, :3].copy()
    # 透明区参与量化会白白占掉调色板名额。先把它填成前景的平均色，
    # 量化后再按 alpha 抠回去。
    opaque = alpha > 0
    if not opaque.any():
        raise ProcessingError("整组帧都是全透明，无法量化")
    rgb[~opaque] = rgb[opaque].mean(axis=0).astype(np.uint8)

    image = Image.fromarray(rgb, mode="RGB")
    quantized = image.quantize(
        colors=max_colors,
        method=Image.Quantize.MEDIANCUT,
        dither=Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE,
    ).convert("RGB")
    result_rgb = np.array(quantized)

    diff = np.abs(result_rgb.astype(np.int16) - strip[:, :, :3].astype(np.int16)).max(axis=-1)
    error_ratio = float((diff[opaque] > 32).mean()) if opaque.any() else 0.0

    out_frames = []
    for index in range(len(frames)):
        piece_rgb = result_rgb[:, index * width : (index + 1) * width]
        piece_alpha = alpha[:, index * width : (index + 1) * width]
        piece = np.zeros((height, width, 4), dtype=np.uint8)
        mask = piece_alpha > 0
        piece[mask, :3] = piece_rgb[mask]
        piece[mask, 3] = 255
        out_frames.append(piece)

    return PaletteResult(
        frames=out_frames,
        colors=extract_palette(out_frames),
        quantization_error_ratio=error_ratio,
    )


def snap_to_palette(
    frames: list[np.ndarray], palette: list[str]
) -> tuple[list[np.ndarray], float]:
    """把整组帧**锁死**到给定调色板。返回 ``(帧, 最大偏移距离)``。

    与 :func:`quantize_frames` 的区别是它不**求**调色板，而是**服从**一个既有的。

    补间必须走这条路：中间帧是新生成的，重新量化会解出一套自己的调色板，
    同一块布料在关键帧与中间帧里落到不同色号上 —— 播放时整个角色闪色。
    关键帧是用户给的基准，颜色只能是它们说了算。

    映射按 RGB 空间的欧氏距离取最近色。返回的最大偏移距离是**诊断值**：
    它大说明生成出来的中间帧用了调色板里根本没有的颜色（比如模型自作主张
    加了高光），值得告警，但不该因此拒绝 —— 锁死本来就是要把它们拉回来。
    """
    if not frames:
        raise ProcessingError("帧列表为空")
    if not palette:
        raise ProcessingError("调色板为空 —— 补间必须锁死到关键帧的调色板")

    table = np.array(
        [[int(color[i : i + 2], 16) for i in (1, 3, 5)] for color in palette],
        dtype=np.int16,
    )

    out: list[np.ndarray] = []
    worst = 0.0
    for frame in frames:
        opaque = frame[:, :, 3] > 0
        result = frame.copy()
        if opaque.any():
            pixels = frame[:, :, :3][opaque].astype(np.int16)
            # (N, 1, 3) - (1, P, 3) → (N, P) 的距离矩阵
            distances = np.linalg.norm(
                pixels[:, None, :] - table[None, :, :], axis=2
            )
            nearest = np.argmin(distances, axis=1)
            worst = max(worst, float(distances[np.arange(len(nearest)), nearest].max()))
            result[:, :, :3][opaque] = table[nearest].astype(np.uint8)
        # 透明像素 RGB 必须为零（致命级验证项）
        result[~opaque] = 0
        out.append(result)

    return out, worst
