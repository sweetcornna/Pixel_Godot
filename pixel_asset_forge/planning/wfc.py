"""Wave Function Collapse，Simple Tiled Model（PLAN §8.3）。

**不调用 API**：输入是 8.2 推出的邻接表，输出是一张每对相邻格都合法的地图。

选 WFC 而不是"规则地图生成"，是因为规则那条路需要的信息我们没有：它要人写
"草原占 60%、河流从北向南"这类**意图**，而邻接表只说得出"谁能挨着谁"。
WFC 恰好只吃这一样东西，而且它涵盖了朴素规则法 —— 把每格候选集初始化成全集、
按约束收敛，正是规则能做的事的超集。

**撞上矛盾时绝不交货。** 不留空格、不"就近挑一个"填上：换 seed 重试，
重试用尽就抛错。默默填格会让下游那条合法性检查在真实产物上静默失效 ——
检查还在跑、还在报通过，只是它查的东西已经被绕过去了。
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from ..errors import ProcessingError

#: 撞上矛盾后换 seed 重试的次数。超过就认为这套邻接表在这个尺寸上无解。
#:
#: 取小值是刻意的：真正有解的邻接表极少需要重试（约束传播已经把大部分死路剪掉
#: 了），而无解的邻接表重试一万次也还是无解 —— 那只会把"立刻说不行"拖成
#: "卡住很久之后说不行"。
MAX_RESTARTS = 0  # CI 自检：pytest 该抓住这个行为改变


@dataclass(frozen=True)
class TileMap:
    """一张铺好的地图。``rows[y][x]`` 是那一格的 ``tile_id``。"""

    rows: tuple[tuple[str, ...], ...]
    seed: int

    @property
    def width(self) -> int:
        return len(self.rows[0])

    @property
    def height(self) -> int:
        return len(self.rows)

    @property
    def tiles_used(self) -> list[str]:
        return sorted({tile for row in self.rows for tile in row})


def _transpose(table: dict[str, list[str]]) -> dict[str, set[str]]:
    """``right`` → ``left``（``down`` → ``up`` 同理）。

    ``B ∈ right[A] ⟺ A ∈ left[B]`` —— 传播要两个方向都用得上，而 Manifest 只存
    一个方向（存两份等于给同一个事实留两个会漂移的副本，见 §8.2）。
    """
    out: dict[str, set[str]] = {key: set() for key in table}
    for first, allowed in table.items():
        for second in allowed:
            out.setdefault(second, set()).add(first)
    return out


class _Solver:
    """一次求解尝试。撞上矛盾就整体作废，由调用方换 seed 重来。"""

    def __init__(
        self,
        tiles: list[str],
        right: dict[str, list[str]],
        down: dict[str, list[str]],
        width: int,
        height: int,
        rng: random.Random,
    ) -> None:
        self.tiles = tiles
        self.width, self.height = width, height
        self.rng = rng
        # 四个方向的允许集：(dx, dy) → {tile: 那个方向上允许的邻居}
        self.allowed: dict[tuple[int, int], dict[str, set[str]]] = {
            (1, 0): {key: set(value) for key, value in right.items()},
            (0, 1): {key: set(value) for key, value in down.items()},
            (-1, 0): _transpose(right),
            (0, -1): _transpose(down),
        }
        self.domains: list[list[set[str]]] = [
            [set(tiles) for _ in range(width)] for _ in range(height)
        ]

    def _neighbours(self, x: int, y: int):  # type: ignore[no-untyped-def]
        for dx, dy in ((1, 0), (0, 1), (-1, 0), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < self.width and 0 <= ny < self.height:
                yield (dx, dy), nx, ny

    def _propagate(self, start: tuple[int, int]) -> bool:
        """从一格出发做弧相容收敛。返回是否仍然可解。"""
        stack = [start]
        while stack:
            x, y = stack.pop()
            for offset, nx, ny in self._neighbours(x, y):
                table = self.allowed[offset]
                # 当前格的候选集能允许的、邻居的取值全集
                permitted: set[str] = set()
                for option in self.domains[y][x]:
                    permitted |= table.get(option, set())
                pruned = self.domains[ny][nx] & permitted
                if not pruned:
                    return False
                if pruned != self.domains[ny][nx]:
                    self.domains[ny][nx] = pruned
                    stack.append((nx, ny))
        return True

    def _lowest_entropy(self) -> tuple[int, int] | None:
        """候选最少的未定格。并列时随机取一个 —— 否则整张图会带上扫描顺序的纹路。"""
        best = 0
        candidates: list[tuple[int, int]] = []
        for y in range(self.height):
            for x in range(self.width):
                size = len(self.domains[y][x])
                if size <= 1:
                    continue
                if not candidates or size < best:
                    best, candidates = size, [(x, y)]
                elif size == best:
                    candidates.append((x, y))
        return self.rng.choice(candidates) if candidates else None

    def run(self) -> list[list[str]] | None:
        """求解。撞上矛盾返回 ``None``，不返回半成品。"""
        for y in range(self.height):
            for x in range(self.width):
                if not self._propagate((x, y)):
                    return None

        while (cell := self._lowest_entropy()) is not None:
            x, y = cell
            # 排序后再抽：集合的迭代顺序不保证稳定，不排序就谈不上"同 seed 同结果"。
            self.domains[y][x] = {self.rng.choice(sorted(self.domains[y][x]))}
            if not self._propagate(cell):
                return None

        if any(len(cell) != 1 for row in self.domains for cell in row):
            return None
        return [[next(iter(cell)) for cell in row] for row in self.domains]


def generate_map(
    right: dict[str, list[str]],
    down: dict[str, list[str]],
    *,
    width: int,
    height: int,
    seed: int,
) -> TileMap:
    """按邻接表铺一张 ``width × height`` 的地图。

    同 ``seed`` + 同邻接表 + 同尺寸 → 同一张地图，逐格相等。

    撞上矛盾会换 seed 重试至多 :data:`MAX_RESTARTS` 次；仍然不行就抛错 ——
    **不交半成品**。
    """
    if width < 1 or height < 1:
        raise ProcessingError(f"地图尺寸非法：{width}×{height}")
    tiles = sorted(right)
    if not tiles:
        raise ProcessingError("邻接表是空的，铺不出地图")
    if sorted(down) != tiles:
        raise ProcessingError("邻接表的 right 与 down 覆盖的 tile 不一致")

    for attempt in range(MAX_RESTARTS):
        rng = random.Random(f"{seed}:{attempt}")
        rows = _Solver(tiles, right, down, width, height, rng).run()
        if rows is not None:
            return TileMap(rows=tuple(tuple(row) for row in rows), seed=seed)

    raise ProcessingError(
        f"这套邻接表在 {width}×{height} 上无解：换了 {MAX_RESTARTS} 次 seed 都撞上矛盾。"
        "多半是某些 tile 在某个方向上没有任何合法邻居 —— 这套 tile 之间缺过渡块。"
        "宁可在这里报错，也不交一张有非法接缝的地图。"
    )
