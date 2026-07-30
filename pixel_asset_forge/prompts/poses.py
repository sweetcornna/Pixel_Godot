"""逐帧姿势模板 —— Sprint 0 追加的范围。

初版的 prompt 只写 ``one complete walk cycle`` + ``exactly 8 distinct poses``。
实测结果是**八张几乎一样的站立姿势**，腿基本不动
（相邻帧差异 8/24/26/8/3/30/26/7，毫无循环节奏）。

把每一格该画什么写死之后才拿到真正的循环
（26/13/25/9/25/12/23/9，规整交替）。结论很直白：
**"一个完整循环"这种整体描述，模型不会自己拆解成具体姿势。**

所以每个动作都要有一套按帧数展开的姿势序列。为了不手写
5 动作 × 5 档位 = 25 份清单，这里用"标准节拍 + 采样/插值"生成。

**采样时绝不允许产生重复描述。** 帧数多于节拍数时要生成**过渡帧**
（"介于 A 与 B 之间"），而不是把同一拍复制两遍 —— prompt 里明写着
"no two cells may be identical"，再递给模型两条一样的描述就是自相矛盾的指令。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..errors import PlanError


@dataclass(frozen=True, slots=True)
class Beat:
    """一个标准节拍。``name`` 单独存，插值时要用它组过渡描述。"""

    name: str
    description: str

    def swapped(self) -> Beat:
        return Beat(self.name, _swap_sides(self.description))


@dataclass(frozen=True, slots=True)
class PoseCycle:
    beats: tuple[Beat, ...]

    half_cycle: bool = False
    """步态类动作：整个循环由左右两个半周期组成，第二个是第一个的左右互换。"""

    linear: bool = False
    """一次性动作：采样必须保留首尾两拍，丢了起手或收势就不成立了。"""

    frontal_beats: tuple[Beat, ...] | None = None
    """正面/背面视角专用的节拍。为 None 表示各视角共用 ``beats``。

    **步态必须分视角。** ``beats`` 里那套 CONTACT / DOWN / PASSING / UP 是
    动画学的**侧视**行走周期，描述的是从侧面才看得见的上下起伏与蹬地。
    正面朝向镜头时"身体降到最低点、用脚尖蹬地"根本读不出来，模型只能瞎猜，
    产出的动作看着很怪。俯视 RPG 的正面行走靠的是另一套线索：
    哪只脚在前、双脚是否并拢。参考 agent-sprite-forge 的四帧步：
    并拢 → 左脚前 → 并拢 → 右脚前。
    """


def _swap_sides(text: str) -> str:
    placeholder = "\x00"
    return (
        text.replace("left", placeholder)
        .replace("right", "left")
        .replace(placeholder, "right")
    )


def _blend(a: Beat, b: Beat, weight: float) -> Beat:
    """生成 a 与 b 之间的过渡帧描述。

    ``weight`` 是靠近 b 的程度（0~1），直接写成百分比。

    早先用"just past / midway / almost at"三档措辞，节拍少而帧数多时会撞车：
    ``cast`` 只有 4 拍却要出 12 帧，同一对节拍之间挤进 4 个过渡帧，
    三档不够分，于是产出重复描述。百分比对每个权重都唯一，也更好让模型定位中间姿势。
    """
    percent = round(weight * 100)
    detail_a = a.description.split(" — ", 1)[-1]
    detail_b = b.description.split(" — ", 1)[-1]
    return Beat(
        f"{a.name}→{b.name}",
        f"{a.name}→{b.name} — about {percent}% of the way from {a.name} to {b.name}: "
        f"starting from ({detail_a}) and moving towards ({detail_b})",
    )


def _sample(beats: tuple[Beat, ...], count: int, *, linear: bool) -> list[Beat]:
    """按 ``count`` 采样节拍序列，需要时插出过渡帧。

    - ``linear=True``：首尾必取，位置按 ``i·(N-1)/(count-1)``。
    - ``linear=False``：循环采样，不重复首尾，位置按 ``i·N/count``。
    """
    total = len(beats)
    if count < 1:
        raise PlanError(f"帧数必须为正，收到 {count}")

    # 帧数不多于节拍数时**只挑不插**。
    #
    # 插值的既定用途是"帧数多于节拍数"，可均匀重采样在 count < total 时
    # 照样会落在两拍中间，产出 "about 33% of the way from DOWN to PASSING:
    # starting from (...) and moving towards (...)" 这种描述 —— 图像模型
    # 据此画不出确定的姿势，实测直接被平均成站姿，腿完全不动。
    #
    # 挑出来的节拍本身互不相同，"不得有重复描述"这条约束自然满足。
    #
    # 挑的方式仍要分 linear：一次性动作（attack/hurt/death）丢了起手或收势
    # 就不成立了，所以首尾必取；循环动作则按循环位置取，不必回到首拍。
    if count <= total:
        if linear:
            picks = [
                0 if count == 1 else round(index * (total - 1) / (count - 1))
                for index in range(count)
            ]
        else:
            # 用 ceil。4 拍取 3 帧时三种取法的差别很实在：
            #   int   → 0/1/2  CONTACT·DOWN·PASSING  没有最高点，只沉不起
            #   round → 0/1/3  CONTACT·DOWN·UP       **丢掉了 PASSING**
            #   ceil  → 0/2/3  CONTACT·PASSING·UP    交叉腿在，且是上升的
            # PASSING 是双腿交叉那一拍，行走最强的可读线索 ——
            # 宁可丢一个极值也不能丢它。ceil 还顺带让 4 拍取 2 帧得到
            # CONTACT·PASSING，正是经典的两帧半周期走法。
            picks = [math.ceil(index * total / count) for index in range(count)]
        return [beats[min(total - 1, p)] for p in picks]

    out: list[Beat] = []
    for index in range(count):
        if linear:
            position = 0.0 if count == 1 else index * (total - 1) / (count - 1)
        else:
            position = index * total / count

        low = int(position)
        weight = position - low
        if weight < 1e-9:
            out.append(beats[low % total])
        else:
            out.append(_blend(beats[low % total], beats[(low + 1) % total], weight))
    return out


def _beats(*pairs: tuple[str, str]) -> tuple[Beat, ...]:
    return tuple(Beat(name, f"{name} — {desc}") for name, desc in pairs)


POSE_CYCLES: dict[str, PoseCycle] = {
    "walk": PoseCycle(
        half_cycle=True,
        frontal_beats=_beats(
            # 这一拍必须带方位线索。half_cycle 靠左右互换生成后半周期，
            # 描述里一个 left/right 都没有的话，互换后与原文一字不差 ——
            # 与 "no two cells may be identical" 直接冲突。
            ("NEUTRAL", "both feet together directly under the body, the right foot "
                        "having just come level with the left, standing squarely, "
                        "arms hanging relaxed at the sides"),
            ("STEP", "the left foot steps forward and is planted flat on the ground, "
                     "the right foot stays back, the left arm swings back and the right "
                     "arm swings forward"),
            ("STRIDE", "the left foot is far forward taking the weight and the right heel "
                       "is lifted well behind, the stride at its widest, "
                       "the shoulders staying level and square to the camera"),
            # 第四拍让 8 帧（每半 4 帧）也走"只挑不插"，不必再生成
            # "NEUTRAL→STEP 约 33%" 那种模型画不出来的插值描述。
            ("RECOVER", "the right foot swings forward off the ground on its way to meet "
                        "the left, the left foot still planted, the arms swinging back "
                        "towards neutral"),
        ),
        beats=_beats(
            ("CONTACT", "the left leg strides far forward and the right leg far back, "
                        "both feet touching the ground, arms swung opposite to the legs"),
            ("DOWN", "the body is at its lowest point, the left foot flat on the ground "
                     "taking the full weight, the right leg bent behind"),
            ("PASSING", "the right leg swings through directly underneath the body, "
                        "the left leg straight, the body at mid height"),
            ("UP", "the body is at its highest point, pushing off the left toes, "
                   "the right leg reaching forward"),
        ),
    ),
    "idle": PoseCycle(
        beats=_beats(
            ("NEUTRAL", "standing relaxed, weight evenly on both feet, arms at the sides"),
            ("INHALE", "the chest rises slightly and the shoulders lift by one or two "
                       "pixels, the head stays level"),
            ("PEAK", "the body is at the highest point of the breath, barely perceptible"),
            ("EXHALE", "the chest settles back down and the shoulders drop, "
                       "returning towards neutral"),
        ),
    ),
    "attack": PoseCycle(
        linear=True,
        beats=_beats(
            ("WINDUP", "weight shifts onto the back foot, the weapon is drawn back "
                       "behind the shoulder, the body coils"),
            ("COMMIT", "the front foot plants forward, the torso begins to rotate, "
                       "the weapon starts its arc"),
            ("STRIKE", "the weapon is at full extension in front of the character, "
                       "the body lunging forward at its furthest reach"),
            ("FOLLOW-THROUGH", "the weapon continues past the target and drops, "
                               "the shoulders rotating through"),
            ("RECOVER", "the character pulls back towards the neutral standing stance"),
        ),
    ),
    "hurt": PoseCycle(
        linear=True,
        beats=_beats(
            ("IMPACT", "the body snaps backwards from the blow, the head tilted back, "
                       "arms flung outwards"),
            ("RECOIL", "the character is pushed back onto the rear foot, the torso folded"),
            ("STAGGER", "struggling to regain balance, one arm reaching out for stability"),
            ("RECOVER", "straightening back up towards the neutral standing stance"),
        ),
    ),
    "death": PoseCycle(
        linear=True,
        beats=_beats(
            ("STAGGER", "the body lurches, the knees beginning to buckle"),
            ("KNEEL", "dropping onto one knee, the torso hunched forward"),
            ("COLLAPSE", "the body tips sideways, the arms no longer supporting it"),
            ("FALL", "falling towards the ground, most of the body below waist height"),
            ("LANDED", "lying on the ground, limbs splayed"),
            ("STILL", "motionless on the ground, the final resting pose"),
        ),
    ),
    "cast": PoseCycle(
        linear=True,
        beats=_beats(
            ("GATHER", "hands drawn together in front of the chest, "
                       "the body slightly crouched"),
            ("CHARGE", "arms rising and the body straightening, "
                       "energy building between the hands"),
            ("RELEASE", "arms thrust forward at full extension, "
                        "the body leaning into the cast"),
            ("SETTLE", "arms lowering back towards the neutral standing stance"),
        ),
    ),
    "travel": PoseCycle(
        beats=_beats(
            ("COMPACT", "the projectile core is compact and bright, "
                        "the trailing wisps short"),
            ("STRETCH", "the core stretches along the direction of travel "
                        "and the wisps lengthen"),
            ("EXTENDED", "the core is at its most elongated, "
                         "the trailing wisps at full length"),
            ("SETTLE", "the core contracts back towards compact and the wisps shorten"),
        ),
    ),
    "impact": PoseCycle(
        linear=True,
        beats=_beats(
            ("SPARK", "a small dense burst core at the point of impact"),
            ("EXPAND", "the burst expands rapidly outwards, still dense"),
            ("WIDEST", "the burst is at its widest, the centre beginning to hollow out"),
            ("BREAK", "the outer ring thins and breaks apart"),
            ("FADE", "only scattered fading embers remain"),
        ),
    ),
    "loop": PoseCycle(
        beats=_beats(
            ("SMALL", "the shape is at its smallest and brightest"),
            ("GROWING", "the shape expands towards its mid size"),
            ("LARGE", "the shape is at its largest and dimmest"),
            ("SHRINKING", "the shape contracts back towards its mid size"),
        ),
    ),
}


#: 正面与背面：镜头看到的是角色的正面或背面，看不到侧向的起伏。
FRONTAL_DIRECTIONS = frozenset({"down", "up"})


def pose_sequence(action: str, frames: int, direction: str | None = None) -> list[str]:
    """返回 ``frames`` 条**互不相同**的逐帧姿势描述。

    ``direction`` 是正面/背面时，有 ``frontal_beats`` 的动作改用那一套 ——
    侧视行走周期在正面视角下读不出来，见 ``PoseCycle.frontal_beats``。

    找不到动作时报错而不是退回泛泛描述 —— 泛泛描述正是 Sprint 0 里
    产出 N 张站姿的那个原因，静默退回去等于把已知失败模式请回来。
    """
    cycle = POSE_CYCLES.get(action)
    if cycle is None:
        raise PlanError(
            f"动作 {action!r} 没有姿势模板。可用：{', '.join(sorted(POSE_CYCLES))}。"
            "不要用泛泛的整体描述兜底 —— 实测那样会产出 N 张几乎一样的站姿。"
        )

    beats_source = cycle.beats
    if direction in FRONTAL_DIRECTIONS and cycle.frontal_beats is not None:
        beats_source = cycle.frontal_beats

    if cycle.half_cycle and frames % 2 == 0 and frames >= 4:
        half = _sample(beats_source, frames // 2, linear=False)
        beats = half + [beat.swapped() for beat in half]
    else:
        beats = _sample(beats_source, frames, linear=cycle.linear)

    descriptions = [beat.description for beat in beats]
    if len(set(descriptions)) != len(descriptions):  # pragma: no cover - 采样保证不重复
        raise PlanError(
            f"{action} 的 {frames} 帧姿势序列出现重复描述。"
            "prompt 里明写着 no two cells may be identical，递重复描述是自相矛盾的指令。"
        )
    return descriptions


def numbered_poses(
    action: str, frames: int, cols: int, direction: str | None = None
) -> str:
    """把姿势序列排成带行列号的清单，直接嵌进 prompt。

    标出行列位置是刻意的：帧序必须固定为从左到右、从上到下（PLAN §2.3.1），
    只写"第 N 格"模型未必按同一种阅读顺序理解。

    ``direction`` 透传给 :func:`pose_sequence` —— 正面与侧面用的是两套步态。
    """
    lines = []
    for index, beat in enumerate(pose_sequence(action, frames, direction)):
        col, row = index % cols + 1, index // cols + 1
        lines.append(f"Cell {index + 1} (row {row}, column {col}): {beat}")
    return "\n".join(lines)
