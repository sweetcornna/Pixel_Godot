"""帧序连续性检查 —— **实测不可判定，本模块只报告统计量，绝不阻断放行。**

## PLAN §9.2 的设想

> 正确排序的循环动画，相邻帧之间的像素差异应大致均匀；若模型把帧序打乱，
> 会出现一到多个"差异突变点"。突变点超过 1 个即告警。
> 这是捕捉 §2.3.1 所述"静默失败"的唯一自动手段。

## 实测结论：这个设想不成立

用 Sprint 4 的真实产出（`walk_down` / `walk_up`，各 8 帧）与它们的随机乱序对比，
四种判据全部无法区分：

| 判据 | 正序 | 随机乱序 | 可区分 |
|---|---|---|:---:|
| 相邻差异 max/min | 3.5 / 2.3 | 2.5 – 6.0 | ✗ 重叠 |
| 局部离群（d[i] 比左右邻均值） | 2.75 / 1.98 | 1.22 – 2.46 | ✗ **且方向相反** |
| 奇偶位置差异的变异系数 | 0.41 / 0.35 | 0.33 – 0.52 | ✗ 重叠 |
| 循环总路径长度（最短哈密顿路径） | — | — | ✗ 正序只优于 75% 的排列 |

第二行尤其致命：**正序的局部离群值反而最高**。
按"标记离群点"的判据去做，会把**正确**的序列判为失败、把乱序放行。

原因不难理解：正确的循环里，某些转场天然就比别的大（半周期交界处的
CONTACT→CONTACT），而随机打乱会让差异序列**向均值回归**、看起来更"均匀"。
PLAN 把"均匀"当成正确的标志，恰好把因果搞反了。

在缩到 32×32 之前的原始单元格（384×512）上重测，结论不变 ——
不是下采样丢了信号，是**走路循环的 8 个姿势本来就太像**，
它们两两差异的量级被噪声主导。

## 因此本模块的做法

计算并报告统计量，但**结果恒为 skip、严重度为 low**，永远不阻断放行。

理由与 PLAN §9.1 拒绝统一阈值时给出的完全一致：
**一个会对正确产出误报的验证器，最终一定会被开发者关掉，等于白做。**
在判据没有被数据支持之前，宁可诚实地承认测不了。

## 但换个问法就有解 —— 见 `beat_signature`

上面测的全是"**只看像素，反推顺序对不对**"。两个参考项目
（agent-sprite-forge、hatch-pet）也都不做这件事，前者的 QC 只查缩放/锚点/触边。

换成"**第 N 格画的，是不是我要的那一拍？**"就有解了 —— 因为逐格的姿势描述
是我们自己写进 prompt 的，有些拍带可测量的特征。实测（knight_01）：

| 视角 | 判据 | 跨步拍 | 其余拍 | 可用 |
|---|---|---|---|:---:|
| 侧视 | 水平脚跨度 | 41 / 43 | 10 ~ 18 | ✓ |
| 正面 | 水平脚跨度 | 9 | 15 | ✗ 反的 |
| 正面 | 脚部竖直错位 | 12 | 12 | ✗ 常数 |

侧视步态的 500 次随机乱序里只有 7.0% 被放行，而组合学下限
（两个宽帧恰好落进两个跨步槽）是 1/C(6,2) = 6.7% —— **几乎重合**，
说明判据本身近乎完美，漏掉的都是信息论意义上无法区分的巧合。

正面那两行不是模型画错了：跨步在正面沿深度轴分开，投影到屏幕上几乎没有
水平位移；而"双脚并拢"是两只脚并排，水平方向本来就最宽。

所以 `beat_signature` 只对**侧视步态**给结论，其余 skip 并说明原因。
覆盖面窄，但窄得有理有据 —— 好过一个会误报的宽判据。

## 遗留缺口（必须写在报告里）

**帧序被打乱目前无法自动检测。** 其余检查项（帧数、尺寸、锚点、无空白帧、
无重复帧）在乱序时全部照常通过 —— 这正是 §2.3.1 所说的"静默失败"。
现阶段唯一的防线是**人眼看 `previews/*.gif`**。

可能的出路（都未实现）：把每格产出与 prompt 里指派给该格的姿势描述做比对，
即"按构造验证"而非"按观察验证"。那需要一个姿势分类器，超出当前范围。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .metrics import adjacent_differences


@dataclass(frozen=True, slots=True)
class FrameOrderStats:
    differences: tuple[float, ...]
    max_over_min: float
    local_outlier: float
    parity_cv: float

    def summary(self) -> str:
        return (
            f"相邻帧差异 max/min={self.max_over_min:.1f}、"
            f"局部离群={self.local_outlier:.2f}、奇偶变异系数={self.parity_cv:.2f}"
        )


def _local_outlier(values: np.ndarray) -> float:
    """每个差异与其左右邻居均值之比的最大值。

    保留这个量是为了**可观测性**，不是为了判定 —— 实测它在正序上反而更高。
    """
    n = len(values)
    if n < 3:
        return 1.0
    ratios = [
        values[i] / max(1e-9, (values[(i - 1) % n] + values[(i + 1) % n]) / 2)
        for i in range(n)
    ]
    return float(max(ratios))


def _parity_cv(values: np.ndarray) -> float:
    """奇偶位置各自的变异系数取大者。步态类动作理应呈现"一大一小"交替。"""
    out = 0.0
    for offset in (0, 1):
        subset = values[offset::2]
        if subset.size < 2:
            continue
        mean = float(subset.mean())
        if mean > 0:
            out = max(out, float(subset.std() / mean))
    return out


def measure_frame_order(frames: list[np.ndarray], *, loop: bool) -> FrameOrderStats | None:
    """计算帧序统计量。帧数不足时返回 None。"""
    diffs = adjacent_differences(frames, loop=loop)
    if len(diffs) < 3:
        return None

    values = np.array(diffs, dtype=float)
    smallest = float(values.min())
    return FrameOrderStats(
        differences=tuple(float(v) for v in values),
        max_over_min=float(values.max() / smallest) if smallest > 0 else float("inf"),
        local_outlier=_local_outlier(values),
        parity_cv=_parity_cv(values),
    )


#: 写进验证报告的说明。措辞刻意直白 —— 用户有权知道哪条防线是缺的。
UNDETECTABLE_MESSAGE = (
    "帧序是否被打乱无法自动判定：实测四种判据在正序与随机乱序上完全重叠，"
    "其中局部离群判据方向相反（正序反而更高），照 PLAN §9.2 实现会把正确产出判为失败。"
    "请人眼查看 previews/ 下的 GIF 确认播放顺序。"
)
