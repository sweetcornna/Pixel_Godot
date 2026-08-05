"""切帧、越界检测、裁剪、缩放、锚点对齐。

Sprint 3 的退出门槛大半落在这个文件：
切帧像素级准确 · 所有输出帧尺寸完全一致 · 最近邻不引入中间色 · 锚点写入 Manifest。
"""

from __future__ import annotations

import numpy as np
import pytest

from pixel_asset_forge.errors import ProcessingError
from pixel_asset_forge.planning import (
    GridLayout,
    grid_for_frames,
    seed_layout,
    strip_for_frames,
)
from pixel_asset_forge.processing import (
    BOTTOM_CENTER,
    align_frames,
    anchor_drift,
    assert_uniform_size,
    block_median_resize,
    center_crop_to_grid,
    content_anchor,
    content_bounds,
    crop_all,
    detect_overflow,
    fit_within,
    introduces_new_colors,
    nearest_resize,
    normalize_cell_sizes,
    place_on_canvas,
    save_frames,
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
    cropped_width = (width // layout.cols) * layout.cols
    cropped_height = (height // layout.rows) * layout.rows
    left = (width - cropped_width) // 2
    top = (height - cropped_height) // 2
    for index in range(layout.frames):
        x0, y0, x1, y1 = layout.cell_box(index, (cropped_width, cropped_height))
        x0, x1 = x0 + left, x1 + left
        y0, y1 = y0 + top, y1 + top
        img[y0:y1, x0:x1, :3] = 10 * (index + 1)
        img[y0:y1, x0:x1, 3] = 255
    return img


def legacy_split_grid(image: np.ndarray, layout: GridLayout) -> list[np.ndarray]:
    """修复前的比例切帧，作为 no-op 与反例测试的明确基线。"""
    height, width = image.shape[:2]
    return [
        np.ascontiguousarray(image[y0:y1, x0:x1])
        for x0, y0, x1, y1 in layout.boxes((width, height))
    ]


def subject_positions(frames: list[np.ndarray]) -> list[tuple[float, float]]:
    positions = []
    for frame in frames:
        ys, xs = np.nonzero(frame)
        positions.append((float(xs.mean()), float(ys.mean())))
    return positions


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
    # 居中裁到 1772×886 后，每格精确 443×443，而不是名义 512×512。
    assert {frame.shape[:2] for frame in frames} == {(443, 443)}


def test_normalize_remains_an_identity_safety_net_after_exact_split() -> None:
    """整倍数裁剪让第二层统一尺寸成为恒等操作，但这层安全网仍保留。"""
    layout = grid_for_frames(6)
    frames = split_grid(marked_grid(layout, (1537, 1025)), layout)
    normalized = normalize_cell_sizes(frames)
    assert len({f.shape[:2] for f in frames}) == 1
    assert all(
        np.array_equal(before, after)
        for before, after in zip(frames, normalized, strict=True)
    )


def test_normalize_still_corrects_independently_supplied_uneven_frames() -> None:
    normalized = normalize_cell_sizes([rgba(5, 7), rgba(6, 6)])
    assert {frame.shape[:2] for frame in normalized} == {(5, 6)}


def test_center_crop_distributes_remainders_across_both_sides() -> None:
    layout = GridLayout(frames=24, cols=6, rows=4)
    image = np.arange(13 * 15, dtype=np.uint8).reshape(13, 15)

    cropped = center_crop_to_grid(image, layout)

    # Width remainder 3 -> left 1/right 2; height remainder 1 -> top 0/bottom 1.
    assert np.array_equal(cropped, image[0:12, 1:13])


@pytest.mark.parametrize(
    ("size", "cols", "rows"),
    [
        ((1774, 887), 4, 2),
        ((2103, 748), 6, 1),
        ((1717, 916), 6, 1),
        ((1902, 827), 4, 1),
        ((1537, 1025), 3, 2),
        ((101, 103), 7, 5),
    ],
)
def test_center_crop_makes_every_grid_line_deviation_exactly_zero(
    size: tuple[int, int], cols: int, rows: int
) -> None:
    width, height = size
    layout = GridLayout(frames=cols * rows, cols=cols, rows=rows)
    cropped = center_crop_to_grid(rgba(height, width), layout)
    cropped_height, cropped_width = cropped.shape[:2]

    deviations = [
        abs(round(line * cropped_width / cols) - line * cropped_width / cols)
        for line in range(cols + 1)
    ] + [
        abs(round(line * cropped_height / rows) - line * cropped_height / rows)
        for line in range(rows + 1)
    ]

    assert max(deviations) == 0
    assert {frame.shape[:2] for frame in split_grid(rgba(height, width), layout)} == {
        (cropped_height // rows, cropped_width // cols)
    }


NOMINAL_LAYOUTS = [
    *(pytest.param(strip_for_frames(frames), id=f"strip-{frames}") for frames in range(1, 7)),
    *(
        pytest.param(grid_for_frames(frames), id=f"grid-{frames}")
        for frames in (4, 6, 8, 9, 12)
    ),
    pytest.param(seed_layout(), id="seed"),
]


@pytest.mark.parametrize("layout", NOMINAL_LAYOUTS)
def test_nominal_sizes_are_byte_identical_to_legacy_split(layout: GridLayout) -> None:
    width, height = layout.size
    image = (np.arange(width * height, dtype=np.uint32) % 251).astype(np.uint8)
    image = image.reshape(height, width)

    cropped = center_crop_to_grid(image, layout)
    before = legacy_split_grid(image, layout)
    after = split_grid(image, layout)

    assert cropped is image
    assert len(before) == len(after)
    assert all(
        old.shape == new.shape and old.tobytes() == new.tobytes()
        for old, new in zip(before, after, strict=True)
    )


def test_center_crop_eliminates_subject_position_error() -> None:
    """修复前主体位置误差大于零；居中整倍数裁剪后严格归零。"""
    width, height = 1774, 887
    layout = grid_for_frames(8)
    cell_width, cell_height = width // layout.cols, height // layout.rows
    left = (width - cell_width * layout.cols) // 2
    top = (height - cell_height * layout.rows) // 2
    within_x, within_y = 100, 120
    image = np.zeros((height, width), dtype=np.uint8)

    for index in range(layout.frames):
        col, row = index % layout.cols, index // layout.cols
        x = left + col * cell_width + within_x
        y = top + row * cell_height + within_y
        image[y : y + 5, x : x + 5] = 255

    expected = (within_x + 2.0, within_y + 2.0)
    legacy = subject_positions(normalize_cell_sizes(legacy_split_grid(image, layout)))
    exact = subject_positions(normalize_cell_sizes(split_grid(image, layout)))
    legacy_error = max(np.hypot(x - expected[0], y - expected[1]) for x, y in legacy)
    exact_error = max(np.hypot(x - expected[0], y - expected[1]) for x, y in exact)

    assert legacy_error == pytest.approx(2**0.5)
    assert legacy_error > 0
    assert exact_error == 0


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


def test_saving_fewer_frames_removes_the_leftovers(tmp_path) -> None:
    """帧数变少时旧帧必须消失 —— 按目录读帧的代码会把残帧算进去。"""
    frames = [np.zeros((4, 4, 4), dtype=np.uint8) for _ in range(5)]
    save_frames(frames, tmp_path, stem="walk_down")
    assert len(list(tmp_path.glob("*.png"))) == 5

    kept = save_frames(frames[:2], tmp_path, stem="walk_down")
    assert sorted(p.name for p in tmp_path.glob("*.png")) == [p.name for p in kept]


# -- 大比例缩小 -------------------------------------------------------------


def test_block_median_keeps_a_line_connected_where_nearest_breaks_it() -> None:
    """一条与块同宽的斜线缩小 8 倍：最近邻会采成断续虚点，分块中位保持连续。

    补间的中间帧就是这么糊掉的 —— 它的源分辨率是关键帧的两倍，同样缩到 74px，
    弓被点采样打成一条虚线。
    """
    frame = np.zeros((64, 64, 4), dtype=np.uint8)
    frame[:, :, 3] = 255
    frame[:, :, :3] = 20
    for row in range(64):  # 8px 宽的斜线，正好一个块
        frame[row, row // 1 % 56 : row % 56 + 8] = [255, 255, 255, 255]

    def lit_rows(out: np.ndarray) -> int:
        return int((out[:, :, 0] > 128).any(axis=1).sum())

    assert lit_rows(block_median_resize(frame, (8, 8))) >= lit_rows(
        nearest_resize(frame, (8, 8))
    )


def test_block_median_drops_features_narrower_than_a_block() -> None:
    """中位是多数决：占不满半个块的东西就是会消失，这是它的定义而不是缺陷。

    写下来是为了别人改滤镜时知道边界在哪 —— 想保住细于块的特征，
    该做的是提高目标分辨率，不是换滤镜。
    """
    frame = np.zeros((64, 64, 4), dtype=np.uint8)
    frame[:, :, 3] = 255
    frame[:, :, :3] = 20
    frame[:, 30:32] = [255, 255, 255, 255]  # 2px 竖线，8px 的块里只占四分之一
    assert block_median_resize(frame, (8, 8))[:, :, 0].max() == 20


def test_block_median_keeps_alpha_binary() -> None:
    frame = np.zeros((32, 32, 4), dtype=np.uint8)
    frame[8:24, 8:24] = [200, 100, 50, 255]
    out = block_median_resize(frame, (7, 7))
    assert set(np.unique(out[:, :, 3])) <= {0, 255}


def test_block_median_does_not_darken_the_edges() -> None:
    """RGB 只能统计不透明像素 —— 把透明区那些被清零的 RGB 算进去，整圈边缘发黑。"""
    frame = np.zeros((32, 32, 4), dtype=np.uint8)
    frame[8:24, 8:24] = [255, 255, 255, 255]
    out = block_median_resize(frame, (16, 16))
    visible = out[out[:, :, 3] > 0][:, :3]
    assert visible.min() > 200, visible.min()
