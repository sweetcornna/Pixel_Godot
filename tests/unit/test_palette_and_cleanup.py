"""调色板量化、像素清理、spritesheet 重组。"""

from __future__ import annotations

import numpy as np
import pytest

from pixel_asset_forge.errors import ProcessingError
from pixel_asset_forge.processing import (
    compose_spritesheet,
    contact_sheet,
    count_isolated,
    extract_palette,
    palette_overflow_ratio,
    quantize_frames,
    remove_isolated_pixels,
    save_gif,
    save_png,
)


def gradient_frame(size: int = 16, shift: int = 0) -> np.ndarray:
    arr = np.zeros((size, size, 4), dtype=np.uint8)
    for y in range(size):
        for x in range(size):
            arr[y, x] = ((x * 13 + shift) % 256, (y * 17) % 256, (x * y) % 256, 255)
    return arr


# -- 量化 -----------------------------------------------------------------


def test_quantization_respects_the_colour_budget() -> None:
    result = quantize_frames([gradient_frame(), gradient_frame(shift=40)], 16)
    assert result.color_count <= 16


def test_group_shares_one_palette() -> None:
    """逐帧各自量化会让同一块布料在相邻帧取到不同颜色，播放起来在闪烁。"""
    result = quantize_frames([gradient_frame(), gradient_frame(shift=40)], 12)
    per_frame = [set(extract_palette([f])) for f in result.frames]
    combined = set(result.colors)
    for colors in per_frame:
        assert colors <= combined
    assert len(combined) <= 12


def test_quantization_preserves_alpha() -> None:
    frame = gradient_frame()
    frame[0:4, 0:4, 3] = 0
    result = quantize_frames([frame], 8)
    assert (result.frames[0][0:4, 0:4, 3] == 0).all()
    assert (result.frames[0][0:4, 0:4, :3] == 0).all()


def test_quantization_is_deterministic() -> None:
    frames = [gradient_frame(), gradient_frame(shift=40)]
    a = quantize_frames(frames, 16)
    b = quantize_frames(frames, 16)
    assert a.colors == b.colors
    for x, y in zip(a.frames, b.frames, strict=True):
        assert np.array_equal(x, y)


def test_our_own_output_never_overflows_its_palette() -> None:
    """量化后的帧按定义就在调色板内 —— 这是回归守卫的基线。"""
    result = quantize_frames([gradient_frame()], 16)
    assert palette_overflow_ratio(result.frames, result.colors) == 0.0


def test_overflow_catches_colours_introduced_after_quantization() -> None:
    """量化之后再引入新颜色（后置 despill、带插值的翻转、手工改图）会被抓到。"""
    result = quantize_frames([gradient_frame()], 16)
    tampered = result.frames[0].copy()
    tampered[0, 0, :3] = (1, 2, 3)  # 调色板里没有的颜色
    tampered[0, 0, 3] = 255
    assert palette_overflow_ratio([tampered], result.colors) > 0


def test_quantization_error_grows_as_the_budget_shrinks() -> None:
    """这是**质量信息，不是合规判据** —— 它与色数强相关。"""
    frames = [gradient_frame()]
    tight = quantize_frames(frames, 4).quantization_error_ratio
    loose = quantize_frames(frames, 64).quantization_error_ratio
    assert tight > loose


def test_quantization_rejects_mismatched_sizes() -> None:
    with pytest.raises(ProcessingError):
        quantize_frames([gradient_frame(8), gradient_frame(16)], 8)


def test_quantization_rejects_fully_transparent_input() -> None:
    with pytest.raises(ProcessingError):
        quantize_frames([np.zeros((8, 8, 4), dtype=np.uint8)], 8)


def test_quantization_rejects_absurd_budget() -> None:
    with pytest.raises(ProcessingError):
        quantize_frames([gradient_frame()], 1)


# -- 像素清理 --------------------------------------------------------------


def test_isolated_pixel_is_removed() -> None:
    img = np.zeros((16, 16, 4), dtype=np.uint8)
    img[8, 8] = (200, 100, 50, 255)  # 四面无邻的孤点
    assert count_isolated(img) == 1
    assert remove_isolated_pixels(img)[8, 8, 3] == 0


def test_thin_line_endpoints_survive() -> None:
    """清理必须保守 —— 宁可留下几个可疑像素，也不要削掉剑尖。"""
    img = np.zeros((16, 16, 4), dtype=np.uint8)
    img[8, 4:12] = (200, 100, 50, 255)  # 一条细线
    out = remove_isolated_pixels(img)
    assert out[8, 4, 3] == 255, "线的端点被削掉了"
    assert out[8, 11, 3] == 255


def test_diagonal_neighbours_count() -> None:
    """像素画里 45° 的线本来就靠对角像素画出来，对角必须算相连。"""
    img = np.zeros((8, 8, 4), dtype=np.uint8)
    img[2, 2] = (1, 1, 1, 255)
    img[3, 3] = (1, 1, 1, 255)
    assert count_isolated(img) == 0


def test_cleanup_rejects_non_rgba() -> None:
    with pytest.raises(ProcessingError):
        remove_isolated_pixels(np.zeros((4, 4, 3), dtype=np.uint8))


# -- Spritesheet ----------------------------------------------------------


def test_spritesheet_layout_is_reconstructible_from_numbers_alone() -> None:
    """所有导出文件必须能仅凭 Manifest + frames/ 重建（ADR-001）。"""
    frames = [gradient_frame(8) for _ in range(6)]
    sheet, layout = compose_spritesheet(frames)

    assert sheet.shape == (8, 48, 4)
    assert layout.to_dict() == {
        "frame_width": 8, "frame_height": 8, "cols": 6, "rows": 1, "count": 6
    }
    for index in range(6):
        x0, y0, x1, y1 = layout.frame_box(index)
        assert np.array_equal(sheet[y0:y1, x0:x1], frames[index])


def test_spritesheet_supports_wrapping() -> None:
    sheet, layout = compose_spritesheet([gradient_frame(8) for _ in range(6)], cols=3)
    assert (layout.cols, layout.rows) == (3, 2)
    assert sheet.shape == (16, 24, 4)


def test_spritesheet_rejects_mismatched_frames() -> None:
    with pytest.raises(ProcessingError):
        compose_spritesheet([gradient_frame(8), gradient_frame(16)])


def test_frame_box_range_is_checked() -> None:
    _, layout = compose_spritesheet([gradient_frame(8)])
    with pytest.raises(ProcessingError):
        layout.frame_box(5)


def test_save_png_zeroes_transparent_rgb(tmp_path) -> None:
    from PIL import Image

    img = np.zeros((8, 8, 4), dtype=np.uint8)
    img[:, :, :3] = 200  # 透明但 RGB 非零
    img[4:6, 4:6, 3] = 255

    path = save_png(img, tmp_path / "out.png")
    loaded = np.array(Image.open(path))
    assert (loaded[loaded[:, :, 3] == 0][:, :3] == 0).all()


def test_save_gif_writes_a_preview(tmp_path) -> None:
    frames = [gradient_frame(8, shift=i * 20) for i in range(4)]
    path = save_gif(frames, tmp_path / "preview.gif", fps=10)
    assert path.exists() and path.stat().st_size > 0


def test_contact_sheet_holds_every_action(tmp_path) -> None:
    groups = {
        "idle_down": [gradient_frame(8) for _ in range(4)],
        "walk_down": [gradient_frame(8) for _ in range(8)],
    }
    sheet = contact_sheet(groups)
    assert sheet.shape[0] == 8 * 2  # 两行
    assert sheet.shape[1] == 8 * 8  # 最宽的动作决定列数
