"""节拍特征核对 —— 帧序问题上**唯一被实测支撑的自动判据**。

## 与 frame_order 的区别

`frame_order` 问的是"这个序列的顺序对不对"，只看像素、不看请求。
实测四种判据全部失效（见该模块），两个参考项目也都不做这件事。

这里问的是**另一个问题**："第 N 格画的，是不是我要的那一拍？"

我们手里有逐格的姿势描述 —— 那是我们自己写进 prompt 的。有些拍带可测量的
特征，于是"观察到的特征分布"与"请求的节拍分布"能对上账。对不上就说明
要么模型把姿势画到了别的格子（帧序错乱），要么它压根没照描述画。
两者都是该报的缺陷。

## 适用范围很窄，而且必须窄

特征得**经得起投影**。实测（knight_01，96px 画布）：

| 视角 | 判据 | CONTACT / STRIDE | 其余拍 | 可用 |
|---|---|---|---|:---:|
| 侧视 | 水平脚跨度 | 41 / 43 | 10 ~ 18 | ✓ 分得很开 |
| 正面 | 水平脚跨度 | 9 | 15（NEUTRAL） | ✗ **反的** |
| 正面 | 脚部竖直错位 | 12 | 12 | ✗ 常数 |

正面那两行不是模型画错了。**跨步在正面是沿深度轴分开的**，投影到屏幕上
几乎没有水平位移；而"双脚并拢"是两只脚并排，水平方向本来就最宽。
竖直错位则被取样带的高度限死，饱和成常数。

所以本模块只对**侧视的步态动作**给结论，其余一律 skip 并说明原因。
硬要给正面也判一个，就是在拿一个已知无效的判据去误报正确产出 ——
而一个会误报的验证器最终一定会被关掉，等于白做（同 PLAN §9.1）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: 带"跨步"语义的节拍名。它们的脚跨度应当明显大于其余拍。
STRIDE_BEATS = frozenset({"CONTACT", "STRIDE", "STEP"})

#: 侧视方向。只有这些方向的跨步才在屏幕上表现为水平分开。
SIDE_DIRECTIONS = frozenset({"left", "right"})

#: 取内容底部这个比例作为"脚"。
FOOT_BAND_RATIO = 0.1


@dataclass(frozen=True, slots=True)
class BeatSignature:
    applicable: bool
    reason: str
    stride_min: float = 0.0
    other_max: float = 0.0
    separation: float = 0.0
    """跨步拍的最小脚跨度 ÷ 其余拍的最大脚跨度。> 1 表示分得开。"""

    spans: tuple[int, ...] = ()
    beats: tuple[str, ...] = ()

    @property
    def consistent(self) -> bool:
        return self.applicable and self.separation > 1.0

    def summary(self) -> str:
        if not self.applicable:
            return f"未核对（{self.reason}）"
        verdict = "一致" if self.consistent else "对不上"
        return (
            f"节拍特征{verdict}：跨步拍最小脚跨度 {self.stride_min:.0f}、"
            f"其余拍最大 {self.other_max:.0f}（分离度 {self.separation:.2f}）"
        )


def foot_span(frame: np.ndarray) -> int:
    """脚部区域的水平跨度（像素）。空帧返回 0。"""
    opaque = frame[:, :, 3] > 0
    rows = np.nonzero(opaque)[0]
    if rows.size == 0:
        return 0
    top, bottom = int(rows.min()), int(rows.max())
    band_height = max(2, round((bottom - top + 1) * FOOT_BAND_RATIO))
    band = opaque[bottom - band_height + 1 : bottom + 1]
    cols = np.nonzero(band)[1]
    return int(cols.max() - cols.min() + 1) if cols.size else 0


def check_beat_signature(
    frames: list[np.ndarray],
    beats: list[str],
    *,
    direction: str | None,
) -> BeatSignature:
    """核对观察到的脚跨度分布是否与请求的节拍对得上。

    判据是**分离**而不是"最宽的那帧在跨步拍上"：后者只看一个极值，
    一次巧合就能蒙混过去。要求跨步拍里最窄的都比其余拍里最宽的还宽，
    才说明两组真的分得开。
    """
    if direction not in SIDE_DIRECTIONS:
        return BeatSignature(
            False,
            f"{direction or '无方向'} 不是侧视 —— 跨步在正面沿深度分开，"
            "投影到屏幕上几乎没有水平位移，脚跨度这个判据在那里是反的",
        )
    if len(frames) != len(beats):
        return BeatSignature(False, f"帧数 {len(frames)} 与节拍数 {len(beats)} 对不上")

    stride_index = [i for i, beat in enumerate(beats) if beat.upper() in STRIDE_BEATS]
    other_index = [i for i in range(len(beats)) if i not in stride_index]
    if not stride_index or not other_index:
        return BeatSignature(False, "这个动作没有跨步拍，或全是跨步拍 —— 无从对比")

    spans = [foot_span(frame) for frame in frames]
    stride_min = float(min(spans[i] for i in stride_index))
    other_max = float(max(spans[i] for i in other_index))
    separation = stride_min / other_max if other_max else float("inf")

    return BeatSignature(
        applicable=True,
        reason="",
        stride_min=stride_min,
        other_max=other_max,
        separation=separation,
        spans=tuple(spans),
        beats=tuple(beats),
    )
