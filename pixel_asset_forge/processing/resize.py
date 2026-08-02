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


#: 超过这个缩小倍数就该用分块中位数而不是最近邻。
#:
#: 最近邻缩小 N 倍等于"每 N 个像素只留 1 个"。N 小的时候丢的是冗余，
#: N 大的时候丢的是**内容** —— 实测补间的中间帧源分辨率是关键帧的两倍
#: （角色 1250px vs 600px），同样缩到 74px，最近邻把弓采成了一条虚线、
#: 脸上散着孤立杂点。用户看到的就是"补出来的那几帧形象不清晰"。
AREA_DOWNSCALE_RATIO = 2.0


def block_median_resize(rgba: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """分块取中位色缩小到 ``(width, height)``。

    大比例缩小时**唯一可用的滤镜**。三种做法在同一张补间产出上逐图比过：

    - **最近邻**：点采样。弓缩成一条断续虚线，脸上散着孤立杂点。
    - **面积平均**：轮廓连续了，但 1px 深色描边和浅色填充被平均成中间调，
      整张掉对比度，看着发灰。
    - **分块中位**：取块内的主导色而不是造中间色 —— 轮廓连续、对比度保住、
      颜色仍然贴着原有色阶。**采用这个。**

    与 ``pixel_grid._rgba_blocks`` 同源（alpha 按多数决、RGB 只统计不透明像素）。
    那边按探测出的块尺寸还原，这边按目标尺寸缩放，边界处理不同，所以各留一份。
    """
    if rgba.ndim != 3 or rgba.shape[2] != 4:
        raise ProcessingError(f"block_median_resize 需要 RGBA，收到 {rgba.shape}")
    width, height = size
    if width < 1 or height < 1:
        raise ProcessingError(f"目标尺寸非法：{size}")

    block_w = max(1, rgba.shape[1] // width)
    block_h = max(1, rgba.shape[0] // height)
    # 先对齐到块的整数倍，块才切得整齐。差额不到一个块，形变可忽略。
    fitted = nearest_resize(rgba, (width * block_w, height * block_h))
    blocks = fitted.reshape(height, block_h, width, block_w, 4)

    opaque = blocks[:, :, :, :, 3] > 0
    keep = opaque.mean(axis=(1, 3)) > 0.5

    values = blocks[:, :, :, :, :3].astype(np.float32)
    values[~opaque] = np.nan

    # 同 ``pixel_grid._rgba_blocks``：只对留下的块求中位数。全透明的块整块是
    # NaN，结果会被 ``keep`` 滤掉、压根用不上，却会发 RuntimeWarning。
    # 不用 ``catch_warnings`` 压制 —— 那是进程级状态，线程池下会失效。
    per_block = values.transpose(0, 2, 1, 3, 4)  # (height, width, bh, bw, 3)
    median = np.zeros((height, width, 3), dtype=np.float32)
    if keep.any():
        median[keep] = np.nanmedian(per_block[keep], axis=(1, 2))

    out = np.zeros((height, width, 4), dtype=np.uint8)
    out[keep, :3] = median[keep].astype(np.uint8)
    out[keep, 3] = 255
    return out
