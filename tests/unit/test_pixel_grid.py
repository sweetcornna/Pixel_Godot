"""像素网格吸附 —— 产出可用像素资产的前提。

模型画的是"块边缘发虚的像素画"：实测一张 1536×1024 产出有 14.8 万色，
块约 5px 但每块边缘是渐变过渡。不还原成块级像素而直接下采样，
采样点落在软边缘上，输出是读不出轮廓的泥。

但**探测出来的块必须先验证再采用**，否则会把本来就干净的 1:1 像素画毁掉。
"""

from __future__ import annotations

import numpy as np
import pytest

from pixel_asset_forge.processing.pixel_grid import snap_rgba_to_grid

# -- 探测结果必须先验证再采用 -----------------------------------------------


def clean_pixel_art(size: int = 64) -> np.ndarray:
    """本来就是 1:1 的干净像素画：每个像素就是一格美术像素。"""
    rng = np.random.default_rng(7)
    palette = np.array([
        [30, 30, 40], [220, 200, 180], [140, 90, 50], [60, 120, 70], [200, 60, 80],
    ], dtype=np.uint8)
    idx = rng.integers(0, len(palette), size=(size, size))
    rgb = palette[idx]
    return np.dstack([rgb, np.full((size, size), 255, np.uint8)])


def blocky_pixel_art(block: int = 6, cells: int = 12) -> np.ndarray:
    """块状像素画 —— 且**块边缘是发虚的**，这才是模型真实产出的样子。

    不加软边的话合成图只有调色板那几种颜色，色数压缩比这一关会先把它挡掉，
    根本走不到要测的还原验证；而真实产出因为软边有上万色。

    模糊系数 0.12 是**对着真实产出标定**的：gpt-image-2 的实测还原误差是 6.7，
    该系数下合成图是 11.8，同一量级。用 0.22 会得到 23，比真实产出糊得多，
    夹具自己就会被还原验证挡掉 —— 那测的就不是代码而是夹具了。
    """
    from PIL import Image, ImageFilter

    small = clean_pixel_art(cells)
    big = np.repeat(np.repeat(small, block, axis=0), block, axis=1)
    blurred = np.array(
        Image.fromarray(big[:, :, :3]).filter(ImageFilter.GaussianBlur(block * 0.12))
    )
    return np.dstack([blurred, big[:, :, 3]])


def test_reconstruction_error_separates_the_two_classes() -> None:
    from pixel_asset_forge.processing.pixel_grid import reconstruction_error

    # 真实产出的实测值是 6.7；阈值 20 对两类都有充分余量
    assert reconstruction_error(blocky_pixel_art(6), 6) < 20.0, "块是真的，还原该没什么损失"
    assert reconstruction_error(clean_pixel_art(64), 8) > 20.0, "块是臆想的，误差该很大"


def test_already_clean_pixel_art_is_left_alone() -> None:
    """**这条防的是资产被毁。**

    检测器的搜索范围从 MIN_BLOCK=3 起步，结构上答不出"块是 1"，
    对 1:1 像素画必然报一个 ≥3 的假块；而把 8 个真实像素糊成 1 个，
    色数当然也掉得多，压缩比那一关照样放行。

    实测：用户上传的 256×256 1:1 像素画被判成 8.5px 块、压成 30×30。
    """
    snap = snap_rgba_to_grid(clean_pixel_art(64))
    assert not snap.applied
    assert snap.image.shape[:2] == (64, 64), "干净像素画被改动了"
    assert "1:1" in snap.summary()


def test_genuinely_blocky_art_still_gets_snapped() -> None:
    """新增的验证不能把真正需要吸附的产出也一起挡掉。"""
    snap = snap_rgba_to_grid(blocky_pixel_art(6, 12))
    assert snap.applied
    assert snap.image.shape[:2] == (12, 12)


def test_an_explicit_block_size_skips_validation() -> None:
    """``process`` 离线重跑要能强制复现当时的块大小。"""
    snap = snap_rgba_to_grid(clean_pixel_art(64), block_size=8)
    assert snap.applied, "显式指定时不该被验证挡掉"


# -- 键控色残留：报比例，不报绝对数 ----------------------------------------


def sprite_with_enclosed_background(size: int = 64) -> np.ndarray:
    """一个"角色"，两腿之间围出一块背景 —— 色键的漫水填充清不掉它。"""
    key = (255, 0, 255)
    rgba = np.zeros((size, size, 4), dtype=np.uint8)
    rgba[10:50, 16:48] = (139, 90, 43, 255)          # 躯干
    rgba[36:50, 28:36] = (*key, 255)                 # 两腿之间的背景，仍不透明
    return rgba


def test_residue_is_reported_as_a_fraction_not_a_count() -> None:
    """绝对数随分辨率线性增长，作为信号毫无意义。

    实测健康资产（1024×1536 的 seed）删掉 34317 个像素 —— 听着吓人，
    其实大头是被角色围住的背景区域，只占前景的百分之几。
    一个在健康资产上报出五位数的告警，只会训练用户忽略告警。
    """
    from pixel_asset_forge.processing.pixel_grid import strip_key_residue

    small = sprite_with_enclosed_background(64)
    big = np.repeat(np.repeat(small, 4, axis=0), 4, axis=1)

    _, ratio_small = strip_key_residue(small, (255, 0, 255))
    _, ratio_big = strip_key_residue(big, (255, 0, 255))
    assert ratio_small == pytest.approx(ratio_big, abs=0.01), (
        "同一张图放大四倍，比例不该跟着变"
    )
    assert 0 < ratio_small < 1


def test_a_clean_sprite_reports_no_residue() -> None:
    from pixel_asset_forge.processing.pixel_grid import strip_key_residue

    clean = np.zeros((32, 32, 4), dtype=np.uint8)
    clean[8:24, 8:24] = (139, 90, 43, 255)
    _, ratio = strip_key_residue(clean, (255, 0, 255))
    assert ratio == 0.0


# -- 验证必须与吸附用同一种取色方式 ----------------------------------------


def keyed_blocky_art(block: int = 6, cells: int = 16) -> np.ndarray:
    """块状像素画 + 键控后的透明背景 —— 实际进入吸附的就是这种形态。"""
    art = blocky_pixel_art(block, cells)
    rgba = art.copy()
    # 四周一圈挖成透明，模拟键控后的背景
    edge = block * 2
    rgba[:edge, :, 3] = 0
    rgba[-edge:, :, 3] = 0
    rgba[:, :edge, 3] = 0
    rgba[:, -edge:, 3] = 0
    return rgba


def test_validation_uses_the_same_block_sampling_as_the_snap() -> None:
    """**这条防的是整条像素网格还原被静默关掉。**

    验证若用最近邻取块的角点，量到的是"角点采样有多差"而不是"块是不是真的" ——
    角点正是软边缘最重的地方。实测同一张模型产出：最近邻还原误差 27.9
    （越过阈值 20，块被误判成臆想的），块中心中位还原只有 5 左右。
    """
    from pixel_asset_forge.processing.pixel_grid import reconstruction_error

    art = keyed_blocky_art(6, 16)
    assert reconstruction_error(art, 6) < 20.0, "真块被判成臆想的了"
    assert snap_rgba_to_grid(art).applied


def test_validation_ignores_the_transparent_background() -> None:
    """RGB 版的块中位会把透明区的键控色也算进去，跨角色边缘的块因此被污染 ——
    误差被边缘完全主导，本该吸附的产出被判成 1:1。
    """
    from pixel_asset_forge.processing.pixel_grid import reconstruction_error

    art = keyed_blocky_art(6, 16)
    polluted = art.copy()
    polluted[art[:, :, 3] == 0, :3] = (255, 0, 255)  # 透明区填满洋红

    assert reconstruction_error(polluted, 6) == pytest.approx(
        reconstruction_error(art, 6), abs=1.0
    ), "透明区的颜色不该影响验证结果"
