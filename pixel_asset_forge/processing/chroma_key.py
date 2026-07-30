"""色键去背景（ADR-004，按 Sprint 0 / A-5 实测重写）。

三条从实测里换来的规则，每条都对应一个真实踩过的坑：

1. **阈值必须逐图求解。** 模型画不出精确的键控色 —— 实测背景是 ``#F204EA``
   这类近洋红，精确 ``#FF00FF`` 命中率 **0.00%**。两轮样本的背景距离 p50
   还差了 24 vs 19，写死任何常数都会在某批图上失效。

2. **形态学必须先补边再做。** ``scipy`` 的腐蚀在图像边界按"外部为前景"处理，
   会把最外一圈背景吞掉，导致从边缘泛洪的种子成为空集、掩膜全黑。
   症状具有欺骗性：表现为"前景占比 100%"，看起来像阈值选错了。

3. **只保留与画布外缘连通的背景。** 只按颜色距离判定会把角色内部恰好接近
   键控色的像素也抠掉，抠出洞来。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import binary_closing, binary_opening, binary_propagation

from ..errors import ProcessingError
from ..logging_utils import get_logger

logger = get_logger("processing.chroma_key")

#: 键控后合理的背景占比区间。越界说明这张图不是双峰分布
#: （例如背景被大面积遮挡），Otsu 会给出无意义的阈值。
MIN_BACKGROUND_RATIO = 0.20
MAX_BACKGROUND_RATIO = 0.98

#: 颜色距离的理论上界（sqrt(3) × 255）。
MAX_COLOR_DISTANCE = 441.673


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    raw = color.lstrip("#")
    if len(raw) != 6:
        raise ProcessingError(f"非法的十六进制颜色：{color!r}")
    return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))


def color_distance(rgb: np.ndarray, key: tuple[int, int, int]) -> np.ndarray:
    """每个像素到键控色的欧氏距离。"""
    diff = rgb.astype(np.float64) - np.asarray(key, dtype=np.float64)
    return np.sqrt((diff**2).sum(axis=-1))


def otsu_threshold(distances: np.ndarray, bins: int = 256) -> float:
    """在距离直方图上做 Otsu 二分。

    色键场景天然是双峰的 —— 背景一簇、前景一簇，中间有极宽的空谷
    （实测 40–200 区间只占 0.76% 的像素）。Otsu 在这种分布上非常稳。
    """
    flat = distances.reshape(-1)
    hist, edges = np.histogram(flat, bins=bins, range=(0.0, MAX_COLOR_DISTANCE))
    centers = (edges[:-1] + edges[1:]) / 2.0

    total = hist.sum()
    if total == 0:  # pragma: no cover - 空图
        raise ProcessingError("无法在空图上求解键控阈值")

    weight_bg = np.cumsum(hist)
    weight_fg = total - weight_bg
    cumulative = np.cumsum(hist * centers)
    grand_total = cumulative[-1]

    with np.errstate(invalid="ignore", divide="ignore"):
        mean_bg = cumulative / np.maximum(weight_bg, 1)
        mean_fg = (grand_total - cumulative) / np.maximum(weight_fg, 1)
        between = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2

    between[weight_bg == 0] = 0
    between[weight_fg == 0] = 0

    # 返回所选桶的**上边界**，不是桶中心。
    #
    # 桶中心会落在桶内部，把这个桶自己劈成两半 —— 若背景值恰好在中心之上
    # （完美均匀的背景就是这样：所有背景像素挤在同一个桶里），
    # ``distance <= threshold`` 会全部为假，整张图被判成前景、背景占比 0%。
    # 有噪声的真实数据里背景跨好几个桶，所以这个 bug 不会显形 ——
    # 恰恰是模型表现最好的时候才会踩中。
    return float(edges[int(np.argmax(between)) + 1])


def _clean(mask: np.ndarray) -> np.ndarray:
    """去噪并只保留与画布外缘连通的部分。

    ``pad=3`` + ``border_value=1`` 两者缺一不可：补边给腐蚀留出余量，
    ``border_value=1`` 让边界外被视为集合内部，最外一圈才不会被吞掉。
    """
    padded = np.pad(mask, 3, constant_values=True)
    padded = binary_opening(padded, np.ones((3, 3), dtype=bool), border_value=1)
    padded = binary_closing(padded, np.ones((3, 3), dtype=bool), border_value=1)

    seed = np.zeros_like(padded)
    seed[0, :] = seed[-1, :] = True
    seed[:, 0] = seed[:, -1] = True
    seed &= padded

    return binary_propagation(seed, mask=padded)[3:-3, 3:-3]


@dataclass(frozen=True, slots=True)
class KeyResult:
    """一次键控的完整结果。``threshold`` 必须写入 Manifest。"""

    rgba: np.ndarray
    threshold: float
    key_color: tuple[int, int, int]
    background_ratio: float
    halo_pixels: int
    """落在阈值与前景簇之间的边缘像素数 —— despill 要处理的就是它们。"""

    @property
    def suspicious(self) -> bool:
        """背景占比越界即说明阈值可能不可信，应升到下一档（ADR-004）。"""
        return not (MIN_BACKGROUND_RATIO <= self.background_ratio <= MAX_BACKGROUND_RATIO)


def background_mask(
    rgb: np.ndarray, key: tuple[int, int, int], threshold: float | None = None
) -> tuple[np.ndarray, float]:
    """返回 ``(背景掩膜, 实际使用的阈值)``。

    传入 ``threshold`` 即复用既有阈值 —— ``process`` 离线重跑时必须走这条路，
    否则重新求解可能得到不同阈值，结果就不可复现了。
    """
    distances = color_distance(rgb, key)
    effective = otsu_threshold(distances) if threshold is None else threshold
    return _clean(distances <= effective), effective


def apply_chroma_key(
    rgb: np.ndarray,
    key: tuple[int, int, int],
    *,
    threshold: float | None = None,
) -> KeyResult:
    """把纯色背景抠成透明，返回 RGBA。

    透明像素的 RGB 一并清零 —— 这是一个**致命级**验证项（PLAN §9.2）：
    ``alpha=0`` 的 RGB 虽然不可见，但会影响压缩率、在双线性过滤下渗出色边、
    并干扰调色板统计。
    """
    if rgb.ndim != 3 or rgb.shape[2] < 3:
        raise ProcessingError(f"期望 HxWx3 的 RGB 数组，收到 {rgb.shape}")

    rgb = rgb[:, :, :3]
    distances = color_distance(rgb, key)
    effective = otsu_threshold(distances) if threshold is None else threshold

    mask = _clean(distances <= effective)
    foreground = ~mask

    rgba = np.zeros((*rgb.shape[:2], 4), dtype=np.uint8)
    rgba[foreground, :3] = rgb[foreground]
    rgba[foreground, 3] = 255
    # 背景区整块保持 (0,0,0,0)：RGB 清零是上面 np.zeros 天然保证的。

    ratio = float(mask.mean())
    halo = int(((distances > effective) & (distances < 200)).sum())

    result = KeyResult(
        rgba=rgba,
        threshold=effective,
        key_color=key,
        background_ratio=ratio,
        halo_pixels=halo,
    )
    if result.suspicious:
        logger.warning(
            "键控后背景占比 %.1f%% 落在合理区间之外 —— 阈值可能不可信，建议升到下一档",
            ratio * 100,
        )
    return result


def zero_transparent_rgb(rgba: np.ndarray) -> np.ndarray:
    """强制清零全透明像素的 RGB。

    :func:`apply_chroma_key` 已经保证了这一点，但经过 despill、量化、
    翻转等步骤后仍要再确认一次 —— 这是致命级验证项，成本又几乎为零。
    """
    out = rgba.copy()
    out[out[:, :, 3] == 0, :3] = 0
    return out
