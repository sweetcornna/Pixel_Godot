"""网格布局与 API 尺寸约束（PLAN §2.3 / ADR-003）。

网格表是整个系统的物理基础：它错了，所有生成请求都会被 API 拒绝，
或者切帧全部错位。所以每个档位都要逐条验四个约束。
"""

from __future__ import annotations

import pytest

from pixel_asset_forge.constants import (
    ALLOWED_FRAME_COUNTS,
    MAX_ASPECT_RATIO,
    MAX_SIDE,
    MAX_TOTAL_PIXELS,
    MIN_TOTAL_PIXELS,
    SIZE_MULTIPLE,
)
from pixel_asset_forge.errors import GridLayoutError
from pixel_asset_forge.planning import (
    aspect_mismatch,
    check_size,
    grid_for_frames,
    layout_for_frames,
    layout_matching_cell,
    seed_layout,
)


@pytest.mark.parametrize("frames", ALLOWED_FRAME_COUNTS)
def test_every_allowed_frame_count_is_api_compliant(frames: int) -> None:
    layout = grid_for_frames(frames)
    assert check_size(layout.width, layout.height) == []
    assert layout.width % SIZE_MULTIPLE == 0
    assert layout.height % SIZE_MULTIPLE == 0
    assert max(layout.size) / min(layout.size) <= MAX_ASPECT_RATIO
    assert MIN_TOTAL_PIXELS <= layout.width * layout.height <= MAX_TOTAL_PIXELS
    assert max(layout.size) <= MAX_SIDE


@pytest.mark.parametrize("frames", ALLOWED_FRAME_COUNTS)
def test_grid_capacity_fits_frames(frames: int) -> None:
    layout = grid_for_frames(frames)
    assert layout.capacity == frames


def test_seed_is_1024_square_and_compliant() -> None:
    layout = seed_layout()
    assert layout.size == (1024, 1024)
    assert check_size(*layout.size) == []


@pytest.mark.parametrize("frames", [1, 2, 3, 5, 7, 10, 16])
def test_unsupported_frame_counts_are_rejected_with_a_hint(frames: int) -> None:
    with pytest.raises(GridLayoutError) as exc:
        grid_for_frames(frames)
    # 报错必须给出可选档位，否则用户只能去翻文档。
    assert "4" in exc.value.message and "12" in exc.value.message


def test_reading_order_is_left_to_right_top_to_bottom() -> None:
    """帧序错乱是"静默失败"：其余检查项全会通过（PLAN §2.3.1）。"""
    layout = grid_for_frames(8)  # 4×2
    assert layout.cell_box(0) == (0, 0, 512, 512)
    assert layout.cell_box(3) == (1536, 0, 2048, 512)
    assert layout.cell_box(4) == (0, 512, 512, 1024)
    assert layout.cell_box(7) == (1536, 512, 2048, 1024)


def test_boxes_are_disjoint_and_cover_each_frame_once() -> None:
    layout = grid_for_frames(12)
    boxes = layout.boxes()
    assert len(boxes) == 12
    assert len(set(boxes)) == 12


# -- Sprint 0 / A-1：端点不保证按请求尺寸返回 -----------------------------


def test_slicing_follows_the_actual_image_size() -> None:
    """实测：请求 2048×1024 返回了 1774×887，且不报错（Sprint 0 报告 A-1）。

    按 512px 绝对偏移切这张图会让每一帧都静默错位 —— 比帧序乱序更致命，
    因为连单帧本身都是残缺的。
    """
    layout = grid_for_frames(8)
    actual = (1774, 887)
    boxes = layout.boxes(actual)

    assert boxes[0] == (0, 0, 444, 444)
    assert boxes[3][2] == 1774  # 最右一格贴住右边缘
    assert boxes[7][3] == 887  # 最下一格贴住下边缘


def test_proportional_boxes_tile_the_image_without_gaps_or_overlap() -> None:
    layout = grid_for_frames(8)
    actual = (1774, 887)
    boxes = layout.boxes(actual)

    for i in range(len(boxes) - 1):
        same_row = (i + 1) % layout.cols != 0
        if same_row:
            assert boxes[i][2] == boxes[i + 1][0], f"第 {i} 与 {i+1} 格之间有缝或重叠"
    covered = sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes)
    assert covered == actual[0] * actual[1]


def test_actual_cell_is_recorded_not_the_nominal_512() -> None:
    """Manifest 记录名义 512 的话，process 就无法离线复现切帧。"""
    assert grid_for_frames(8).actual_cell((1774, 887)) == (443, 443)
    assert grid_for_frames(8).actual_cell((1536, 1024)) == (384, 512)


def test_aspect_mismatch_quantifies_the_distortion() -> None:
    """长短边比不符 → 单元格不再是正方形 → 下采样会把角色拉扁。"""
    layout = grid_for_frames(8)  # 期望 2048×1024，即 2:1
    assert aspect_mismatch(layout.size, (1774, 887)) == pytest.approx(0.0, abs=1e-3)
    # 实测第一次返回的 1536×1024 是 1.5:1，偏差 25%
    assert aspect_mismatch(layout.size, (1536, 1024)) == pytest.approx(0.25, abs=1e-3)


def test_out_of_range_index_is_rejected() -> None:
    with pytest.raises(GridLayoutError):
        grid_for_frames(4).cell_box(4)


def test_check_size_flags_each_constraint() -> None:
    assert any(v.constraint == "multiple_of_16" for v in check_size(1000, 1024))
    # 4:1 超过长短边比上限 3
    assert any(v.constraint == "aspect_ratio" for v in check_size(4096, 1024))
    assert any(v.constraint == "min_pixels" for v in check_size(512, 512))
    assert any(v.constraint == "max_pixels" for v in check_size(3840, 3840))
    assert any(v.constraint == "max_side" for v in check_size(3856, 1280))


def test_compliant_size_has_no_violations() -> None:
    assert check_size(2048, 1024) == []


# -- 补间网格：格子形状必须跟着关键帧 ----------------------------------------


def test_the_gap_grid_copies_the_keyframe_cell_shape() -> None:
    """关键帧格竖着，间隔格就不能是方的。

    实测弓手关键帧格 543×724，补间却按默认布局要了 1024×1024，
    模型于是按方格重新取景，把头和兜帽画成一团 —— 用户报的"形象不清晰"。
    """
    layout = layout_matching_cell(2, (543, 724))
    keyframe_aspect = 543 / 724
    assert abs(layout.cell[0] / layout.cell[1] - keyframe_aspect) < 0.05


def test_the_gap_grid_stays_near_the_keyframe_cell_size() -> None:
    """也不能一味取最大：3000px 的间隔格缩到 74px 画布，糊得比不匹配还厉害。"""
    layout = layout_matching_cell(2, (543, 724))
    assert 0.5 < layout.cell[1] / 724 < 2.0


def test_the_gap_grid_is_still_api_legal() -> None:
    for frames in (1, 2, 3, 4, 6):
        for cell in ((543, 724), (724, 543), (512, 512), (400, 1000)):
            layout = layout_matching_cell(frames, cell)
            assert not check_size(layout.width, layout.height), (frames, cell)


def test_a_degenerate_keyframe_cell_falls_back_instead_of_crashing() -> None:
    assert layout_matching_cell(2, (0, 0)).cell == layout_for_frames(2).cell
