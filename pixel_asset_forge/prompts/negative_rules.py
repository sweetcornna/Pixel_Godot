"""负面约束清单。

每一条都对应一种会让资产直接作废的产出，不是"最好别有"而是"有了就废"：

- **文字/标签/数字** —— 模型很爱在 sprite sheet 上标 "1 2 3 4"。
  那些像素会被当成角色的一部分切进帧里。
- **格线/边框** —— 画出来的格线会被越界检测判为跨格连通域。
- **阴影/地面** —— 落地阴影紧贴脚底，键控抠不掉，会变成角色身上多出来的一块。
- **辉光/动态模糊** —— 在角色轮廓外围造出一圈半透明渐变，
  既污染键控边缘又把调色板打爆。
- **背景渐变/纹理** —— 直接摧毁色键的双峰分布前提（ADR-004）。
- **脱体特效**（仅 attack/cast）—— 把包围盒撑得远大于 idle，脚线锚点与
  跨动作缩放基准双双失准。
- **多余的 45° 转身** —— 模型把"3/4 视角"理解成角色绕自身轴转 45°
  （美术口径）而不是相机俯角（游戏开发口径），于是背面走路能看见半张脸、
  正面走路变成斜侧面。写死在正面约束里还不够，负面清单里再堵一次。
"""

from __future__ import annotations

#: 通用负面约束。任何资产类型都适用。
UNIVERSAL_NEGATIVES: tuple[str, ...] = (
    "no text, no labels, no numbers, no watermarks, no signatures",
    "no visible grid lines, no cell borders, no frame outlines",
    "no scenery, no ground plane, no horizon, no props other than the subject",
    "no drop shadows, no cast shadows, no ambient occlusion on the ground",
    "no glow, no bloom, no lens flare, no motion blur",
    "no background gradient, no background texture, no vignette",
)

#: 只对角色类资产适用。
CHARACTER_NEGATIVES: tuple[str, ...] = (
    "no additional characters, no crowd, no reflection",
    "no cropping of the head, hands or feet",
    "no three-quarter turn and no angled body: do not rotate the character away "
    "from the stated orientation, the shoulder line must stay square to it",
)

#: 只对**挥击类动作**适用：attack / cast 这类会自带特效的动作。
#:
#: 来自 agent-sprite-forge 的 body-only 规则。脱体特效会把包围盒撑到比 idle 大得多，
#: 于是这个动作的脚线锚点和跨动作缩放基准全被带偏 —— 角色在 attack 和 idle
#: 之间忽大忽小、脚还离地。特效该单独出一张 fx 表，在引擎里叠加。
SWING_ACTION_NEGATIVES: tuple[str, ...] = (
    "no detached slash arc, no weapon trail, no motion streak",
    "no muzzle flash, no projectile, no impact burst, no hit spark",
    "no detached dust cloud, no debris, no shockwave ring",
    "keep the weapon close enough to the body that the overall silhouette stays "
    "about the same size as a normal standing pose",
)

#: 像素风格约束。写成负面形式是因为模型对"不要什么"比"要什么"更敏感。
PIXEL_STYLE_NEGATIVES: tuple[str, ...] = (
    "no anti-aliasing, no soft edges, no gradients inside the sprite",
    "no photorealistic rendering, no 3D shading, no painterly brush strokes",
)


#: 会自带特效的动作。这些动作要额外加 body-only 约束。
SWING_ACTIONS: frozenset[str] = frozenset({"attack", "cast"})


def negative_block(
    *, character: bool = True, pixel_style: bool = True, action: str | None = None
) -> str:
    rules: list[str] = list(UNIVERSAL_NEGATIVES)
    if character:
        rules.extend(CHARACTER_NEGATIVES)
    if action in SWING_ACTIONS:
        rules.extend(SWING_ACTION_NEGATIVES)
    if pixel_style:
        rules.extend(PIXEL_STYLE_NEGATIVES)
    return "\n".join(f"- {rule}" for rule in rules)
