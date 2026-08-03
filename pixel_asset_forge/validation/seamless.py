"""无缝平铺的纯测量函数。

和 :mod:`.metrics` 同口径：只测量、不判定。判定要查阈值，而 tile 的阈值还没有
真实样本校准（PLAN §8.1），测量本身不受此影响。

**为什么是两条判据而不是一条。** "平铺后出现网格线"是两种互不相同的失败：

- **对边接不上** —— 左缘与右缘内容不连续（整幅左右渐变是典型），平铺后每隔
  一个 tile 一道突变。接缝处的差异**大**，:func:`seam_ratio` 抓它。
- **带边框 / 暗角** —— 模型把 tile 画成一张"有边的方形贴图"。这种 tile 的接缝
  是**边框接边框**，两边一样暗，差异**小**，接缝判据对它恒判通过；
  可它平铺后恰恰是最刺眼的规则网格线。:func:`border_deviation` 抓它。

先只写了接缝判据，构造带边框反例时才发现它判通过 —— 这里留一笔，免得后来者
把第二条当冗余删掉。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

SeamAxis = Literal["horizontal", "vertical"]

#: 差异的下限（0–255 强度）。低于 1 个色阶的差异肉眼不可见，用它兜底可以避免
#: 纯色 tile 把比值算成天文数字。
_DELTA_FLOOR = 1.0


def _rgb(tile: np.ndarray) -> np.ndarray:
    """取 RGB 三通道。tile 是满幅不透明的，alpha 即使存在也不参与判断。"""
    if tile.ndim != 3 or tile.shape[2] < 3:
        raise ValueError(f"tile 必须是 HxWx3 或 HxWx4，收到 {tile.shape}")
    return tile[:, :, :3].astype(np.float64)


def _lines(rgb: np.ndarray, axis: SeamAxis) -> np.ndarray:
    """按接缝方向取出一条条扫描线：水平接缝看列，垂直接缝看行。"""
    return np.moveaxis(rgb, 1, 0) if axis == "horizontal" else rgb


def _interior_diffs(lines: np.ndarray) -> np.ndarray:
    """内部每一对相邻扫描线的平均差异。"""
    return np.asarray(np.abs(lines[1:] - lines[:-1]).mean(axis=(1, 2)))


def pair_seam_ratio(first: np.ndarray, second: np.ndarray, axis: SeamAxis) -> float:
    """``first`` 的末列（行）接 ``second`` 的首列（行）时，接缝处的差异比。

    分子是接缝处**逐像素差异的均值**，分母是两块 tile 内部相邻扫描线差异的
    中位数。问的是"把这两块拼起来，接缝处的落差比这两块料子本身的典型落差大多少"。

    **分母取两块合起来的中位数**，不是只取 ``first`` 的：只拿一侧的颗粒度当尺子，
    会让"光滑 tile 接粗糙 tile"按错误的量纲判定，还会让关系凭空带上与接缝无关的
    方向性。合起来取中位数则天然对称。

    ``first is second`` 时合集就是同一批差异各出现两次，中位数不变 ——
    于是这个函数退化成 :func:`seam_ratio`，**逐位相等**。8.2 的邻接矩阵对角线
    因此必然与 8.1 已经验过的无缝判定同源（PLAN §8.2）。

    取中位数而不是均值：地面 tile 里常有少量高对比的细节（石子、裂缝），
    均值会被它们抬高，从而把真实的接缝突变稀释掉。
    """
    a = _lines(_rgb(first), axis)
    b = _lines(_rgb(second), axis)
    if len(a) < 2 or len(b) < 2:
        return 0.0
    if a.shape[1:] != b.shape[1:]:
        raise ValueError(
            f"两块 tile 在接缝方向上的长度不一致：{a.shape[1:]} vs {b.shape[1:]}"
        )
    interior = np.concatenate([_interior_diffs(a), _interior_diffs(b)])
    seam = float(np.abs(a[-1] - b[0]).mean())
    return seam / max(float(np.median(interior)), _DELTA_FLOOR)


def seam_ratio(tile: np.ndarray, axis: SeamAxis) -> float:
    """接缝处相邻扫描线的差异 ÷ 内部相邻扫描线差异的中位数。

    可平铺的 tile 里，接缝只是又一处普通的相邻关系，比值应当在 1 附近；
    对边接不上时接缝差异会显著高于内部的典型差异。

    "首尾相接"就是**这块 tile 接它自己**，所以这里直接委托给
    :func:`pair_seam_ratio` —— 两者相等是**构造出来的**，不是各写一遍再指望它们
    碰巧一致。
    """
    return pair_seam_ratio(tile, tile, axis)


def edge_color_gap(first: np.ndarray, second: np.ndarray, axis: SeamAxis) -> float:
    """接缝两侧那一列（行）的**均值之差**，逐通道取最大。单位是 0–255 色阶。

    **刻意不做任何归一化**，这是它与 :func:`pair_seam_ratio` 的全部区别，
    也是它存在的理由。8.1 的教训是"分母不能取会被失败信号抬高的量"，
    这里踩的是同一块石头的另一面：``pair_seam_ratio`` 的分母是颗粒度，而高频
    噪声纹理的颗粒度本来就大 —— 两块材质完全不同的高噪 tile（草接水）接缝差异
    虽大，除以同样大的颗粒度后照样落在阈值下方，判通过。

    两条判据的差别正在"先取绝对值还是先取均值"上，而这个差别恰好把两种失败分开：

    ==========================================  ==================  ==========
    ..                                          同材质（噪声不对齐）  材质换了
    ==========================================  ==================  ==========
    ``mean(|first末列 − second首列|)``           大                  大
    ``|mean(first末列) − mean(second首列)|``     小                  大
    ==========================================  ==================  ==========

    第一行除以颗粒度后回到 1 附近；第二行不除任何东西，所以噪声抬不高它。
    """
    a = _lines(_rgb(first), axis)
    b = _lines(_rgb(second), axis)
    if len(a) < 1 or len(b) < 1:
        return 0.0
    return float(np.max(np.abs(a[-1].mean(axis=0) - b[0].mean(axis=0))))


def _grain(rgb: np.ndarray) -> np.ndarray:
    """纹理自身的颗粒度：相邻像素差异的逐通道中位数。

    用它做分母，问的是"边缘与中心的落差，比这块料子本身的粗糙度大多少"。
    """
    horizontal = np.abs(rgb[:, 1:, :] - rgb[:, :-1, :]).reshape(-1, rgb.shape[2])
    vertical = np.abs(rgb[1:, :, :] - rgb[:-1, :, :]).reshape(-1, rgb.shape[2])
    return np.asarray(np.median(np.vstack([horizontal, vertical]), axis=0))


def border_deviation(tile: np.ndarray, *, ring: int = 1) -> float:
    """最外一圈与**中心区**的逐通道均值之差，除以纹理自身的颗粒度。

    三个选择都是被反例逼出来的：

    - **逐通道**而不是先转灰度：边框常常"同样亮但偏色"（暗绿描边压在灰地上），
      灰度会把这种差异抵消掉。取各通道里最大的那个偏离。
    - 比的是**中心区**而不是"除边框外的全部内部"：暗角是渐变的，紧挨边框那一圈
      也已经被压暗，拿整个内部做基准会被它自己稀释。
    - 除以**颗粒度**而不是内部标准差：暗角本身就会把标准差抬高，等于拿失败信号
      去归一化失败信号 —— 实测 32×32 的暗角 tile 这样只算出 1.07，落在阈值下方。

    适用范围是 **8.1 的基础地面 tile**。故意做成中心构图的装饰 tile 会被这条判高，
    那类 tile 不在 8.1 范围内（见 PLAN §8.1）。
    """
    rgb = _rgb(tile)
    height, width = rgb.shape[:2]
    if height <= 2 * ring or width <= 2 * ring:
        return 0.0

    mask = np.zeros((height, width), dtype=bool)
    mask[:ring, :] = mask[-ring:, :] = True
    mask[:, :ring] = mask[:, -ring:] = True
    border = rgb[mask]

    top, left = height // 4, width // 4
    center = rgb[top : height - top, left : width - left].reshape(-1, rgb.shape[2])
    if center.size == 0:
        return 0.0

    diff = np.abs(border.mean(axis=0) - center.mean(axis=0))
    return float(np.max(diff / np.maximum(_grain(rgb), _DELTA_FLOOR)))


@dataclass(frozen=True)
class SeamlessMeasurement:
    """一张 tile 的全部无缝测量值。"""

    horizontal_seam_ratio: float
    vertical_seam_ratio: float
    border_deviation: float

    @property
    def worst_seam_ratio(self) -> float:
        return max(self.horizontal_seam_ratio, self.vertical_seam_ratio)


def measure_seamless(tile: np.ndarray) -> SeamlessMeasurement:
    return SeamlessMeasurement(
        horizontal_seam_ratio=seam_ratio(tile, "horizontal"),
        vertical_seam_ratio=seam_ratio(tile, "vertical"),
        border_deviation=border_deviation(tile),
    )
