"""单元格越界检测（PLAN §2.3.2 / ADR-003）。

**在切帧之前**，在整张网格图上检测跨越格线的连通域。

为什么必须是切帧之前：切完之后每一格都是独立图像，跨格的肢体已经被切成两半，
再想判断"这是跨格还是本来就长这样"就没有依据了。

越界**不做本地修复**。跨格意味着构图本身错了 —— 被切掉的像素本地补不回来，
只能重生成整个动作网格（PLAN §9.3）。

Sprint 0 实测这条检查确实会命中：prompt 要 8% 边距时，一条格线上有 364 px 越界；
要 12% 时四条格线全干净。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import label

from ..planning.grid_layout import GridLayout


@dataclass(frozen=True, slots=True)
class GutterViolation:
    axis: str
    """``vertical`` 或 ``horizontal``。"""

    index: int
    position: int
    pixels: int

    def describe(self) -> str:
        axis = "竖" if self.axis == "vertical" else "横"
        return f"{axis}格线 {self.index} @ {self.position}px 上有 {self.pixels} 个前景像素"


@dataclass(frozen=True, slots=True)
class OverflowReport:
    violations: tuple[GutterViolation, ...]
    crossing_components: int
    """真正跨越格线的连通域个数 —— 比格线上的像素数更能说明问题严重程度。"""

    min_margin: float
    """所有单元格中最小的内容边距（占单元格边长的比例）。"""

    @property
    def clean(self) -> bool:
        return not self.violations and self.crossing_components == 0

    def summary(self) -> str:
        if self.clean:
            return f"无越界，最小边距 {self.min_margin:.1%}"
        parts = [v.describe() for v in self.violations]
        return (
            f"{self.crossing_components} 个连通域跨格；" + "；".join(parts)
            + f"；最小边距 {self.min_margin:.1%}"
        )


def _gutter_band(size: int, position: int, tolerance: int) -> slice:
    return slice(max(0, position - tolerance), min(size, position + tolerance + 1))


def detect_overflow(
    foreground: np.ndarray,
    layout: GridLayout,
    *,
    tolerance: int = 1,
) -> OverflowReport:
    """在前景掩膜上检测越界。

    ``tolerance`` 是格线两侧各允许的像素带宽 —— 按比例切格时格线位置会有
    ±1px 的取整误差，不留容差会把取整误差报成越界。
    """
    height, width = foreground.shape
    violations: list[GutterViolation] = []

    for col in range(1, layout.cols):
        x = round(col * width / layout.cols)
        band = foreground[:, _gutter_band(width, x, tolerance)]
        count = int(band.sum())
        if count:
            violations.append(GutterViolation("vertical", col, x, count))

    for row in range(1, layout.rows):
        y = round(row * height / layout.rows)
        band = foreground[_gutter_band(height, y, tolerance), :]
        count = int(band.sum())
        if count:
            violations.append(GutterViolation("horizontal", row, y, count))

    crossing = _count_crossing_components(foreground, layout)
    margin = _min_margin(foreground, layout)

    return OverflowReport(
        violations=tuple(violations),
        crossing_components=crossing,
        min_margin=margin,
    )


def _count_crossing_components(foreground: np.ndarray, layout: GridLayout) -> int:
    """连通域标注后，看有几个连通域同时落在两个以上的单元格里。

    比"格线上有多少像素"更能说明问题：一根越过格线的剑尖是真越界，
    而按比例切格的取整误差只会在格线上留下零星像素、不构成跨格连通域。
    """
    labels, count = label(foreground)
    if count == 0:
        return 0

    height, width = foreground.shape
    col_index = (np.arange(width) * layout.cols // width).astype(np.int32)
    row_index = (np.arange(height) * layout.rows // height).astype(np.int32)
    cell_index = row_index[:, None] * layout.cols + col_index[None, :]

    crossing = 0
    for component in range(1, count + 1):
        cells = np.unique(cell_index[labels == component])
        if cells.size > 1:
            crossing += 1
    return crossing


def _min_margin(foreground: np.ndarray, layout: GridLayout) -> float:
    """所有单元格中最小的内容边距。prompt 要 12%、判定按 8%（ADR-003）。"""
    height, width = foreground.shape
    margins: list[float] = []

    for index in range(layout.frames):
        x0, y0, x1, y1 = layout.cell_box(index, (width, height))
        cell = foreground[y0:y1, x0:x1]
        ys, xs = np.nonzero(cell)
        if xs.size == 0:
            continue  # 空格由 blank_frame 检查负责，不在这里判
        h, w = cell.shape
        margins.append(
            min(
                xs.min() / w,
                ys.min() / h,
                (w - 1 - xs.max()) / w,
                (h - 1 - ys.max()) / h,
            )
        )

    return min(margins) if margins else 0.0
