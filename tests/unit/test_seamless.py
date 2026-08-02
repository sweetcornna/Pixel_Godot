"""无缝平铺测量的判别力。

这个文件的重点不是"可平铺的 tile 能通过"，而是**两种失败各自被哪一条判据抓到**。
只验通过侧的检查没有判别力（Sprint 7 §7.3 的 `key_color_residue` 误报就是这么来的）。
"""

from __future__ import annotations

import numpy as np
import pytest

from pixel_asset_forge.validation.seamless import (
    border_deviation,
    measure_seamless,
    seam_ratio,
)

SIZE = 32


def _rng() -> np.random.Generator:
    # 固定种子：测量是纯函数，测试就不该有随机性。
    return np.random.default_rng(20260802)


def seamless_ground() -> np.ndarray:
    """可平铺的噪点地面。

    均匀噪声天然可平铺：接缝处的相邻差异与内部的相邻差异同分布 ——
    它不是"边缘恰好对上"，而是"哪里都一样"，这正是地面 tile 想要的性质。
    """
    base = np.array([90, 130, 70], dtype=np.float64)
    noise = _rng().normal(0, 12, size=(SIZE, SIZE, 3))
    return np.clip(base + noise, 0, 255).astype(np.uint8)


def gradient_tile() -> np.ndarray:
    """左右渐变 —— 失败形态一：对边接不上。"""
    ramp = np.linspace(30, 220, SIZE)
    return np.repeat(
        np.repeat(ramp[None, :, None], SIZE, axis=0), 3, axis=2
    ).astype(np.uint8)


def framed_tile(border: int = 2) -> np.ndarray:
    """带深色边框 —— 失败形态二：接缝连续，但平铺后是规则网格线。"""
    tile = seamless_ground()
    tile[:border, :] = tile[-border:, :] = (20, 25, 18)
    tile[:, :border] = tile[:, -border:] = (20, 25, 18)
    return tile


def vignette_tile() -> np.ndarray:
    """四角压暗的暗角 —— 失败形态二的柔和版，边框判据同样该抓到。"""
    tile = seamless_ground().astype(np.float64)
    axis = np.linspace(-1, 1, SIZE)
    radius = np.sqrt(axis[:, None] ** 2 + axis[None, :] ** 2)
    return np.clip(tile * (1 - 0.75 * np.clip(radius, 0, 1))[:, :, None], 0, 255).astype(
        np.uint8
    )


# -- 通过侧 ---------------------------------------------------------------


def test_seamless_ground_passes_both_measurements() -> None:
    measured = measure_seamless(seamless_ground())
    assert measured.worst_seam_ratio < 2.0
    assert measured.border_deviation < 1.0


def test_flat_colour_tile_is_seamless() -> None:
    """纯色是可平铺的极端情形。内部差异为 0，判据不能因此炸掉。"""
    flat = np.full((SIZE, SIZE, 3), (120, 90, 60), dtype=np.uint8)
    measured = measure_seamless(flat)
    assert measured.worst_seam_ratio < 1.0
    assert measured.border_deviation < 1.0


def test_alpha_channel_is_ignored() -> None:
    """tile 满幅不透明，带不带 alpha 都该测出同一组数。"""
    rgb = seamless_ground()
    rgba = np.dstack([rgb, np.full((SIZE, SIZE), 255, dtype=np.uint8)])
    assert measure_seamless(rgba) == measure_seamless(rgb)


# -- 失败形态一：对边接不上 -------------------------------------------------


def test_gradient_tile_is_caught_by_seam_ratio() -> None:
    tile = gradient_tile()
    assert seam_ratio(tile, "horizontal") > 10.0
    # 竖直方向每一行都相同，本来就接得上 —— 别把它一起判死。
    assert seam_ratio(tile, "vertical") < 1.0


def test_gradient_tile_slips_past_the_border_measurement() -> None:
    """记录**为什么需要两条判据**：渐变的边框统计与内部无异。

    左缘暗、右缘亮，一圈平均下来正好落在内部均值附近 —— 边框判据对它无能为力。
    """
    assert border_deviation(gradient_tile()) < 1.0


# -- 失败形态二：带边框 / 暗角 ---------------------------------------------


@pytest.mark.parametrize("tile_factory", [framed_tile, vignette_tile])
def test_framed_and_vignette_tiles_are_caught_by_border_deviation(tile_factory) -> None:
    assert border_deviation(tile_factory()) > 2.0


def test_framed_tile_slips_past_the_seam_measurement() -> None:
    """这条是本模块存在两条判据的**全部理由**，删掉它等于删掉设计依据。

    带边框的 tile 接缝处是"边框接边框"，两边一样暗，接缝判据恒判通过 ——
    而它平铺后恰恰是最刺眼的规则网格线。
    """
    tile = framed_tile()
    assert seam_ratio(tile, "horizontal") < 2.0
    assert seam_ratio(tile, "vertical") < 2.0
    assert border_deviation(tile) > 2.0


# -- 形状与退化输入 ---------------------------------------------------------


def test_non_image_input_is_rejected() -> None:
    with pytest.raises(ValueError):
        measure_seamless(np.zeros((SIZE, SIZE), dtype=np.uint8))


def test_tile_smaller_than_the_border_ring_does_not_crash() -> None:
    assert border_deviation(np.zeros((2, 2, 3), dtype=np.uint8)) == 0.0
