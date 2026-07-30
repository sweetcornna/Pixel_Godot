"""并发上限与速率限制。

为什么这个不能推迟到后期（PLAN §8 Sprint 2）：**Sprint 4 单个角色就有 21 次调用**
（seed + 5 动作 × 4 方向）。等到批量生成时才发现撞速率限制，
排查成本远高于现在就把闸门装上。

实现成同步的信号量 + 最小间隔，是因为当前流水线是同步的。
Sprint 7 的 asyncio 调度会复用同一套语义（届时换成 ``asyncio.Semaphore``），
所以接口刻意做成上下文管理器，调用方代码不必改。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from ..logging_utils import get_logger

logger = get_logger("provider.throttle")


@dataclass
class Throttle:
    """限制并发数与请求频率。线程安全。

    - ``max_concurrency``：同时在途的请求数上限。
    - ``min_interval``：两次请求**开始**之间的最小间隔（秒）。0 表示不限。

    两者是正交的：并发上限防止一次打出去太多，最小间隔防止短时间内连续打太密。
    速率限制大多同时管这两件事，所以两个旋钮都要有。
    """

    max_concurrency: int = 3
    min_interval: float = 0.0

    def __post_init__(self) -> None:
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency 至少为 1")
        self._semaphore = threading.BoundedSemaphore(self.max_concurrency)
        self._lock = threading.Lock()
        self._next_allowed = 0.0
        self._in_flight = 0
        self._peak_in_flight = 0
        self._total = 0

    @classmethod
    def from_rpm(cls, max_concurrency: int, requests_per_minute: int | None) -> Throttle:
        interval = 60.0 / requests_per_minute if requests_per_minute else 0.0
        return cls(max_concurrency=max_concurrency, min_interval=interval)

    def _wait_for_slot(self, now: Callable[[], float], sleep: Callable[[float], None]) -> None:
        if self.min_interval <= 0:
            return
        while True:
            with self._lock:
                current = now()
                if current >= self._next_allowed:
                    self._next_allowed = current + self.min_interval
                    return
                delay = self._next_allowed - current
            sleep(delay)

    @contextmanager
    def slot(
        self,
        *,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> Iterator[None]:
        """占用一个调用名额。退出上下文时归还。"""
        self._semaphore.acquire()
        try:
            self._wait_for_slot(now, sleep)
            with self._lock:
                self._in_flight += 1
                self._total += 1
                self._peak_in_flight = max(self._peak_in_flight, self._in_flight)
            try:
                yield
            finally:
                with self._lock:
                    self._in_flight -= 1
        finally:
            self._semaphore.release()

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "in_flight": self._in_flight,
                "peak_in_flight": self._peak_in_flight,
                "total": self._total,
                "max_concurrency": self.max_concurrency,
            }
