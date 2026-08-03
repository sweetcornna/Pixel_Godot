"""从像素推出"哪块 tile 能挨着哪块"（PLAN §8.2）。

**不调用 API**：这一步只读已经处理好的 tile，是纯确定性推导。

判据在 :mod:`.seamless` 里，两条各抓一种失败（缺一不可，理由见那边的
:func:`~.seamless.edge_color_gap`）。这里只负责把判据铺到每一对上、
把结果收成一张表。

**关系不对称。** ``A 右接 B`` 比的是 A 的末列与 B 的首列，``B 右接 A`` 比的是
B 的末列与 A 的首列 —— 两件事。只有 ``A 右接 B ⟺ B 左接 A`` 才是同一件事。
所以这里只算 ``right`` 与 ``down``，``left`` / ``up`` 由转置得出：存四份等于给
同一个事实留四个可以各自漂移的副本。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .seamless import edge_color_gap, pair_seam_ratio

#: 邻接判据的工程默认值。**未用真实 tile 校准**（同 §9.1 的口径）。
#:
#: 合成 tile 实测分离度：同材质不同实例 0.97 / 7.19（都通过）；边列均值相同但
#: 结构错位 60.00（被接缝比抓）；草接水在噪声颗粒度 60 时 1.88 / 102.16 ——
#: 接缝比已被颗粒度稀释到阈值下方，只剩色差判据抓得住（PLAN §8.2）。
ADJACENCY_SEAM_RATIO_MAX = 3.0
ADJACENCY_EDGE_COLOR_GAP_MAX = 28.0

#: Manifest 里实际存下来的两个方向。``left`` / ``up`` 是它们的转置，不另存。
STORED_DIRECTIONS = ("right", "down")


@dataclass(frozen=True)
class PairVerdict:
    """一对 tile 在一个方向上的判定与两个测量值。"""

    seam_ratio: float
    color_gap: float
    seam_max: float
    gap_max: float

    @property
    def compatible(self) -> bool:
        """任一判据超阈值即判不相容。"""
        return self.seam_ratio <= self.seam_max and self.color_gap <= self.gap_max


@dataclass(frozen=True)
class AdjacencyResult:
    """整套 tile 的邻接推导结果。"""

    right: dict[str, list[str]]
    down: dict[str, list[str]]
    seam_ratio_max: float
    edge_color_gap_max: float

    def neighbours(self, tile_id: str, direction: str) -> list[str]:
        """某块 tile 在某个方向上允许的邻居。四个方向都答得出。

        ``left`` / ``up`` 现算：``A ∈ left[B] ⟺ B ∈ right[A]``。
        """
        if direction == "right":
            return list(self.right.get(tile_id, ()))
        if direction == "down":
            return list(self.down.get(tile_id, ()))
        stored = self.right if direction == "left" else self.down
        if direction not in ("left", "up"):
            raise ValueError(
                f"未知方向：{direction}。可选：right / down / left / up"
            )
        return sorted(other for other, allowed in stored.items() if tile_id in allowed)


def judge_pair(
    first: np.ndarray,
    second: np.ndarray,
    axis: str,
    *,
    seam_max: float = ADJACENCY_SEAM_RATIO_MAX,
    gap_max: float = ADJACENCY_EDGE_COLOR_GAP_MAX,
) -> PairVerdict:
    """``first`` 接 ``second`` 是否拼得上。``axis`` 取 horizontal / vertical。"""
    return PairVerdict(
        seam_ratio=pair_seam_ratio(first, second, axis),  # type: ignore[arg-type]
        color_gap=edge_color_gap(first, second, axis),  # type: ignore[arg-type]
        seam_max=seam_max,
        gap_max=gap_max,
    )


def derive_adjacency(
    tiles: dict[str, np.ndarray],
    *,
    seam_max: float = ADJACENCY_SEAM_RATIO_MAX,
    gap_max: float = ADJACENCY_EDGE_COLOR_GAP_MAX,
) -> AdjacencyResult:
    """把每一对 tile 在两个方向上各判一次，收成一张表。

    ``tile_id`` 按字典序遍历，结果里的邻居列表也排序 —— 表要进 Manifest，
    顺序不定就不幂等了（同 :func:`..processing.tile.process_tiles` 的理由）。

    **自配对不跳过**：``A 右接 A`` 就是 8.1 的水平无缝判定，对角线正该由它填。
    """
    ordered = sorted(tiles)
    right: dict[str, list[str]] = {}
    down: dict[str, list[str]] = {}

    for first in ordered:
        for axis, table in (("horizontal", right), ("vertical", down)):
            table[first] = [
                second
                for second in ordered
                if judge_pair(
                    tiles[first], tiles[second], axis,
                    seam_max=seam_max, gap_max=gap_max,
                ).compatible
            ]

    return AdjacencyResult(
        right=right, down=down,
        seam_ratio_max=seam_max, edge_color_gap_max=gap_max,
    )
