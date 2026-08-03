"""中间帧的尺寸护栏（补间链）。

这个模块只有一个函数，但它值得单独一份用例 —— 它是全项目唯一一处**算错就会
把整台机器拖垮**的地方，而且出事时进程被内核 SIGKILL，连回溯都拿不到。

原来的写法是 ``factor = wanted / max(1, own_content)``：只有下界没有上界。
一张键控后全透明的中间帧让 ``own_content`` 归零、被兜底成 1，倍数于是变成
"画布高 ÷ 1" —— 实测要求把 480×375 放大到 46080×36000，16.6 亿像素、
单张 RGBA 就 6.6 GB，吃穿 30 G 内存触发内核 OOM，连桌面进程一起被杀。

所以两条失败路径各有一条反例，只验通过侧没有判别力。
"""

from __future__ import annotations

import numpy as np
import pytest

from pixel_asset_forge.errors import ProcessingError
from pixel_asset_forge.pipelines.interpolate import (
    MAX_INBETWEEN_CANVAS_MULTIPLE,
    inbetween_size,
)

CANVAS = (96, 96)


def frame(width: int, height: int, *, content_rows: tuple[int, int] | None) -> np.ndarray:
    """一张 RGBA 帧。``content_rows`` 为 None 表示全透明。"""
    out = np.zeros((height, width, 4), dtype=np.uint8)
    if content_rows is not None:
        top, bottom = content_rows
        out[top:bottom, :, :3] = 200
        out[top:bottom, :, 3] = 255
    return out


# -- 通过侧 ----------------------------------------------------------------


def test_a_normal_frame_scales_so_its_content_matches_the_wanted_height() -> None:
    """内容占 375 行里的 300 行，要缩到 60 高 —— 倍数 0.2，整幅同比例缩。"""
    size = inbetween_size(
        frame(480, 375, content_rows=(50, 350)), wanted_height=60, canvas=CANVAS
    )
    assert size == (96, 75)


def test_upscaling_a_small_frame_is_still_allowed() -> None:
    """护栏拦的是荒谬的倍数，不是"放大"本身。"""
    size = inbetween_size(
        frame(40, 60, content_rows=(10, 40)), wanted_height=60, canvas=CANVAS
    )
    assert size == (80, 120)


# -- 失败侧一：空帧（分母为 0） ---------------------------------------------


def test_an_empty_frame_is_refused_instead_of_being_treated_as_one_pixel_tall() -> None:
    """兜底成 1 会算出画布高那么多倍的放大 —— 这正是 OOM 的来路。"""
    with pytest.raises(ProcessingError, match="没有任何内容"):
        inbetween_size(
            frame(480, 375, content_rows=None), wanted_height=96, canvas=CANVAS
        )


def test_the_empty_frame_case_is_exactly_the_one_that_used_to_allocate_gigabytes() -> None:
    """点名记录实测数字，免得后来者把这条判据当冗余删掉。

    480×375、内容高兜底成 1、目标高 96 → 倍数 96 → 46080×36000 = 16.6 亿像素。
    """
    empty = frame(480, 375, content_rows=None)
    would_have_been = (480 * 96, 375 * 96)
    assert would_have_been[0] * would_have_been[1] > 1_600_000_000
    with pytest.raises(ProcessingError):
        inbetween_size(empty, wanted_height=96, canvas=CANVAS)


# -- 失败侧二：内容只剩几像素高（倍数有限但荒谬） ---------------------------


def test_a_frame_whose_content_is_a_few_pixels_tall_is_refused_by_the_upper_bound() -> None:
    """抽帧把一条边框或一撮噪点当成一帧时，分母不为 0，但倍数一样荒谬。

    375 高的图里只有 2 行内容，要缩到 96 高 → 倍数 48 → 18000 高，
    远超画布 96 的 8 倍上限。
    """
    with pytest.raises(ProcessingError, match="上限"):
        inbetween_size(
            frame(480, 375, content_rows=(100, 102)), wanted_height=96, canvas=CANVAS
        )


def test_the_upper_bound_leaves_real_frames_a_full_order_of_magnitude_of_room() -> None:
    """安全阀不能误伤正常值：实测中间帧约 63×102，上限是 768。"""
    limit = MAX_INBETWEEN_CANVAS_MULTIPLE * max(CANVAS)
    size = inbetween_size(
        frame(480, 375, content_rows=(10, 360)), wanted_height=96, canvas=CANVAS
    )
    assert max(size) * 5 < limit
