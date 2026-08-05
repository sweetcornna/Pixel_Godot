"""过渡 tile 的角落地形像素推导（PLAN §8.5）。

反例必须真的改变被检查的事实：A 把左上角的像素从草换成土，最近地形随之从
grass 变成 dirt；C 用与两个 base 都不像的纹理，把四角都推到 unknown。
"""

from __future__ import annotations

import numpy as np
import pytest

from pixel_asset_forge.processing.tile import process_tiles
from pixel_asset_forge.validation.terrain import (
    TERRAIN_DISTANCE_MAX,
    derive_terrain_corners,
    quadrant_distance,
)

SIZE = 16
GRASS = (82, 132, 68)
DIRT = (146, 92, 48)


def texture(base: tuple[int, int, int], grain: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    rgb = np.clip(
        np.array(base)[None, None, :]
        + rng.integers(-grain, grain + 1, (SIZE, SIZE, 1)),
        0,
        255,
    ).astype(np.uint8)
    alpha = np.full((SIZE, SIZE, 1), 255, dtype=np.uint8)
    return np.concatenate([rgb, alpha], axis=2)


def independently_sampled_texture(size: int, seed: int) -> np.ndarray:
    """给真实共享调色板量化链使用的独立同材质 RGB 样本。"""
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 14, size=(size, size, 3))
    return np.clip(np.array(GRASS) + noise, 0, 255).astype(np.uint8)


def tile(corners: list[np.ndarray]) -> np.ndarray:
    return np.concatenate(
        [np.concatenate(corners[:2], axis=1), np.concatenate(corners[2:], axis=1)],
        axis=0,
    )


@pytest.fixture
def family() -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], list[np.ndarray]]:
    grass = texture(GRASS, 22, 1)
    dirt = texture(DIRT, 22, 2)
    true_corners = [
        texture(GRASS, 22, 11),
        texture(GRASS, 22, 12),
        texture(DIRT, 22, 13),
        texture(DIRT, 22, 14),
    ]
    tiles = {
        "grass_base": tile([grass] * 4),
        "dirt_base": tile([dirt] * 4),
        "transition": tile(true_corners),
    }
    return tiles, {"grass": tiles["grass_base"], "dirt": tiles["dirt_base"]}, true_corners


def test_true_transition_has_a_measured_margin(family) -> None:  # type: ignore[no-untyped-def]
    tiles, bases, _corners = family
    result = derive_terrain_corners(tiles, bases)
    transition = result.tiles["transition"]

    assert transition.corners == ("grass", "grass", "dirt", "dirt")
    assert transition.distances == pytest.approx((1.367188, 1.128906, 1.472656, 1.261719))
    assert max(transition.distances) < TERRAIN_DISTANCE_MAX * 0.125


def test_replacing_one_corner_really_changes_the_measured_fact(family) -> None:  # type: ignore[no-untyped-def]
    tiles, bases, true_corners = family
    before = derive_terrain_corners(tiles, bases).tiles["transition"]
    changed = dict(tiles)
    changed["transition"] = tile([texture(DIRT, 22, 2), *true_corners[1:]])
    after = derive_terrain_corners(changed, bases).tiles["transition"]

    assert before.corners[0] == "grass" and before.distances[0] == pytest.approx(1.367188)
    assert after.corners[0] == "dirt" and after.distances[0] == 0.0
    grass_quadrant = bases["grass"][:SIZE, :SIZE]
    assert quadrant_distance(texture(DIRT, 22, 2), grass_quadrant) == 52.0


def test_an_outlier_is_unknown_instead_of_being_forced_to_the_nearest_base(family) -> None:  # type: ignore[no-untyped-def]
    tiles, bases, _corners = family
    rng = np.random.default_rng(99)
    outlier = [
        np.concatenate(
            [
                rng.integers(185, 256, (SIZE, SIZE, 3), dtype=np.uint8),
                np.full((SIZE, SIZE, 1), 255, dtype=np.uint8),
            ],
            axis=2,
        )
        for _ in range(4)
    ]
    changed = dict(tiles)
    changed["transition"] = tile(outlier)

    measured = derive_terrain_corners(changed, bases).tiles["transition"]
    assert measured.corners == (None, None, None, None)
    assert measured.distances == pytest.approx(
        (145.074219, 144.152344, 145.722656, 144.183594), abs=0.000001
    )
    assert min(measured.distances) > TERRAIN_DISTANCE_MAX * 12


def test_distance_has_no_failure_dependent_denominator() -> None:
    """把错材质的噪声放大，不会像 seam ratio 那样被自己的颗粒度稀释掉。"""
    gaps = [
        quadrant_distance(texture(GRASS, grain, 21), texture(DIRT, grain, 22))
        for grain in (8, 22, 40)
    ]
    assert min(gaps) > TERRAIN_DISTANCE_MAX * 3
    assert max(gaps) - min(gaps) < 3, gaps


def test_healthy_distance_has_cross_configuration_margin_after_shared_quantization() -> None:
    """合法尺寸与色数不能改变“同一种材质”的含义。"""
    readings: dict[tuple[int, int], float] = {}
    for tile_size in (16, 24, 32):
        sources = {
            "grass_base": independently_sampled_texture(tile_size, 1),
            "grass_corner": independently_sampled_texture(tile_size, 11),
        }
        for max_colors in (16, 64, 128, 256):
            processed = process_tiles(
                sources,
                tile_size=(tile_size, tile_size),
                max_colors=max_colors,
            )
            midpoint = tile_size // 2
            base = processed.tiles["grass_base"][:midpoint, :midpoint]
            corner = processed.tiles["grass_corner"][:midpoint, :midpoint]
            readings[(tile_size, max_colors)] = quadrant_distance(base, corner)

    assert readings == pytest.approx(
        {
            (16, 16): 2.554688,
            (16, 64): 2.552083,
            (16, 128): 2.708333,
            (16, 256): 2.755208,
            (24, 16): 1.479167,
            (24, 64): 2.335648,
            (24, 128): 2.532407,
            (24, 256): 2.784722,
            (32, 16): 0.816406,
            (32, 64): 1.535156,
            (32, 128): 1.638672,
            (32, 256): 1.611979,
        }
    )
    assert max(readings.values()) < TERRAIN_DISTANCE_MAX * 0.25, readings


def test_joint_rgb_wasserstein_closes_the_channel_marginal_blind_spot() -> None:
    """边缘分布相同但 RGB 组合完全不同，必须由第二条判据抓住。"""
    red_green = np.empty((SIZE, SIZE, 3), dtype=np.uint8)
    red_green[: SIZE // 2] = (255, 0, 0)
    red_green[SIZE // 2 :] = (0, 255, 0)
    yellow_black = np.empty_like(red_green)
    yellow_black[: SIZE // 2] = (255, 255, 0)
    yellow_black[SIZE // 2 :] = (0, 0, 0)

    # 两组的 R/G/B 边缘分布逐通道完全相同；127.5 全来自联合 RGB 投影。
    assert quadrant_distance(red_green, yellow_black) == 127.5
    assert quadrant_distance(
        np.repeat(np.repeat(red_green, 2, axis=0), 2, axis=1),
        np.repeat(np.repeat(yellow_black, 2, axis=0), 2, axis=1),
    ) == 127.5


def test_pixel_order_is_intentionally_ignored_but_normal_terrains_still_separate() -> None:
    grass = texture(GRASS, 22, 1)
    shuffled = grass.reshape(-1, 4)[
        np.random.default_rng(7).permutation(SIZE * SIZE)
    ].reshape(grass.shape)

    assert quadrant_distance(grass, shuffled) == 0.0
    assert quadrant_distance(grass, texture(GRASS, 22, 11)) == pytest.approx(1.367188)
    assert quadrant_distance(grass, texture(DIRT, 22, 2)) == 52.0
