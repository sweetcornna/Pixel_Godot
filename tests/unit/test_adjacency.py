"""邻接判据与推导（PLAN §8.2）。

判据是**两条**，缺一不可，理由与 8.1 同源：`pair_seam_ratio` 的分母是颗粒度，
而高频噪声纹理的颗粒度本来就大 —— 两块材质完全不同的高噪 tile 接缝差异虽大，
除以同样大的颗粒度后照样落在阈值下方。拿会掩盖失败的量去归一化，等于没查。

所以下面的反例必须覆盖两种失败，且**各自只被一条判据抓到**：只验通过侧的检查
没有判别力（§7.3 的 `key_color_residue` 误报就是这么来的）。
"""

from __future__ import annotations

import numpy as np
import pytest

from pixel_asset_forge.validation.adjacency import (
    ADJACENCY_EDGE_COLOR_GAP_MAX,
    ADJACENCY_SEAM_RATIO_MAX,
    derive_adjacency,
    judge_pair,
)
from pixel_asset_forge.validation.seamless import (
    edge_color_gap,
    pair_seam_ratio,
    seam_ratio,
)

SIZE = 32


def noise(base: tuple[int, int, int], grain: int, seed: int) -> np.ndarray:
    """一块可平铺的噪点地面：均匀噪声天然可平铺（接缝处与内部同分布）。"""
    rng = np.random.default_rng(seed)
    out = np.zeros((SIZE, SIZE, 4), np.uint8)
    out[:, :, 3] = 255
    for channel in range(3):
        out[:, :, channel] = np.clip(
            base[channel] + rng.integers(-grain, grain, (SIZE, SIZE)), 0, 255
        )
    return out


def hstripes(base: tuple[int, int, int], phase: int) -> np.ndarray:
    """横条纹：只随行变化。

    两个性质是刻意的：沿水平方向内部相邻列完全相同（颗粒度为 0），
    且**任意一列的均值都恒等于 base** —— 于是错位拼接时色差判据读数为 0，
    只有接缝判据能抓到。
    """
    out = np.zeros((SIZE, SIZE, 4), np.uint8)
    out[:, :, 3] = 255
    band = ((np.arange(SIZE) + phase) // 2) % 2
    for channel in range(3):
        out[:, :, channel] = np.clip(
            base[channel] + np.where(band, 30, -30), 0, 255
        )[:, None]
    return out


GRASS = (90, 130, 70)
WATER = (60, 90, 180)


# -- 恒等式：自配对退化回 8.1 -----------------------------------------------
#
# 这条是本切最有用的一根桩：邻接矩阵的对角线因此与 8.1 已验过的无缝判定同源。
# `seam_ratio` 现在直接委托给 `pair_seam_ratio`，相等是构造出来的 —— 这条测试
# 守的是"以后别把它们各写一遍"。


@pytest.mark.parametrize("axis", ["horizontal", "vertical"])
@pytest.mark.parametrize("grain", [12, 28, 60])
def test_self_pairing_is_bit_for_bit_the_seam_ratio_from_81(axis, grain) -> None:  # type: ignore[no-untyped-def]
    tile = noise(GRASS, grain, seed=3)
    assert pair_seam_ratio(tile, tile, axis) == seam_ratio(tile, axis)


def test_self_pairing_holds_for_a_tile_that_fails_the_seam_gate_too() -> None:
    """恒等式不能只在通过侧成立 —— 否则对角线在失败侧就对不上了。"""
    gradient = np.zeros((SIZE, SIZE, 4), np.uint8)
    gradient[:, :, 3] = 255
    gradient[:, :, :3] = np.linspace(0, 255, SIZE, dtype=np.uint8)[None, :, None]

    assert seam_ratio(gradient, "horizontal") > ADJACENCY_SEAM_RATIO_MAX
    assert pair_seam_ratio(gradient, gradient, "horizontal") == seam_ratio(
        gradient, "horizontal"
    )


# -- 通过侧 ----------------------------------------------------------------


def test_two_instances_of_the_same_material_are_compatible() -> None:
    """同材质不同实例必须判相容，否则整张表退化成"只能接自己"就没有信息量了。"""
    verdict = judge_pair(noise(GRASS, 28, seed=1), noise(GRASS, 28, seed=2), "horizontal")
    assert verdict.compatible
    # 两条都要留出余量，贴着线通过等于随机数决定结果。
    assert verdict.seam_ratio < ADJACENCY_SEAM_RATIO_MAX / 2
    assert verdict.color_gap < ADJACENCY_EDGE_COLOR_GAP_MAX / 2


# -- 失败侧一：结构对不上，色差读数为 0 -------------------------------------


def test_misaligned_structure_is_caught_by_the_seam_ratio_alone() -> None:
    """边列均值完全相同、只有结构错位 —— 色差判据对它恒判通过。"""
    verdict = judge_pair(hstripes(GRASS, 0), hstripes(GRASS, 2), "horizontal")
    assert not verdict.compatible
    assert verdict.seam_ratio > ADJACENCY_SEAM_RATIO_MAX
    assert verdict.color_gap == 0.0, "这个反例的全部意义就是色差判据读不到它"


# -- 失败侧二：材质换了，接缝比被颗粒度稀释 -----------------------------------


@pytest.mark.parametrize("grain", [60, 90])
def test_a_material_change_survives_only_because_of_the_color_gap(grain) -> None:  # type: ignore[no-untyped-def]
    """噪声一大，接缝比就掉到阈值下方 —— 这就是第二条判据存在的全部理由。

    实测颗粒度 60 时接缝比 1.88、90 时 1.37，两者都远低于阈值 3；
    而色差判据读到 100 以上。单靠接缝比会把"草接水"判成相容。
    """
    verdict = judge_pair(noise(GRASS, grain, seed=4), noise(WATER, grain, seed=5), "horizontal")
    assert verdict.seam_ratio <= ADJACENCY_SEAM_RATIO_MAX, (
        "前提失效：这个反例要的就是接缝比判不出来"
    )
    assert verdict.color_gap > ADJACENCY_EDGE_COLOR_GAP_MAX
    assert not verdict.compatible


def test_the_color_gap_is_not_normalised_by_anything() -> None:
    """颗粒度翻几倍，色差读数不该跟着动 —— 它一旦被归一化就会重蹈 8.1 的覆辙。"""
    gaps = [
        edge_color_gap(noise(GRASS, grain, seed=6), noise(WATER, grain, seed=7), "horizontal")
        for grain in (20, 60, 100)
    ]
    assert min(gaps) > ADJACENCY_EDGE_COLOR_GAP_MAX * 2
    assert max(gaps) - min(gaps) < 30, f"色差读数随颗粒度漂了：{gaps}"


# -- 推导出来的表 -----------------------------------------------------------


@pytest.fixture
def family() -> dict[str, np.ndarray]:
    """两块同材质 + 一块异材质。表里该出现的关系一目了然。"""
    return {
        "grass_a": noise(GRASS, 28, seed=11),
        "grass_b": noise(GRASS, 28, seed=12),
        "water": noise(WATER, 28, seed=13),
    }


def test_the_derived_table_groups_tiles_by_material(family) -> None:  # type: ignore[no-untyped-def]
    table = derive_adjacency(family)
    assert table.right["grass_a"] == ["grass_a", "grass_b"]
    assert table.right["grass_b"] == ["grass_a", "grass_b"]
    assert table.right["water"] == ["water"]


def test_every_tile_is_adjacent_to_itself(family) -> None:  # type: ignore[no-untyped-def]
    """对角线全真 —— 这几块都是可平铺的噪点地面，8.1 的无缝判定也会说通过。"""
    table = derive_adjacency(family)
    for tile_id in family:
        assert tile_id in table.right[tile_id]
        assert tile_id in table.down[tile_id]


def test_left_and_up_are_the_transpose_of_right_and_down(family) -> None:  # type: ignore[no-untyped-def]
    """``B ∈ right[A] ⟺ A ∈ left[B]``。存两个方向、现算另两个，全靠这条成立。"""
    table = derive_adjacency(family)
    for a in family:
        for b in family:
            assert (b in table.right[a]) == (a in table.neighbours(b, "left"))
            assert (b in table.down[a]) == (a in table.neighbours(b, "up"))


def test_the_derivation_is_deterministic_and_sorted(family) -> None:  # type: ignore[no-untyped-def]
    """表要进 Manifest，顺序不定就不幂等了。"""
    first, second = derive_adjacency(family), derive_adjacency(family)
    assert first == second
    for row in (*first.right.values(), *first.down.values()):
        assert row == sorted(row)


def test_an_unknown_direction_is_refused(family) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="未知方向"):
        derive_adjacency(family).neighbours("grass_a", "diagonal")
