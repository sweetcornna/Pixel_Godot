"""帧率重采样（Sprint 6.8.2）。

纯计算，不调用 API。算错了会在生成阶段变成"多花了几次调用还对不齐"，
所以这里断言得比别处密。
"""

from __future__ import annotations

import pytest

from pixel_asset_forge.errors import PlanError
from pixel_asset_forge.planning import frame_order, plan_inbetweens

# -- 目标帧数 --------------------------------------------------------------


def test_target_frames_preserve_the_duration() -> None:
    """3 帧 @ 3fps 是一秒；要 12fps 就得是 12 帧。"""
    budget = plan_inbetweens(3, source_fps=3, target_fps=12)
    assert budget.target_frames == 12
    assert budget.generated_frames == 9


def test_an_explicit_frame_count_is_not_second_guessed() -> None:
    """用户说 8 帧就是 8 帧，不要被帧率换算的取整改成 7 或 9。"""
    budget = plan_inbetweens(2, source_fps=2, target_fps=7, target_frames=8)
    assert budget.target_frames == 8
    assert budget.target_fps == 7


def test_downsampling_is_refused_rather_than_silently_dropping_keyframes() -> None:
    with pytest.raises(PlanError, match="不会删掉用户给的关键帧"):
        plan_inbetweens(8, source_fps=12, target_fps=4)


def test_a_single_keyframe_is_not_interpolation() -> None:
    with pytest.raises(PlanError, match="至少需要 2 张"):
        plan_inbetweens(1, source_fps=6, target_fps=12)


def test_an_absurd_target_is_refused() -> None:
    with pytest.raises(PlanError, match="超过上限"):
        plan_inbetweens(4, source_fps=1, target_fps=60)


# -- 间隔数：循环与一次性不同 ----------------------------------------------


def test_a_loop_wraps_the_last_keyframe_back_to_the_first() -> None:
    """循环动作的末帧要接回首帧，所以间隔数等于关键帧数。"""
    assert plan_inbetweens(3, source_fps=3, target_fps=12, loop=True).gaps == 3


def test_a_one_shot_has_one_gap_fewer() -> None:
    """倒地、受击不接回去。"""
    assert plan_inbetweens(3, source_fps=3, target_fps=12, loop=False).gaps == 2


# -- 分布 ------------------------------------------------------------------


def test_inbetweens_are_spread_as_evenly_as_possible() -> None:
    """相邻间隔的帧数差超过 1，播放节奏就是忽快忽慢的。"""
    budget = plan_inbetweens(3, source_fps=3, target_fps=12, loop=False)
    assert budget.inbetweens == (5, 4)
    assert max(budget.inbetweens) - min(budget.inbetweens) <= 1


def test_the_remainder_goes_to_the_earlier_gaps() -> None:
    """动作的起始段通常变化最快，多给一帧更有用。"""
    budget = plan_inbetweens(4, source_fps=4, target_fps=9, target_frames=9)
    assert budget.inbetweens[0] >= budget.inbetweens[-1]


def test_every_gap_costs_exactly_one_call() -> None:
    """一个间隔一次调用，一次画完该间隔的全部中间帧 ——
    逐帧调用会让身份漂移，这是全项目最贵的教训。
    """
    budget = plan_inbetweens(3, source_fps=3, target_fps=12)
    assert budget.api_calls == 3
    assert budget.api_calls <= budget.gaps


def test_gaps_needing_nothing_cost_nothing() -> None:
    budget = plan_inbetweens(4, source_fps=4, target_fps=5, target_frames=5)
    assert budget.generated_frames == 1
    assert budget.api_calls == 1, "只有一个间隔要补，不该为空间隔也发一次调用"


# -- 播放顺序 --------------------------------------------------------------


def test_frame_order_interleaves_keyframes_and_inbetweens() -> None:
    budget = plan_inbetweens(2, source_fps=2, target_fps=6, target_frames=6)
    order = frame_order(budget)
    assert len(order) == budget.target_frames
    assert order[0] == "key:0"
    assert order.count("key:0") == 1 and order.count("key:1") == 1
    assert order.index("key:0") < order.index("key:1")


def test_a_one_shot_order_ends_on_the_last_keyframe() -> None:
    """一次性动作的收势必须是最后一帧，后面不能再挂中间帧。"""
    budget = plan_inbetweens(3, source_fps=3, target_fps=9, loop=False)
    assert frame_order(budget)[-1] == "key:2"


def test_a_loop_order_ends_inside_the_wrap_gap() -> None:
    """循环动作最后是"末帧回首帧"那段的中间帧，播放时才接得上。"""
    budget = plan_inbetweens(3, source_fps=3, target_fps=12, loop=True)
    assert frame_order(budget)[-1].startswith("gap:2:")


def test_a_gap_that_cannot_be_drawn_in_one_call_is_refused() -> None:
    """一个间隔一次调用画完，所以每个间隔的帧数必须落在能一次画出的档位上。

    在规划阶段拦住，而不是等到生成阶段 —— 那时已经花掉了前面几个间隔的调用。
    """
    with pytest.raises(PlanError, match="一次调用只能画出"):
        # 2 张关键帧循环 → 2 个间隔，补 14 帧即每隔 7 帧，而 7 不是可用档位
        plan_inbetweens(2, source_fps=2, target_fps=16, target_frames=16)


def test_more_keyframes_make_an_impossible_target_possible() -> None:
    """报错信息里建议的两条路，至少第一条得真的走得通。"""
    from pixel_asset_forge.planning import supported_batch_sizes

    budget = plan_inbetweens(4, source_fps=4, target_fps=9, target_frames=9)
    assert max(budget.inbetweens) in supported_batch_sizes()
