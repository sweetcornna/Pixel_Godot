"""节拍特征核对（Sprint / 任务 #36）。

`frame_order` 问"这个序列顺序对不对"，只看像素 —— 实测四种判据全部失效，
两个参考项目也都不做。

这里问的是**另一个问题**："第 N 格画的，是不是我要的那一拍？"
我们手里有逐格的姿势描述（是自己写进 prompt 的），有些拍带可测量的特征。
"""

from __future__ import annotations

import math
import random

import numpy as np
import pytest

from pixel_asset_forge.validation.beat_signature import (
    check_beat_signature,
    foot_span,
)

WALK_BEATS = ["CONTACT", "PASSING", "UP", "CONTACT", "PASSING", "UP"]


def frame_with_stance(width: int, *, canvas: int = 96) -> np.ndarray:
    """一个"角色"：躯干固定，只有脚的横向跨度变。"""
    frame = np.zeros((canvas, canvas, 4), dtype=np.uint8)
    cx = canvas // 2
    frame[20:80, cx - 8 : cx + 8] = (139, 90, 43, 255)
    half = max(1, width // 2)
    frame[76:86, cx - half : cx + half] = (90, 60, 30, 255)
    return frame


def side_walk() -> list[np.ndarray]:
    """跨步拍脚分得开，其余拍并拢 —— 侧视步态的真实形态。"""
    return [frame_with_stance(w) for w in (40, 18, 10, 42, 12, 10)]


# -- 度量 ------------------------------------------------------------------


def test_foot_span_measures_the_bottom_band_only() -> None:
    """量的是脚，不是整个轮廓 —— 挥出去的手臂不该算进来。"""
    frame = frame_with_stance(40)
    frame[30:36, 0:90] = (200, 200, 200, 255)  # 一条横贯全宽的"手臂"
    assert foot_span(frame) < 50


def test_an_empty_frame_spans_nothing() -> None:
    assert foot_span(np.zeros((96, 96, 4), dtype=np.uint8)) == 0


# -- 适用范围 --------------------------------------------------------------


def test_the_front_view_is_refused(caplog) -> None:  # type: ignore[no-untyped-def]
    """**跨步在正面是沿深度轴分开的**，投影到屏幕上几乎没有水平位移；
    而"双脚并拢"是两只脚并排，水平方向本来就最宽。

    实测 knight_01：正面 STRIDE 脚跨度 9、NEUTRAL 15 —— 判据是反的。
    硬要在这里给结论，就是拿一个已知无效的判据去误报正确产出。
    """
    result = check_beat_signature(side_walk(), WALK_BEATS, direction="down")
    assert not result.applicable
    assert "深度" in result.reason


def test_an_action_without_stride_beats_is_refused() -> None:
    idle = ["NEUTRAL", "INHALE", "PEAK", "EXHALE", "NEUTRAL", "INHALE"]
    result = check_beat_signature(side_walk(), idle, direction="left")
    assert not result.applicable


def test_mismatched_counts_are_refused() -> None:
    result = check_beat_signature(side_walk()[:3], WALK_BEATS, direction="left")
    assert not result.applicable


# -- 判定 ------------------------------------------------------------------


def test_a_correct_side_walk_is_consistent() -> None:
    result = check_beat_signature(side_walk(), WALK_BEATS, direction="left")
    assert result.applicable and result.consistent
    assert result.separation > 1.0


def test_the_criterion_is_separation_not_a_single_extreme() -> None:
    """只看"最宽的那帧在不在跨步拍上"，一次巧合就能蒙混过去。

    要求跨步拍里**最窄**的都比其余拍里**最宽**的还宽，两组才算真的分得开。
    """
    # 一个跨步拍很宽、另一个却很窄 —— 单极值判据会放行，分离判据不会
    frames = [frame_with_stance(w) for w in (40, 18, 10, 12, 12, 10)]
    result = check_beat_signature(frames, WALK_BEATS, direction="left")
    assert result.applicable and not result.consistent


def test_shuffles_are_caught_at_the_combinatorial_floor() -> None:
    """漏掉的只是"宽帧恰好落进跨步槽"那种巧合 —— 这是信息论下限，
    不是判据的弱点。

    实测（knight_01 真实产出，6 帧 2 个跨步拍）：500 次随机乱序放行 7.0%，
    组合学下限 1/C(6,2) = 6.7%，几乎重合。
    """
    frames = side_walk()
    stride = sum(1 for b in WALK_BEATS if b == "CONTACT")
    floor = 1 / math.comb(len(frames), stride)

    rng = random.Random(0)
    passed = 0
    for _ in range(400):
        shuffled = frames[:]
        rng.shuffle(shuffled)
        if check_beat_signature(shuffled, WALK_BEATS, direction="left").consistent:
            passed += 1
    rate = passed / 400
    assert rate < 0.20, f"放行率 {rate:.1%} 太高，判据没起作用"
    assert rate == pytest.approx(floor, abs=0.10), (
        f"放行率 {rate:.1%} 与组合学下限 {floor:.1%} 差得多 —— 度量本身有问题"
    )
