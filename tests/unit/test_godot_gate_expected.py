"""Godot 门槛的期望值生成器与生产导出器必须对同一条规则给出同一答案。

`tools/godot-gate/make_expected.py` 产出的是**门槛的期望值**，
`exporters/godot.py` 产出的是**被验的实际值**。代表 terrain 那条规则
（四角众数、同票取首个）两边各写了一遍。

**这份重复是有意保留的**：门槛脚本必须能用裸 `python3` 跑（README 的门槛流程就是
这么写的），import 生产模块会连带拉进 pydantic 等整套依赖。试过 import，裸 python3
直接 `ModuleNotFoundError`，退回来了。

但重复必须有测试锁住 —— 只改一边时，门槛会拿着**错误的期望**去比对，
然后**依然报 GATE-OK**。门槛在自己失效时保持沉默，是它能出的最坏故障。
"""

from __future__ import annotations

import importlib.util
import itertools
from pathlib import Path

import pytest

from pixel_asset_forge.exporters.godot import representative_terrain as production

_GATE = Path(__file__).resolve().parents[2] / "tools" / "godot-gate" / "make_expected.py"
_SPEC = importlib.util.spec_from_file_location("godot_gate_make_expected", _GATE)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
gate = _MODULE.representative_terrain


#: 穷举三种地形名在四个角上的全部组合。同票、众数、全同都覆盖到，
#: 尤其含示例里那块过渡 tile 的 (grass, grass, dirt, dirt) —— 它正好是同票，
#: 也正好是两种取法给出不同答案的那一类。
ALL_CORNERS = tuple(itertools.product(("grass", "dirt", "stone"), repeat=4))


@pytest.mark.parametrize("corners", ALL_CORNERS)
def test_gate_and_production_agree_on_every_corner_combination(
    corners: tuple[str, str, str, str],
) -> None:
    assert gate(list(corners)) == production(corners)


def test_the_tie_break_case_is_actually_covered() -> None:
    """同票是两种实现最容易分歧的地方 —— 确认它真的在用例里，而不是碰巧没覆盖到。

    只断言"全部组合一致"是不够的：如果组合集合里根本没有同票的情形，
    这条一致性检查就在一个不会出问题的范围上永真。
    """
    ties = [c for c in ALL_CORNERS if len(set(c)) == 2 and c.count(c[0]) == 2]
    assert ties, "用例里没有同票组合，一致性检查失去判别力"
    assert ("grass", "grass", "dirt", "dirt") in ties
    # 同票时规则是"按固定角顺序取首个"，两边都必须给出第一个角。
    for corners in ties:
        assert production(corners) == corners[0]
        assert gate(list(corners)) == corners[0]
