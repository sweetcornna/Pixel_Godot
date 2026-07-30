"""切帧、越界检测、裁剪、缩放、锚点对齐。

Sprint 3 的退出门槛大半落在这个文件：
切帧像素级准确 · 所有输出帧尺寸完全一致 · 最近邻不引入中间色 · 锚点写入 Manifest。
"""

from __future__ import annotations

import numpy as np
import pytest

from pixel_asset_forge.errors import ProcessingError
from pixel_asset_forge.planning import GridLayout, grid_for_frames
from pixel_asset_forge.processing import (
    BOTTOM_CENTER,
    align_frames,
    anchor_drift,
    assert_uniform_size,
    content_anchor,
    content_bounds,
    crop_all,
    detect_overflow,
    fit_within,
    introduces_new_colors,
    nearest_resize,
    normalize_cell_sizes,
    place_on_canvas,
    split_grid,
    union_bounds,
)


def rgba(height: int, width: int, colour=(10, 20, 30)) -> np.ndarray:
    arr = np.zeros((height, width, 4), dtype=np.uint8)
    arr[:, :, :3] = colour
    arr[:, :, 3] = 255
    return arr


def blank(height: int, width: int) -> np.ndarray:
    return np.zeros((height, width, 4), dtype=np.uint8)


def marked_grid(layout: GridLayout, size: tuple[int, int]) -> np.ndarray:
    """每格填一个唯一的灰度值，切帧正确与否一眼可判。"""
    width, height = size
    img = np.zeros((height, width, 4), dtype=np.uint8)
    for index in range(layout.frames):
        x0, y0, x1, y1 = layout.cell_box(index, size)
        img[y0:y1, x0:x1, :3] = 10 * (index + 1)
        img[y0:y1, x0:x1, 3] = 255
    return img


# -- 切帧 -----------------------------------------------------------------


def test_split_is_pixel_exact_in_reading_order() -> None:
    """帧序固定为从左到右、从上到下（PLAN §2.3.1）。"""
    layout = grid_for_frames(8)
    size = (1774, 887)  # Sprint 0 实测返回的尺寸
    frames = split_grid(marked_grid(layout, size), layout)

    assert len(frames) == 8
    for index, frame in enumerate(frames):
        assert frame[:, :, 0].min() == frame[:, :, 0].max() == 10 * (index + 1)


def test_split_follows_the_actual_size_not_the_requested_one() -> None:
    """按 512px 绝对偏移切 1774×887 会让每一帧静默错位（Sprint 0 / A-1）。"""
    layout = grid_for_frames(8)
    frames = split_grid(marked_grid(layout, (1774, 887)), layout)
    total = sum(f.shape[0] * f.shape[1] for f in normalize_cell_sizes(frames))
    assert total > 0
    # 每格尺寸应接近 1774/4 × 887/2，而不是 512×512
    assert frames[0].shape[1] in (443, 444)
    assert frames[0].shape[0] in (443, 444)


def test_normalize_makes_every_frame_identical_in_size() -> None:
    """尺寸不一致的帧组装成 spritesheet 会整体错位。"""
    layout = grid_for_frames(6)
    frames = split_grid(marked_grid(layout, (1537, 1025)), layout)
    assert len({f.shape[:2] for f in frames}) > 1  # 取整让格子差 1px
    assert len({f.shape[:2] for f in normalize_cell_sizes(frames)}) == 1


def test_assert_uniform_size_rejects_mismatch() -> None:
    with pytest.raises(ProcessingError, match="尺寸不一致"):
        assert_uniform_size([rgba(10, 10), rgba(10, 11)])


def test_assert_uniform_size_returns_width_height() -> None:
    assert assert_uniform_size([rgba(8, 12), rgba(8, 12)]) == (12, 8)


def test_split_rejects_undersized_image() -> None:
    with pytest.raises(ProcessingError):
        split_grid(rgba(1, 1), grid_for_frames(8))


# -- 越界检测 --------------------------------------------------------------


def test_clean_grid_reports_no_overflow() -> None:
    layout = grid_for_frames(4)
    fg = np.zeros((1024, 1024), dtype=bool)
    for index in range(4):
        x0, y0, x1, y1 = layout.cell_box(index, (1024, 1024))
        fg[y0 + 80 : y1 - 80, x0 + 80 : x1 - 80] = True

    report = detect_overflow(fg, layout)
    assert report.clean
    assert report.min_margin > 0.15


def test_pose_crossing_a_gutter_is_detected() -> None:
    """跨格意味着构图错了，本地补不回被切掉的像素 —— 只能重生成。"""
    layout = grid_for_frames(4)
    fg = np.zeros((1024, 1024), dtype=bool)
    fg[100:400, 400:700] = True  # 横跨 x=512 的竖格线

    report = detect_overflow(fg, layout)
    assert not report.clean
    assert report.crossing_components >= 1
    assert any(v.axis == "vertical" for v in report.violations)


def test_rounding_noise_within_tolerance_is_not_reported() -> None:
    """按比例切格有 ±1px 取整误差，不留容差会把它报成越界。"""
    layout = grid_for_frames(4)
    fg = np.zeros((1024, 1024), dtype=bool)
    for index in range(4):
        x0, y0, x1, y1 = layout.cell_box(index, (1024, 1024))
        fg[y0 + 80 : y1 - 80, x0 + 80 : x1 - 80] = True
    assert detect_overflow(fg, layout, tolerance=1).clean


def test_min_margin_reflects_the_tightest_cell() -> None:
    layout = grid_for_frames(4)
    fg = np.zeros((1024, 1024), dtype=bool)
    for index in range(4):
        x0, y0, x1, y1 = layout.cell_box(index, (1024, 1024))
        pad = 50 if index else 10  # 第 0 格贴得很近
        fg[y0 + pad : y1 - pad, x0 + pad : x1 - pad] = True
    report = detect_overflow(fg, layout)
    assert report.min_margin == pytest.approx(10 / 512, abs=0.005)


# -- 裁剪 -----------------------------------------------------------------


def test_content_bounds_ignores_transparent_padding() -> None:
    img = blank(20, 20)
    img[5:12, 3:9] = (1, 2, 3, 255)
    box = content_bounds(img)
    assert (box.left, box.top, box.right, box.bottom) == (3, 5, 9, 12)


def test_blank_frame_has_empty_bounds() -> None:
    assert content_bounds(blank(8, 8)).empty


def test_group_shares_one_crop_box() -> None:
    """逐帧各裁各的等于给每帧施加不同平移，角色会在帧间跳动。"""
    a, b = blank(20, 20), blank(20, 20)
    a[4:10, 4:10] = (1, 1, 1, 255)
    b[8:16, 2:14] = (1, 1, 1, 255)

    cropped, box = crop_all([a, b])
    assert box == union_bounds([a, b])
    assert cropped[0].shape == cropped[1].shape


def test_crop_all_rejects_all_blank_frames() -> None:
    with pytest.raises(ProcessingError):
        crop_all([blank(8, 8), blank(8, 8)])


# -- 缩放 -----------------------------------------------------------------


def test_nearest_resize_introduces_no_intermediate_colours() -> None:
    """双线性插值会把 24 色调色板打成上千色 —— 这条是 Sprint 3 的硬门槛。"""
    img = blank(64, 64)
    img[:32, :, :3] = (200, 30, 40)
    img[32:, :, :3] = (20, 90, 200)
    img[:, :, 3] = 255

    small = nearest_resize(img, (16, 16))
    assert introduces_new_colors(img, small) == set()


def test_upscaling_also_introduces_nothing() -> None:
    img = blank(8, 8)
    img[2:6, 2:6] = (11, 22, 33, 255)
    assert introduces_new_colors(img, nearest_resize(img, (64, 64))) == set()


def test_resize_keeps_transparent_rgb_zero() -> None:
    img = blank(16, 16)
    img[4:8, 4:8] = (90, 90, 90, 255)
    out = nearest_resize(img, (8, 8))
    assert (out[out[:, :, 3] == 0][:, :3] == 0).all()


def test_fit_within_preserves_aspect_ratio() -> None:
    """非等比会把角色拉扁，而这能通过全部几何类验证项。"""
    assert fit_within((100, 50), (32, 32)) == (32, 16)
    assert fit_within((50, 100), (32, 32)) == (16, 32)
    assert fit_within((40, 40), (32, 32)) == (32, 32)


def test_resize_rejects_bad_target() -> None:
    with pytest.raises(ProcessingError):
        nearest_resize(rgba(8, 8), (0, 8))


# -- 锚点 -----------------------------------------------------------------


def test_bottom_center_places_feet_on_the_baseline() -> None:
    img = blank(20, 20)
    img[2:8, 6:10] = (1, 1, 1, 255)  # 内容偏上偏左

    out = place_on_canvas(img, (32, 32))
    ys, xs = np.nonzero(out[:, :, 3])
    assert ys.max() + 1 == 32  # 脚底贴住画布底边
    assert abs((xs.min() + xs.max() + 1) / 2 - 16) <= 1  # 水平居中


def test_alignment_removes_the_drift_the_model_leaves() -> None:
    """实测模型不会把脚对齐到统一基线，8 帧脚底极差达 9~10%。"""
    frames = []
    for offset in (0, 3, 7, 2):
        img = blank(32, 32)
        img[10 + offset : 20 + offset, 12:20] = (1, 1, 1, 255)
        frames.append(img)

    assert anchor_drift(frames) > 1
    # 质心是小数、像素偏移是整数，残差最多半个像素 —— 这是定义上的下限，
    # 不是回归。断言 == 0 会把"锚点必须是整数坐标"当成不变量。
    assert anchor_drift(align_frames(frames, (32, 32))) <= 0.5


def test_aligned_frames_all_share_the_canvas_size() -> None:
    frames = [rgba(9, 7), rgba(12, 5)]
    aligned = align_frames(frames, (16, 16))
    assert {f.shape[:2] for f in aligned} == {(16, 16)}


def test_blank_frame_yields_blank_canvas() -> None:
    out = place_on_canvas(blank(8, 8), (16, 16))
    assert out.shape == (16, 16, 4)
    assert out.sum() == 0


def test_anchor_pixel_position() -> None:
    assert BOTTOM_CENTER.pixel_position((32, 32)) == (16, 32)


# -- 左右摇摆（用户实测缺陷）-----------------------------------------------


def swinging_sword_frame(sword_dx: int, canvas: int = 60) -> np.ndarray:
    """身体固定在正中，只有"剑"在左右甩。

    身体每帧都在同一位置 —— 对齐之后它也必须在同一位置。
    """
    frame = np.zeros((canvas, canvas, 4), dtype=np.uint8)
    cx = canvas // 2
    frame[10:50, cx - 6 : cx + 6] = (139, 90, 43, 255)       # 躯干
    frame[44:50, cx - 7 : cx + 7] = (90, 60, 30, 255)        # 靴子
    x = max(0, min(canvas - 3, cx + sword_dx))
    frame[26:34, x : x + 3] = (220, 220, 200, 255)           # 剑
    return frame


def torso_center(frame: np.ndarray) -> float:
    """只看上身那几行的横向中心 —— 剑够不着这里。

    行号要从内容顶边往下数，不能写死：对齐会把内容整体上下平移。
    """
    top = int(np.nonzero(frame[:, :, 3])[0].min())
    return float(np.nonzero(frame[top : top + 8, :, 3] > 0)[1].mean())


def test_a_swinging_sword_does_not_make_the_body_sway() -> None:
    """用户实测：walk_down 播放时角色左右摇摆。

    根因是锚点取整个轮廓的包围盒中心 —— 剑甩出去时包围盒边界跟着动，
    中心相对身体就偏了，再对齐到画布中央等于把身体往反方向推。
    """
    frames = [swinging_sword_frame(dx) for dx in (-18, -9, 0, 9, 18, 9, 0, -9)]
    centers = [torso_center(f) for f in align_frames(frames, (60, 60))]
    spread = max(centers) - min(centers)
    # 包围盒中心下这里约 9px（剑摆幅的一半）。质心把它压到 1~2px：
    # 剑只有二十来个像素，相对头+躯干四百多个像素带不动质心。
    assert spread <= 2.0, f"身体横向漂移 {spread:.1f}px —— 播放时会左右摇摆"


def test_the_anchor_follows_the_feet_not_the_silhouette() -> None:
    left = content_anchor(swinging_sword_frame(-18))
    right = content_anchor(swinging_sword_frame(18))
    assert left is not None and right is not None
    assert abs(left[0] - right[0]) <= 2.0, "锚点被剑带跑了"


def test_the_anchor_still_tracks_a_real_sidestep() -> None:
    """整个角色真的挪了位置时，锚点必须跟着挪 —— 不能钝到连侧移都吃掉。"""
    a = content_anchor(np.roll(swinging_sword_frame(0), -8, axis=1))
    b = content_anchor(swinging_sword_frame(0))
    assert a is not None and b is not None
    assert b[0] - a[0] == pytest.approx(8.0, abs=0.5)
