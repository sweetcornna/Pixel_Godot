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
from .poses import FRONTAL_DIRECTIONS, PoseCycle, cycle_from_beats, numbered_poses

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

_STATIC_PERSPECTIVE = {
    "top_down_3_4": "the camera slightly above the item, looking down at a shallow angle",
    "top_down": "the camera directly overhead, looking straight down at the item",
    "side_view": "the camera level with the item, looking straight at it from the side",
    "isometric": "an isometric camera looking at the item",
}

_STATIC_SUBJECT = {
    "pickup": "Crisp pixel art of one single isolated pickup item.",
    "weapon": "Crisp pixel art of one single isolated weapon.",
    "prop": "Crisp pixel art of one single isolated prop object.",
    "ui_icon": "Crisp pixel art of one single isolated UI icon.",
    "environment_object": (
        "Crisp pixel art of one single isolated environment object."
    ),
}

_WEAPON_ORIENTATION = (
    "Weapon orientation: place the weapon diagonally, with its blade tip or muzzle "
    "pointing toward the upper right, following the standard game icon convention."
)

_UI_ICON_CONVENTION = (
    "UI icon convention: show the icon straight-on in a front-facing view, with no "
    "ground contact or cast shadow, and keep its silhouette clearly readable."
)

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


def _stride_rule(direction: str | None) -> str:
    """跨步幅度的写法**必须分视角**，否则会写出一个正面看的侧视姿势。

    原来这条对所有视角都写"striding 的格子里两脚间距至少一个肩宽"。肩宽的
    左右间距在侧视里是跨步，在正视里是**劈叉** —— 模型为了同时满足"正对镜头"
    和"两脚拉开一肩宽"，只能把躯干画成正面、把腿画成侧视，出来是个拼接怪物。
    用户看到的就是"走路朝向怎么是侧着的"。

    正视里跨步是**朝镜头方向**的，画面上表现为抬脚、脚在画布上更低更靠前、
    以及整个身体的上下起伏 —— 横向间距始终不超过胯宽。
    """
    if direction in FRONTAL_DIRECTIONS:
        return (
            "- this is a FRONT/BACK view: a step travels TOWARDS or AWAY from the camera, "
            "not sideways across the cell. Show it by lifting one foot clear of the ground "
            "and drawing it lower on the canvas (nearer the viewer) than the planted foot, "
            "plus the up-and-down bob of the whole body\n"
            "- the two feet NEVER separate sideways by more than the character's hips. "
            "A wide sideways stance is a side view; drawing one here gives a front-facing "
            "torso on side-facing legs, which is the worst artefact this sheet can have\n"
            "- both feet keep their toes pointing at the camera in every cell. No cell may "
            "show a leg, knee or foot in profile\n"
        )
    return (
        "- this is a SIDE view: in the cells where the legs are described as striding, "
        "the gap between the two feet must be at least as wide as the character's "
        "shoulders\n"
    )


def _custom_cycle(request: AssetRequest, action: str) -> PoseCycle | None:
    """取该动作在 request 里自带的节拍。内置动作返回 None（用内置模板）。"""
    for spec in request.animation_list():
        if spec.name == action and spec.beats:
            return cycle_from_beats(
                [(beat.name, beat.description) for beat in spec.beats], spec.cycle
            )
    return None


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


def compile_static_prompt(request: AssetRequest, *, key_color: str) -> CompiledPrompt:
    """单张静态资产的 prompt；不复用任何角色或动作模板。"""
    from ..planning.grid_layout import seed_layout

    layout = seed_layout()
    colors = request.style.palette_colors
    palette = (
        "Explicit palette — use only these colours for the item: " + ", ".join(colors) + "."
        if colors is not None
        else f"Use at most {request.style.max_colors} colours for the item."
    )
    blocks = [
        _STATIC_SUBJECT.get(
            request.asset_type,
            "Crisp pixel art of one single isolated item.",
        ),
        f"Subject: {request.description.strip()}",
        f"Camera: {_STATIC_PERSPECTIVE[request.style.perspective]}.",
        _style_block(request),
        palette,
        (
            "Composition: place the item at the exact center of the square canvas, "
            f"with at least {PROMPT_MARGIN_PERCENT}% empty background margin on all "
            "four sides. Keep the entire item visible and separated from every edge."
        ),
    ]
    if request.asset_type == "weapon":
        blocks.append(_WEAPON_ORIENTATION)
    if request.asset_type == "ui_icon":
        blocks.append(_UI_ICON_CONVENTION)
    blocks.extend(
        [
            (
                f"Background: one completely flat solid {key_color} colour filling every "
                "pixel outside the item, including all margin around it. The background "
                "must be uniform, with no gradient, texture or vignette."
            ),
            (
                "Exclude people, humanoids, creatures, faces, limbs, text, labels, numbers, "
                "watermarks, scenery, ground planes, horizons, shadows, glow, bloom, blur, "
                "photorealism, soft edges, anti-aliasing, and every second object."
            ),
        ]
    )
    text = "\n\n".join(blocks)
    return CompiledPrompt(text=text, key_color=key_color, size=layout.size)


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

    自定义动作（request 里带 ``beats`` 的）走同一条编译链 —— 只是节拍来自
    请求而不是内置模板。逐帧写死姿势这条约束对它们同样成立。
    """
    style = request.style
    cycle = _custom_cycle(request, action)

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
        # 小恶魔的走路：腿几乎不动，翅膀却每格换一个展幅，播起来就是"鬼畜"。
        # 模型需要被告知**哪个部件负责表现运动**。
        "- the LEGS are what carry the motion. Wings, cloak, cape, tail, hair and scarf "
        "may trail or sway very slightly, but they keep the same span, the same shape "
        "and the same silhouette in every cell — never spread a wing wide in one cell "
        "and fold it in the next\n"
        # 史莱姆的走路：模型照"左脚向前迈"的描述给一团没有腿的身体现编了两条腿。
        # 同一个角色四个动作是圆团、第五个长出了腿，就是"形象不统一"。
        "- use ONLY the body parts the character in the template actually has. If it has "
        "no legs, no arms, no head or no hands, do NOT invent them — express the pose "
        "with the parts it does have (a legless body squashes, stretches and hops; a "
        "floating body bobs and tilts). Adding a limb the template does not have is the "
        "worst failure possible: it turns the sequence into a different character\n"
        "\n"
        "What is LOCKED is the orientation, NOT the motion. The limbs must move a lot:\n"
        "- the pose difference between neighbouring cells must be obvious at a glance "
        "when the cells are seen side by side\n"
        + _stride_rule(direction)
        + "- do not draw a row of near-identical standing poses with only tiny "
        "differences — that is the single most common way this comes out wrong\n"
        "\n"
        "Sidedness — a pose that involves ONE arm, ONE leg or ONE shoulder must use "
        "the SAME one in every cell where it appears. Do not let the character's left "
        "and right swap places partway through the sequence: if the weapon is in the "
        "right hand it is in the right hand in every cell, if the character falls to "
        "one side it falls to that side in every cell that shows the fall."
    )

    poses = (
        f"The {frames} cells must show these DIFFERENT poses. "
        "This is an animation, not a set of standing portraits — "
        "the body must visibly change between cells:\n"
        + numbered_poses(
            action, frames, layout.cols, direction, cycle, request.resolved_locomotion
        )
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
