"""固定网格切帧（ADR-003，按 Sprint 0 / A-1 修订为按比例）。

切分**不做任何内容分析**：行列数在请求发出前就定了，这里只按几何位置硬切。
自动帧识别被明确否决过 —— 它的失败模式不可预测，且会让"生成错误"与
"识别错误"无法区分，而整个修复机制建立在能准确归因的前提上。

第一层修订是格线位置：由 512px 绝对偏移改为按实际图像尺寸的比例。
端点不保证按请求尺寸返回且不报错，按绝对偏移切会让每一帧静默错位。

比例切分之上还有一层收尾：先把原图居中裁到行列数的整倍数。否则 ``round()``
会让格线偏离理想位置最多 0.5px，随后统一尺寸又总从右/下裁，形成方向性偏差。
实测的 1774x887、2103x748、1717x916、1902x827 都出现过非整除边；例如
4x2 的 1774x887 会先裁成 1772x886，再切成完全相等的 443x443 单元格。
这只是按比例切帧前的确定性收尾，不取代按比例切帧或统一尺寸的安全网。
"""

from __future__ import annotations

import numpy as np

from ..errors import ProcessingError
from ..planning.grid_layout import GridLayout


def center_crop_to_grid(image: np.ndarray, layout: GridLayout) -> np.ndarray:
    """居中裁到 ``layout`` 行列数的整倍数，保证比例格线落在整数像素上。

    每个方向丢弃的像素必然少于对应的行列数；奇数余量时，右侧或下侧多丢
    一个像素。已经整除的输入原样返回，确保所有名义尺寸严格 no-op。
    """
    if image.ndim < 2:
        raise ProcessingError(f"期望二维以上的图像数组，收到 {image.shape}")

    height, width = image.shape[:2]
    if width < layout.cols or height < layout.rows:
        raise ProcessingError(
            f"图像 {width}×{height} 小于 {layout.cols}×{layout.rows} 网格，无法切分"
        )

    cropped_width = (width // layout.cols) * layout.cols
    cropped_height = (height // layout.rows) * layout.rows
    discard_x = width - cropped_width
    discard_y = height - cropped_height
    if discard_x == 0 and discard_y == 0:
        return image

    left = discard_x // 2
    top = discard_y // 2
    return np.ascontiguousarray(
        image[top : top + cropped_height, left : left + cropped_width]
    )


def split_grid(image: np.ndarray, layout: GridLayout) -> list[np.ndarray]:
    """把网格图切成 ``layout.frames`` 个单元格。

    返回的每一格都是独立副本 —— 不返回视图，避免后续原地操作互相污染。
    """
    image = center_crop_to_grid(image, layout)
    height, width = image.shape[:2]

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

    ``split_grid`` 的整倍数裁剪应让这一步成为恒等操作，但仍保留为第二层安全网，
    防止其他调用方传入不等大的帧。统一裁掉的边缘有 prompt 要求的至少 12% 空白。
    """
    if not frames:
        return frames

    min_h = min(f.shape[0] for f in frames)
    min_w = min(f.shape[1] for f in frames)
    return [np.ascontiguousarray(f[:min_h, :min_w]) for f in frames]
