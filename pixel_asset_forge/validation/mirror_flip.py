"""检测帧间的整体镜像翻转。

用户反复反馈"走着走着突然翻面"。这在**播放时**极刺眼，可静态看单帧完全正常 ——
每一帧都是合格的角色，只是有几帧朝向反了。

## 判据：手性

把每帧按质心对齐，然后问一个问题：

    这一帧与首帧更像，还是与**首帧的镜像**更像？

与镜像更像，就是翻面了。只比不透明掩膜、不比颜色 —— 翻面是几何事件，
颜色分布在翻面前后几乎不变，掺进来只会稀释信号。

## 只对**明显有手性**的帧下结论

躺平的尸体、圆形的史莱姆，本身左右近乎对称，与自己的镜像重叠率极高 ——
这种帧的手性差值天然在 0 附近抖动，据此判翻面就是纯噪声。
所以先量每帧**自身的**不对称度，够不对称才参与判定。

## 实测（6 角色 × 5 动作 = 30 个）

首轮检出 4 个，全部集中在 ``hurt``（3/6）与 ``attack``（1/6），
``walk`` 与 ``idle`` **一个都没有**。

这个分布指出了病根：``walk`` 的姿势节拍句句写死 left/right，而 ``hurt`` /
``attack`` 写的是"the rear foot""one arm""the shoulder" —— 哪一只全没说，
模型逐格自由选择，选得不一致就读成整体翻面。节拍已按此修正
（见 ``prompts/poses.py``），改完重生成，四个里三个不再报。

## 这个判据的可信度：有限，所以只到 MEDIUM

逐图核对之后必须承认两件事：

1. **确有真阳性。** mage·attack 的法杖第 1、4 帧在左手、第 2、3 帧在右侧；
   knight·hurt 的剑同样换过侧。这是真缺陷，播放时极刺眼。
2. **也确有误报。** golem·hurt 与 knight·death 被报出来，看图是正常的
   受击后仰与倒地。

真阳性 -0.03 ~ -0.10、误报 -0.020 ~ -0.024，两组之间只隔 0.006。

**判据在低手性的角色上根本不成立。** 石魔方正、史莱姆浑圆，自身与镜像的
重叠率就有 0.72 ~ 1.00，手性信号本来就只有零点零几，与噪声同量级。
``MAX_SELF_SYMMETRY`` 那道关拦掉了最糟的一批，但拦不干净。

所以严重度定 **MEDIUM**：报出来让人去看 contact sheet，但不阻断放行。
一个会误报的阻断项，最终一定会被开发者关掉（PLAN §9.1）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: 手性差值低于此判为翻面。
#:
#: **这个阈值的余量很薄，所以本检查只到 MEDIUM，不阻断放行。**
#:
#: 逐图核对过的实测值：
#:
#:     真翻面   mage·attack -0.10、golem·hurt -0.10、archer·hurt -0.06、
#:              knight·hurt -0.03   （法杖/剑确实换到了另一侧）
#:     误报     golem·hurt -0.024、knight·death -0.020
#:              （看图是正常的受击后仰与倒地，没有翻面）
#:
#: 两组之间只隔 0.006，取 -0.03 能分开，但**没有余量**。
#: 而且真阳性只有四个、其中一个正好压在线上 —— 拿六个点调出来的阈值
#: 不足以支撑"阻断放行"这种强度的判定。
FLIP_THRESHOLD = -0.03

#: 帧自身的不对称度下限。与自己的镜像重叠率高于此，就认为这帧没手性可言。
#:
#: 躺平的尸体、圆形的史莱姆本来就左右对称，对它们判翻面纯属噪声。
MAX_SELF_SYMMETRY = 0.90


@dataclass(frozen=True, slots=True)
class MirrorReport:
    chirality: tuple[float, ...]
    """每帧相对首帧的手性差值。负值表示与首帧的镜像更像。"""

    judged: tuple[bool, ...]
    """每帧是否**够手性**、参与了判定。"""

    flipped: tuple[int, ...]
    """判为翻面的帧下标。"""

    @property
    def applicable(self) -> bool:
        return any(self.judged)

    def summary(self) -> str:
        if not self.applicable:
            return "未判定（各帧左右近乎对称，手性判据在这里没有意义）"
        if not self.flipped:
            return f"朝向一致（{sum(self.judged)} 帧参与判定）"
        listed = "、".join(str(i + 1) for i in self.flipped)
        return f"第 {listed} 帧相对首帧是镜像的 —— 播放时会突然翻面"


def _centred(mask: np.ndarray) -> np.ndarray:
    """把掩膜按质心平移到画面中心。

    不对齐的话，一次平移就能让重叠率大跌，翻面信号被位移淹没。
    """
    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return mask
    return np.roll(
        np.roll(mask, mask.shape[0] // 2 - int(ys.mean()), axis=0),
        mask.shape[1] // 2 - int(xs.mean()),
        axis=1,
    )


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    union = (a | b).sum()
    return float((a & b).sum() / union) if union else 0.0


def detect_mirror_flips(frames: list[np.ndarray]) -> MirrorReport:
    """逐帧判断是否相对首帧翻了面。

    帧尺寸不一致时**跳过而不是报错**。那种输入自有 ``frame_size`` 那条致命项去拦，
    这里再抛一次异常只会让整份报告生不出来 —— 用户看不到任何一条检查结果，
    连"哪里坏了"都无从得知。
    """
    if len(frames) < 2:
        return MirrorReport((), (), ())
    if len({frame.shape[:2] for frame in frames}) > 1:
        return MirrorReport((), (), ())

    masks = [_centred(frame[:, :, 3] > 0) for frame in frames]
    reference = masks[0]

    chirality: list[float] = []
    judged: list[bool] = []
    flipped: list[int] = []
    for index, mask in enumerate(masks):
        value = _iou(mask, reference) - _iou(mask[:, ::-1], reference)
        chirality.append(round(value, 4))
        # 这一帧本身够不够"左右有别"
        has_chirality = _iou(mask, mask[:, ::-1]) < MAX_SELF_SYMMETRY
        judged.append(has_chirality)
        if has_chirality and value < FLIP_THRESHOLD:
            flipped.append(index)

    return MirrorReport(tuple(chirality), tuple(judged), tuple(flipped))
