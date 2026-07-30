"""固定网格切帧（ADR-003，按 Sprint 0 / A-1 修订为按比例）。

切分**不做任何内容分析**：行列数在请求发出前就定了，这里只按几何位置硬切。
自动帧识别被明确否决过 —— 它的失败模式不可预测，且会让"生成错误"与
"识别错误"无法区分，而整个修复机制建立在能准确归因的前提上。

唯一的修订是格线位置：由 512px 绝对偏移改为按实际图像尺寸的比例。
端点不保证按请求尺寸返回且不报错，按绝对偏移切会让每一帧静默错位。
"""

from __future__ import annotations

import numpy as np

from ..errors import ProcessingError
from ..planning.grid_layout import GridLayout


def split_grid(image: np.ndarray, layout: GridLayout) -> list[np.ndarray]:
    """把网格图切成 ``layout.frames`` 个单元格。

    返回的每一格都是独立副本 —— 不返回视图，避免后续原地操作互相污染。
    """
    if image.ndim < 2:
        raise ProcessingError(f"期望二维以上的图像数组，收到 {image.shape}")

    height, width = image.shape[:2]
    if width < layout.cols or height < layout.rows:
        raise ProcessingError(
            f"图像 {width}×{height} 小于 {layout.cols}×{layout.rows} 网格，无法切分"
        )

    frames = []
    for index in range(layout.frames):
        x0, y0, x1, y1 = layout.cell_box(index, (width, height))
        frames.append(np.ascontiguousarray(image[y0:y1, x0:x1]))
    return frames


def assert_uniform_size(frames: list[np.ndarray]) -> tuple[int, int]:
    """所有帧尺寸必须完全一致（Sprint 3 退出门槛）。

    按比例切格时，非整除的图像尺寸会让某些格子差 1px。
    这不是可以容忍的舍入误差 —— 尺寸不一致的帧组装成 spritesheet 会整体错位。
    """
    if not frames:
        raise ProcessingError("帧列表为空")

    sizes = {frame.shape[:2] for frame in frames}
    if len(sizes) > 1:
        raise ProcessingError(
            f"帧尺寸不一致：{sorted(sizes)}。按比例切格产生了差异，需要统一裁齐。"
        )
    height, width = sizes.pop()
    return (width, height)


def normalize_cell_sizes(frames: list[np.ndarray]) -> list[np.ndarray]:
    """把所有帧裁到共同的最小尺寸。

    按比例切格时格线取整会让边缘的格子差 1px。统一裁到最小尺寸是安全的：
    差的这 1px 落在单元格边缘，而 prompt 要求那里至少留 12% 空白。
    """
    if not frames:
        return frames

    min_h = min(f.shape[0] for f in frames)
    min_w = min(f.shape[1] for f in frames)
    return [np.ascontiguousarray(f[:min_h, :min_w]) for f in frames]
