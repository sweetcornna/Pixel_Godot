"""WFC 地图求解（PLAN §8.3）。

**用例几乎全靠合成邻接表。** 真实的 `grass_field` 推出的是对角矩阵，铺出来的
地图只有一种 tile —— 在那上面断言"每对相邻格都合法"会**恒真**，那是把检查写成
永真，和不查是一回事（§7.3 `key_color_residue` 的老毛病）。

所以这里的邻接表刻意做成**部分相容**：草与土之间靠 edge 过渡，水谁也接不上。
求解器必须真的用上过渡块，也必须真的排除掉接不上的那块。
"""

from __future__ import annotations

import pytest

from pixel_asset_forge.errors import ProcessingError
from pixel_asset_forge.planning.wfc import MAX_RESTARTS, generate_map

#: 部分相容的合成邻接表：grass →(edge)→ dirt，water 与谁都接不上。
RIGHT = {
    "grass": ["grass", "edge"],
    "edge": ["dirt"],
    "dirt": ["dirt"],
    "water": ["water"],
}
DOWN = {
    "grass": ["grass", "edge"],
    "edge": ["edge", "dirt"],
    "dirt": ["dirt"],
    "water": ["water"],
}


def illegal_pairs(rows, right, down):  # type: ignore[no-untyped-def]
    """地图里所有非法相邻对，水平与垂直分开数。"""
    horizontal = vertical = 0
    for y, row in enumerate(rows):
        for x, tile in enumerate(row):
            if x + 1 < len(row) and row[x + 1] not in right[tile]:
                horizontal += 1
            if y + 1 < len(rows) and rows[y + 1][x] not in down[tile]:
                vertical += 1
    return horizontal, vertical


# -- 合法性 ----------------------------------------------------------------


@pytest.mark.parametrize("seed", [0, 1, 7, 20260802])
def test_every_neighbour_pair_in_the_generated_map_is_legal(seed: int) -> None:
    result = generate_map(RIGHT, DOWN, width=10, height=7, seed=seed)
    assert illegal_pairs(result.rows, RIGHT, DOWN) == (0, 0)


def test_the_map_actually_uses_more_than_one_tile() -> None:
    """否则上面那条合法性断言就是恒真的 —— 没有判别力的检查等于没有检查。"""
    result = generate_map(RIGHT, DOWN, width=12, height=8, seed=20260802)
    assert len(result.tiles_used) >= 2


def test_a_tile_that_cannot_neighbour_anything_else_is_left_out() -> None:
    """water 只接自己，而网格是连通的：放一格水就整张图都得是水。

    12×8 里同时出现 water 和别的材质是**不可能**的 —— 求解器要么全水，
    要么一格不用。这条守的是"它没有偷偷放一格进去"。
    """
    used = generate_map(RIGHT, DOWN, width=12, height=8, seed=3).tiles_used
    assert not ({"water"} < set(used)), f"water 不该和别的材质共存：{used}"


# -- 确定性 ----------------------------------------------------------------


def test_the_same_seed_reproduces_the_map_cell_for_cell() -> None:
    a = generate_map(RIGHT, DOWN, width=9, height=6, seed=42)
    b = generate_map(RIGHT, DOWN, width=9, height=6, seed=42)
    assert a.rows == b.rows


def test_different_seeds_give_different_maps() -> None:
    """同一张图说明 seed 根本没接进去 —— 那样"可复现"是假的。"""
    maps = {generate_map(RIGHT, DOWN, width=9, height=6, seed=s).rows for s in range(6)}
    assert len(maps) > 1


# -- 撞上矛盾时绝不交货 ------------------------------------------------------


def test_an_unsolvable_adjacency_raises_instead_of_shipping_a_partial_map() -> None:
    """单个 tile，右边什么都接不上，地图却要两格宽 —— 无解。

    默默填格会让下游的合法性检查在真实产物上静默失效：检查还在跑、还在报通过，
    只是它查的东西已经被绕过去了。所以这里必须炸。
    """
    with pytest.raises(ProcessingError, match="无解"):
        generate_map({"a": []}, {"a": ["a"]}, width=2, height=1, seed=1)


def test_the_error_says_how_many_times_it_retried() -> None:
    """"重试过了"这件事要写在错误里，否则用户会以为换个 seed 就能好。"""
    with pytest.raises(ProcessingError, match=str(MAX_RESTARTS)):
        generate_map({"a": []}, {"a": ["a"]}, width=2, height=1, seed=1)


def test_a_one_by_one_map_is_still_solvable_for_that_same_tile() -> None:
    """无解是**尺寸相关**的，不是这块 tile 本身不能用 —— 别把话说过头。"""
    assert generate_map({"a": []}, {"a": ["a"]}, width=1, height=1, seed=1).rows == (
        ("a",),
    )


# -- 输入本身不合法 ----------------------------------------------------------


@pytest.mark.parametrize("size", [(0, 4), (4, 0), (-1, 4)])
def test_a_degenerate_size_is_refused(size) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ProcessingError, match="尺寸非法"):
        generate_map(RIGHT, DOWN, width=size[0], height=size[1], seed=1)


def test_an_empty_adjacency_table_is_refused() -> None:
    with pytest.raises(ProcessingError, match="空的"):
        generate_map({}, {}, width=4, height=4, seed=1)


def test_right_and_down_must_cover_the_same_tiles() -> None:
    """覆盖的 tile 对不上说明表本身是坏的，猜一个补上只会把错误藏起来。"""
    with pytest.raises(ProcessingError, match="不一致"):
        generate_map({"a": ["a"], "b": ["b"]}, {"a": ["a"]}, width=3, height=3, seed=1)


# -- 频率权重（§8.7）------------------------------------------------------


def tile_counts(rows):  # type: ignore[no-untyped-def]
    from collections import Counter

    return Counter(tile for row in rows for tile in row)


def test_no_weights_reproduces_the_unweighted_map_cell_for_cell() -> None:
    """不传权重必须逐格复现旧产物。

    ``random.choices`` 与 ``random.choice`` 消耗随机数的方式不同，所以即使权重
    全相等，"顺手都走 choices"也会让所有既有 seed 产出另一张地图 —— 那会静默
    作废"同 seed 同地图"的既有记录。这条测试盯的就是那个诱惑。
    """
    plain = generate_map(RIGHT, DOWN, width=9, height=6, seed=31)
    explicit_none = generate_map(RIGHT, DOWN, width=9, height=6, seed=31, weights=None)
    empty = generate_map(RIGHT, DOWN, width=9, height=6, seed=31, weights={})

    assert explicit_none.rows == plain.rows
    assert empty.rows == plain.rows


def test_weight_shifts_the_distribution_towards_the_favoured_tile() -> None:
    """把 dirt 的权重压到很低，它在同一批 seed 上必须明显变少。"""
    seeds = range(40)
    plain = sum(
        tile_counts(generate_map(RIGHT, DOWN, width=8, height=6, seed=s).rows)["dirt"]
        for s in seeds
    )
    starved = sum(
        tile_counts(
            generate_map(
                RIGHT, DOWN, width=8, height=6, seed=s,
                weights={"dirt": 0.01, "grass": 4.0},
            ).rows
        )["dirt"]
        for s in seeds
    )
    assert starved < plain, (starved, plain)


def test_weights_cannot_make_an_illegal_seam_legal() -> None:
    """权重只影响抽哪个，不参与相容性判定 —— water 接不上任何东西，

    给它天大的权重也不能让它和别的 tile 相邻。
    """
    for seed in range(12):
        result = generate_map(
            RIGHT, DOWN, width=8, height=6, seed=seed,
            weights={"water": 10_000.0, "grass": 0.001, "dirt": 0.001, "edge": 0.001},
        )
        assert illegal_pairs(result.rows, RIGHT, DOWN) == (0, 0)


def test_all_zero_weights_fall_back_to_uniform_instead_of_raising() -> None:
    """全零权重等于没表达偏好，不该让 random.choices 抛 ValueError。"""
    result = generate_map(
        RIGHT, DOWN, width=6, height=4, seed=5,
        weights={tile: 0.0 for tile in RIGHT},
    )
    assert illegal_pairs(result.rows, RIGHT, DOWN) == (0, 0)


def test_a_weight_for_an_unknown_tile_is_refused() -> None:
    """静默忽略会让"我调了权重却没反应"变成查不出来的问题。"""
    with pytest.raises(ProcessingError) as exc:
        generate_map(RIGHT, DOWN, width=4, height=4, seed=0, weights={"lava": 3.0})
    assert "lava" in exc.value.message


def test_a_negative_weight_is_refused() -> None:
    with pytest.raises(ProcessingError) as exc:
        generate_map(RIGHT, DOWN, width=4, height=4, seed=0, weights={"dirt": -1.0})
    assert "负数" in exc.value.message
