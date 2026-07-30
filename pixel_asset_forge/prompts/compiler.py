"""Prompt Compiler —— 把 Asset Request 编译成生成 prompt。

**纯函数**：同样的输入必然产出同样的字符串（PLAN §2.7 的确定性边界里，
prompt 编译是确定性的那一侧）。因此它可以被 golden 测试完全覆盖，
而生成层不能。

编译出的 prompt 必须包含 PLAN §8 Sprint 4 列出的全部固定约束。
它们不是文风建议，每一条都对应一个已知失败模式 —— 漏掉哪条，
哪条对应的失败就会回来。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..constants import Direction
from ..models.request import AssetRequest
from ..planning.grid_layout import GridLayout
from .negative_rules import negative_block
from .poses import numbered_poses

#: prompt 里要求的边距。**判定按 8%**（PLAN §2.3.2）。
#:
#: 这个不对称是实测出来的：模型把边距要求打约七折执行 ——
#: 写 8% 时实测最小边距 0.0% 且真的跨了格线，写 12% 时得到 7.9%、四条格线全干净。
PROMPT_MARGIN_PERCENT = 12

#: 相机俯角。**绝对不能出现 "3/4" 这三个字符。**
#:
#: "3/4 top-down" 在游戏开发里说的是**相机俯角**，在角色美术里说的是
#: **角色绕自身轴转了 45°**。模型按后者理解 —— 于是每个朝向都被额外叠了
#: 一个 45° 转身：``down`` 变成斜四分之三侧面，``up`` 变成斜背面。
#: 实测产出的背面走路能看见半张脸和斜着的肩线，用户一眼就认出来了。
#:
#: 所以这里只描述相机在哪、不描述角色转了多少，转身交给 _FACING 说死。
_PERSPECTIVE = {
    "top_down_3_4": (
        "the camera placed slightly above and looking down at a shallow angle, "
        "the way a classic top-down JRPG renders its overworld — "
        "this describes only where the camera is, it does not rotate the character"
    ),
    "top_down": "the camera directly overhead looking straight down",
    "side_view": "the camera level with the subject, looking straight at it from the side",
    "isometric": "an isometric camera",
}

#: 角色绕自身竖轴转多少。**每一项都必须把肩线和脸的可见程度说死** ——
#: 只说 "back view" 不够，模型会理解成"大致背着"然后转 45°。
_FACING = {
    "down": (
        "turned to face the camera dead-on, shoulders square to the viewer, "
        "both eyes visible, the nose pointing straight at the camera — "
        "not angled, not turned to either side"
    ),
    "up": (
        "turned a full 180 degrees away from the camera, seen from directly behind, "
        "shoulders square to the viewer, the back of the head fully facing the camera, "
        "no part of the face visible — not even an ear, a cheek or the tip of the nose"
    ),
    "left": (
        "turned a full 90 degrees to the left, in exact side profile, "
        "shoulders in line with the direction of travel, only the left side of the body "
        "and one eye visible — not angled toward the camera"
    ),
    "right": (
        "turned a full 90 degrees to the right, in exact side profile, "
        "shoulders in line with the direction of travel, only the right side of the body "
        "and one eye visible — not angled toward the camera"
    ),
}

_SHADING = {
    "flat": "flat single-tone shading",
    "two_tone": "two-tone shading (one base tone plus one shadow tone)",
    "three_tone": "three-tone shading (base, shadow and highlight)",
}

_OUTLINE = {
    "none": "no outline",
    "single_pixel_dark": "a single-pixel dark outline",
    "single_pixel_colored": "a single-pixel outline tinted from the local colour",
}

_LIGHTING = {
    "fixed_top_left": "the light source fixed at the top-left",
    "fixed_top": "the light source fixed directly above",
    "fixed_top_right": "the light source fixed at the top-right",
    "none": "no directional lighting, the subject is self-lit",
}


def _orientation_block(direction: Direction | None, perspective: str) -> str:
    """相机俯角与角色转身分开说，且转身单独成段。

    合成一句 "... , {facing}, {perspective}." 时朝向被埋在逗号中间，
    模型把它当修饰语而不是硬约束。拆成独立一段、并明说"相机角度不改变朝向"
    之后才压得住那个多余的 45° 转身。
    """
    facing = _FACING[direction] if direction else "seen from one fixed angle throughout"
    return (
        f"Camera: {_PERSPECTIVE[perspective]}.\n\n"
        "Body orientation — this is a hard requirement, it applies to every single "
        f"cell and overrides any default framing:\n"
        f"the character is {facing}.\n"
        "The camera angle described above must not rotate the body away from this "
        "orientation."
    )


@dataclass(frozen=True, slots=True)
class CompiledPrompt:
    text: str
    key_color: str
    size: tuple[int, int]

    def __str__(self) -> str:  # pragma: no cover - 平凡实现
        return self.text


def _style_block(request: AssetRequest) -> str:
    style = request.style
    return (
        f"Style: crisp pixel art, {_OUTLINE[style.outline]}, "
        f"{_SHADING[style.shading]}, {_LIGHTING[style.lighting]}, "
        f"a limited palette of about {style.max_colors} colours, "
        f"hard pixel edges with no anti-aliasing."
    )


def _background_block(key_color: str) -> str:
    return (
        f"Background: one completely flat solid {key_color} colour filling every pixel "
        f"that is not the subject, including the space between cells. "
        f"The background must be a single uniform colour with no gradient and no texture."
    )


def compile_seed_prompt(request: AssetRequest, *, key_color: str) -> CompiledPrompt:
    """canonical seed 的 prompt。

    seed 是所有动画的身份基准，所以这里要把身份细节说满 ——
    后续每个动作网格都靠模型从这张图里读出这些细节。
    """
    from ..planning.grid_layout import seed_layout

    layout = seed_layout()
    style = request.style

    text = "\n\n".join(
        [
            "Pixel art character sprite, a single subject.",
            _orientation_block("down", style.perspective),
            f"Subject: {request.description.strip()}",
            _style_block(request),
            (
                "Composition: the full body is visible and centred, standing in a neutral "
                f"idle stance, the feet near the bottom, at least "
                f"{PROMPT_MARGIN_PERCENT}% empty background margin on all four sides."
            ),
            _background_block(key_color),
            "Do not include any of the following:\n"
            + negative_block(character=request.asset_type == "character"),
        ]
    )
    return CompiledPrompt(text=text, key_color=key_color, size=layout.size)


def compile_animation_prompt(
    request: AssetRequest,
    *,
    action: str,
    direction: Direction | None,
    frames: int,
    layout: GridLayout,
    key_color: str,
) -> CompiledPrompt:
    """动作网格的 prompt。

    包含 PLAN §8 Sprint 4 的全部固定约束，外加 Sprint 0 追加的逐帧姿势清单。
    """
    style = request.style

    identity = (
        "Identity constraints — these must be IDENTICAL in every single cell:\n"
        "- the same character as the reference image, same face and hair\n"
        "- the same outfit, same armour, same colours\n"
        "- the same weapon or held item, in the same hand\n"
        "- the same body proportions and the same overall size\n"
        "- the same body orientation, exactly the one stated above"
    )

    if layout.rows == 1:
        layout_rules = (
            f"Layout: this image is ONE single complete animation strip — "
            f"exactly {layout.cols} equally sized cells in ONE horizontal row, "
            f"read left to right.\n"
            f"Draw exactly {frames} poses, one per cell. "
            f"They are consecutive frames of one continuous cycle of one character, "
            f"not {frames} separate drawings: cell 2 continues the motion started in "
            f"cell 1, cell 3 continues cell 2, and the last cell loops back to the first."
        )
    else:
        layout_rules = (
            f"Layout: this image is ONE single complete animation sequence laid out as "
            f"a {layout.cols} columns x {layout.rows} rows grid of {layout.capacity} "
            f"equally sized cells, read left to right then top to bottom.\n"
            f"Draw exactly {frames} poses, one per cell. "
            f"They are consecutive frames of one continuous cycle of one character, "
            f"not {frames} separate drawings: each cell continues the motion of the "
            f"previous one, the last cell loops back to the first, and the second row "
            f"continues straight on from the end of the first row — the two rows are "
            f"one sequence, not two."
        )

    placement = (
        "Placement constraints:\n"
        "- draw each pose entirely inside its own cell, never touching or crossing "
        "a cell boundary\n"
        f"- leave at least {PROMPT_MARGIN_PERCENT}% empty background margin on all four "
        "sides of every pose\n"
        "- the full body must be visible in every cell\n"
        "- the feet of every pose must rest on the same horizontal baseline\n"
        "- every character must be drawn at exactly the same size\n"
        "- no cell may be empty and no two cells may be identical"
    )

    # 摇摆的真正来源：模型把每个格子当独立立绘画，于是同一个正面走路里
    # 有的格子身体略偏左、有的略偏右，播放起来角色左右晃。
    # 光说"朝向正对镜头"不够 —— 必须点名禁止"格子之间"的朝向变化。
    continuity = (
        "Frame-to-frame continuity — the single most common failure here:\n"
        "- the body orientation is LOCKED across all cells: if the character faces "
        "the camera in one cell it faces the camera in every cell, at exactly the "
        "same angle\n"
        "- do NOT let some cells lean or turn slightly left while others lean or turn "
        "slightly right — the shoulder line must have the same angle in every cell\n"
        "- only the limbs move between cells; the torso, head and shoulders keep the "
        "same facing throughout\n"
        "- do not mirror, flip or rotate the character between cells\n"
        "- when a pose description swaps which leg is forward, ONLY the legs and arms "
        "swap roles — this is NOT a mirror of the character. The weapon stays in the "
        "same hand, the cloak stays on the same shoulder, the parting of the hair stays "
        "on the same side, and every asymmetric detail stays exactly where it was\n"
        "- the head stays at the same horizontal position in every cell; the character "
        "must not drift sideways from cell to cell\n"
        "\n"
        "What is LOCKED is the orientation, NOT the motion. The limbs must move a lot:\n"
        "- the pose difference between neighbouring cells must be obvious at a glance "
        "when the cells are seen side by side\n"
        "- in the cells where the legs are described as striding, the gap between the "
        "two feet must be at least as wide as the character's shoulders\n"
        "- do not draw a row of near-identical standing poses with only tiny "
        "differences — that is the single most common way this comes out wrong"
    )

    poses = (
        f"The {frames} cells must show these DIFFERENT poses. "
        "This is an animation, not a set of standing portraits — "
        "the body must visibly change between cells:\n"
        + numbered_poses(action, frames, layout.cols, direction)
    )

    text = "\n\n".join(
        [
            f"Pixel art {action} animation sprite sheet for the SAME character "
            "as the reference image.",
            # 参考 agent-sprite-forge 的 character anchor sheet 措辞：告诉模型
            # 底图不是空白，而是一张已经摆好位置与大小的模板，它只需改姿势。
            "The image you are editing is a template: every cell already contains the "
            "exact same accepted character, at the correct size, with its feet on the "
            "correct ground line. Change ONLY the pose in each cell. Keep each cell's "
            "character size, body-root position, foot-contact line and padding exactly "
            "as the template has them. Never zoom or resize a pose to fill its cell.",
            _orientation_block(direction, style.perspective),
            layout_rules,
            poses,
            identity,
            continuity,
            placement,
            _style_block(request),
            _background_block(key_color),
            "Do not include any of the following:\n"
            + negative_block(
                character=request.asset_type == "character", action=action
            ),
        ]
    )
    return CompiledPrompt(text=text, key_color=key_color, size=layout.size)
