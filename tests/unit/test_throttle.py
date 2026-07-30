"""并发上限与速率限制（PLAN §8 Sprint 2）。

退出门槛写的是"并发上限**可配置且生效**"——所以这里不测"能不能构造出来"，
只测"真的挡住了"：并发峰值不超过上限、最小间隔真的被等待。
"""

from __future__ import annotations

import threading

import pytest

from pixel_asset_forge.providers import Throttle
from pixel_asset_forge.providers.throttle import Throttle as _T


def test_rejects_zero_concurrency() -> None:
    with pytest.raises(ValueError):
        _T(max_concurrency=0)


def test_peak_concurrency_never_exceeds_the_limit() -> None:
    throttle = Throttle(max_concurrency=3)
    barrier_hits = []
    lock = threading.Lock()
    active = 0
    peak = 0

    def worker() -> None:
        nonlocal active, peak
        with throttle.slot():
            with lock:
                active += 1
                peak = max(peak, active)
            # 让并发真的重叠，否则测不出上限
            threading.Event().wait(0.01)
            with lock:
                barrier_hits.append(active)
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert peak <= 3, f"并发峰值 {peak} 超过上限 3"
    assert len(barrier_hits) == 12  # 12 个任务全部完成，没有被丢弃


def test_all_work_completes_even_when_throttled() -> None:
    """限流是排队，不是丢弃 —— 单点排队不能变成任务丢失。"""
    throttle = Throttle(max_concurrency=1)
    done: list[int] = []

    def worker(i: int) -> None:
        with throttle.slot():
            done.append(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(done) == list(range(20))


def test_min_interval_is_enforced() -> None:
    """用假时钟测，不真的睡 —— 测试不该为了验证等待而变慢。"""
    throttle = Throttle(max_concurrency=4, min_interval=2.0)
    clock = [0.0]
    slept: list[float] = []

    def now() -> float:
        return clock[0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds

    for _ in range(3):
        with throttle.slot(now=now, sleep=sleep):
            pass

    # 第一次立即放行，之后每次都等满 2.0
    assert slept == [2.0, 2.0]
    assert clock[0] == 4.0


def test_no_interval_means_no_sleeping() -> None:
    throttle = Throttle(max_concurrency=2, min_interval=0.0)
    slept: list[float] = []
    for _ in range(5):
        with throttle.slot(now=lambda: 0.0, sleep=slept.append):
            pass
    assert slept == []


def test_from_rpm_converts_to_interval() -> None:
    assert Throttle.from_rpm(3, 30).min_interval == pytest.approx(2.0)
    assert Throttle.from_rpm(3, None).min_interval == 0.0


def test_slot_is_released_even_if_the_body_raises() -> None:
    """调用抛异常时名额必须归还，否则一次失败就永久占住一个并发位。"""
    throttle = Throttle(max_concurrency=1)
    with pytest.raises(RuntimeError), throttle.slot():
        raise RuntimeError("boom")

    # 还能再拿到名额说明确实归还了
    with throttle.slot():
        pass
    assert throttle.stats["in_flight"] == 0


def test_stats_track_usage() -> None:
    throttle = Throttle(max_concurrency=2)
    for _ in range(3):
        with throttle.slot():
            pass
    stats = throttle.stats
    assert stats["total"] == 3
    assert stats["in_flight"] == 0
    assert stats["max_concurrency"] == 2
