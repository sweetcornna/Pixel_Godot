"""帧率重采样 —— 算清楚要补几帧、补在哪里（PLAN §8 Sprint 6.8.2）。

用户手上是几张关键帧，想要的是能播的动画。"补到正常帧率"这句话里藏着
两个必须先答清楚的问题：

1. **目标帧数是多少？** 由"保持时长不变、提高帧率"推出来 ——
   3 帧 @ 3fps 是一秒，要 12fps 就得是 12 帧。
2. **补出来的帧插在哪两张之间？** 循环动作最后一张要接回第一张，
   所以间隔数等于关键帧数；一次性动作（倒地、受击）不接回去，间隔数少一个。

这一步**纯计算，不调用 API**。算错了会在生成阶段变成"多花了几次调用还对不齐"，
所以它值得单独成模块并被完整断言。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import PlanError
from .grid_layout import supported_batch_sizes

#: 目标帧数的上限。再多也补不出更多信息，只是把钱烧在生成上。
MAX_TARGET_FRAMES = 24


@dataclass(frozen=True, slots=True)
class FrameBudget:
    """一次补间的完整预算。"""

    keyframes: int
    target_frames: int
    target_fps: int
    loop: bool

    inbetweens: tuple[int, ...]
    """每个间隔要补几帧。``inbetweens[i]`` 是第 i 张与第 i+1 张之间的数量。

    循环动作的最后一项是"末帧 → 首帧"之间。
    """

    @property
    def gaps(self) -> int:
        return len(self.inbetweens)

    @property
    def generated_frames(self) -> int:
        return sum(self.inbetweens)

    @property
    def api_calls(self) -> int:
        """要补的间隔数就是调用次数 —— 一个间隔一次调用，一次画完该间隔的全部中间帧。

        逐帧调用会让身份漂移（这是全项目最贵的教训），所以间隔内绝不拆开。
        """
        return sum(1 for count in self.inbetweens if count > 0)

    def describe(self) -> str:
        spread = "+".join(str(n) for n in self.inbetweens)
        return (
            f"{self.keyframes} 关键帧 → {self.target_frames} 帧 @ {self.target_fps}fps"
            f"（补 {self.generated_frames} 帧，分布 {spread}，{self.api_calls} 次调用）"
        )


def _distribute(total: int, gaps: int) -> tuple[int, ...]:
    """把 ``total`` 个中间帧尽量均匀地分到 ``gaps`` 个间隔里。

    余数摊在**靠前**的间隔上而不是全塞进最后一个：动作的起始段通常变化最快，
    多给一帧更有用；而且这样相邻间隔的帧数差不会超过 1，播放节奏才是匀的。
    """
    base, remainder = divmod(total, gaps)
    return tuple(base + (1 if index < remainder else 0) for index in range(gaps))


def plan_inbetweens(
    keyframes: int,
    *,
    source_fps: int,
    target_fps: int,
    loop: bool = True,
    target_frames: int | None = None,
) -> FrameBudget:
    """算出补间预算。

    ``target_frames`` 省略时按**保持时长不变**推算：
    关键帧序列的时长是 ``keyframes / source_fps``，乘上目标帧率就是目标帧数。

    显式给 ``target_frames`` 时不再推算 —— 用户明确要 8 帧就是 8 帧，
    不要因为帧率换算的取整把它改成 7 或 9。
    """
    if keyframes < 2:
        raise PlanError(
            f"补间至少需要 2 张关键帧，收到 {keyframes}。"
            "只有一张时没有中间态可言，那是生成而不是补间。"
        )
    if source_fps < 1 or target_fps < 1:
        raise PlanError(f"帧率必须为正，收到 源 {source_fps} / 目标 {target_fps}")

    if target_frames is None:
        target_frames = round(keyframes * target_fps / source_fps)

    if target_frames < keyframes:
        raise PlanError(
            f"目标 {target_frames} 帧少于 {keyframes} 张关键帧 —— "
            "补间只会增加帧，不会删掉用户给的关键帧。"
            f"（{source_fps}fps → {target_fps}fps 是降帧率，这里不做抽帧。）"
        )
    if target_frames > MAX_TARGET_FRAMES:
        raise PlanError(
            f"目标 {target_frames} 帧超过上限 {MAX_TARGET_FRAMES}。"
            "再多也补不出更多信息，只是把钱烧在生成上。"
        )

    gaps = keyframes if loop else keyframes - 1
    inbetweens = _distribute(target_frames - keyframes, gaps)

    # 一个间隔一次调用画完，所以每个间隔的帧数必须落在**能一次画出来**的档位上。
    # 这里拦住而不是等到生成阶段：那时已经花了前面几个间隔的调用。
    supported = supported_batch_sizes()
    biggest = max(inbetweens)
    if biggest and biggest not in supported:
        usable = ", ".join(str(n) for n in supported)
        raise PlanError(
            f"每个间隔要补 {biggest} 帧，而一次调用只能画出 {usable} 帧。"
            f"要么多给几张关键帧（间隔变多、每个间隔要补的就少了），"
            f"要么把目标帧率降下来。"
        )

    return FrameBudget(
        keyframes=keyframes,
        target_frames=target_frames,
        target_fps=target_fps,
        loop=loop,
        inbetweens=inbetweens,
    )


def frame_order(budget: FrameBudget) -> list[str]:
    """按播放顺序列出每一帧的来源标记。

    ``key:i`` 是第 i 张关键帧（原样保留），``gap:i:j`` 是第 i 个间隔里的第 j 张。

    导出这份顺序表是刻意的：补间最容易出的错就是把中间帧插错位置，
    而帧序错乱**无法自动检测**（见 validation/frame_order.py）。
    有一份显式的顺序表，至少人工核对时有据可依。
    """
    out: list[str] = []
    for index in range(budget.keyframes):
        out.append(f"key:{index}")
        if index < budget.gaps:
            out.extend(f"gap:{index}:{j}" for j in range(budget.inbetweens[index]))
    return out
