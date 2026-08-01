"""Prompt Compiler 与逐帧姿势模板。

编译是纯函数（PLAN §2.7），所以这一层可以被完全断言 —— 与生成层不同。

用例围绕 Sprint 0 的结论展开：**整体描述会产出 N 张几乎一样的站姿**，
所以每一格该画什么必须写死，且**不允许出现重复描述** ——
prompt 里明写着 no two cells may be identical，再递重复描述就是自相矛盾的指令。
"""

from __future__ import annotations

import pytest
import yaml

from pixel_asset_forge.constants import ALLOWED_FRAME_COUNTS
from pixel_asset_forge.errors import PlanError
from pixel_asset_forge.models import load_pack, load_request, parse_request
from pixel_asset_forge.models.request import infer_locomotion
from pixel_asset_forge.planning import grid_for_frames, layout_for_frames
from pixel_asset_forge.prompts import (
    PROMPT_MARGIN_PERCENT,
    compile_animation_prompt,
    compile_seed_prompt,
    compile_static_prompt,
    numbered_poses,
    pose_sequence,
)
from pixel_asset_forge.prompts.poses import POSE_CYCLES

FRAME_COUNTS = (4, 6, 8, 9, 12)


# -- 姿势序列 --------------------------------------------------------------


@pytest.mark.parametrize("action", sorted(POSE_CYCLES))
@pytest.mark.parametrize("frames", FRAME_COUNTS)
def test_every_combination_is_unique_and_complete(action: str, frames: int) -> None:
    """9 动作 × 5 档位 = 45 组，每组都必须给出 N 条互不相同的描述。"""
    seq = pose_sequence(action, frames)
    assert len(seq) == frames
    assert len(set(seq)) == frames, f"{action} {frames} 帧出现重复描述"
    assert all(s.strip() for s in seq)


def test_walk_is_built_from_two_mirrored_half_cycles() -> None:
    """步态类动作左右各一遍 —— N/2 处必须是换腿的那一帧。"""
    seq = pose_sequence("walk", 8)
    first, second = seq[:4], seq[4:]
    assert first != second
    # 第二个半周期是第一个的左右互换
    assert second[0].replace("right", "\x00").replace("left", "right").replace(
        "\x00", "left"
    ) == first[0]


def test_walk_covers_the_whole_cycle_at_every_budget() -> None:
    """每个档位都要覆盖完整周期，不能把 PASSING 之类的关键帧整段丢掉。"""
    for frames in (4, 6, 8, 12):
        joined = " ".join(pose_sequence("walk", frames))
        assert "CONTACT" in joined
        assert "PASSING" in joined


def test_one_shot_actions_keep_their_first_and_last_beat() -> None:
    """一次性动作丢了起手或收势就不成立了。"""
    for action, first, last in (
        ("attack", "WINDUP", "RECOVER"),
        ("hurt", "IMPACT", "RECOVER"),
        ("death", "STAGGER", "STILL"),
    ):
        seq = pose_sequence(action, 4)
        assert seq[0].startswith(first)
        assert seq[-1].startswith(last)


def test_upsampling_produces_transitions_not_duplicates() -> None:
    """节拍少于帧数时要插过渡帧。复制同一拍会与"不得有相同格"自相矛盾。"""
    seq = pose_sequence("cast", 12)  # cast 只有 4 拍
    assert len(set(seq)) == 12
    assert any("% of the way from" in s for s in seq)


def test_unknown_action_fails_loudly() -> None:
    """静默退回泛泛描述 = 把已知失败模式请回来。"""
    with pytest.raises(PlanError) as exc:
        pose_sequence("moonwalk", 8)
    assert "泛泛" in exc.value.message


def test_numbered_poses_state_the_row_and_column() -> None:
    """只写"第 N 格"模型未必按同一种阅读顺序理解（PLAN §2.3.1）。"""
    text = numbered_poses("walk", 8, cols=4)
    assert "Cell 1 (row 1, column 1)" in text
    assert "Cell 5 (row 2, column 1)" in text
    assert "Cell 8 (row 2, column 4)" in text


# -- Prompt 编译 -----------------------------------------------------------


def test_static_prompt_is_item_only_and_uses_explicit_palette(examples_dir) -> None:
    request = load_pack(examples_dir / "potion_pack.yaml").expand_requests()[0]
    prompt = compile_static_prompt(request, key_color="#FF00FF").text
    lowered = prompt.lower()

    assert "exact center" in lowered
    assert "12% empty background margin" in lowered
    for color in request.style.palette_colors or ():
        assert color in prompt
    for banned in ("character", "full body", "feet", "animation"):
        assert banned not in lowered





@pytest.fixture
def knight(examples_dir):
    return load_request(examples_dir / "knight.yaml")


def test_compilation_is_a_pure_function(knight) -> None:
    layout = grid_for_frames(8)
    a = compile_animation_prompt(
        knight, action="walk", direction="down", frames=8, layout=layout,
        key_color="#FF00FF",
    )
    b = compile_animation_prompt(
        knight, action="walk", direction="down", frames=8, layout=layout,
        key_color="#FF00FF",
    )
    assert a.text == b.text


def test_animation_prompt_carries_every_mandatory_constraint(knight) -> None:
    """PLAN §8 Sprint 4 的清单，每一条都对应一个已知失败模式。"""
    prompt = compile_animation_prompt(
        knight, action="walk", direction="down", frames=8,
        layout=grid_for_frames(8), key_color="#FF00FF",
    ).text

    for fragment in (
        "4 columns x 2 rows",            # 网格布局
        "left to right then top to bottom",  # 帧序（PLAN §2.3.1）
        "Draw exactly 8 poses, one per cell",
        "never touching or crossing a cell boundary",  # 越界（PLAN §2.3.2）
        "same weapon or held item, in the same hand",  # 身份一致性
        "same horizontal baseline",      # 脚底对齐
        "no cell may be empty",
        "#FF00FF",                       # 键控色非硬编码
    ):
        assert fragment in prompt, f"prompt 缺少约束：{fragment}"


def test_margin_is_stated_as_twelve_percent(knight) -> None:
    """写 12 判 8：实测模型把边距要求打约七折执行（Sprint 0 / A-4）。"""
    assert PROMPT_MARGIN_PERCENT == 12
    prompt = compile_animation_prompt(
        knight, action="walk", direction="down", frames=8,
        layout=grid_for_frames(8), key_color="#FF00FF",
    ).text
    assert "12% empty background margin" in prompt


def test_prompt_says_this_is_an_animation_not_portraits(knight) -> None:
    """Sprint 0 的教训：不说这句，模型给的就是 N 张站姿。"""
    prompt = compile_animation_prompt(
        knight, action="walk", direction="down", frames=8,
        layout=grid_for_frames(8), key_color="#FF00FF",
    ).text
    assert "not a set of standing portraits" in prompt


def test_key_color_follows_the_conflict_downgrade(examples_dir) -> None:
    """史莱姆会降级到纯绿，prompt 必须跟着走，不能写死洋红。"""
    slime = load_request(examples_dir / "slime.yaml")
    prompt = compile_animation_prompt(
        slime, action="walk", direction="down", frames=6,
        layout=grid_for_frames(6), key_color="#00FF00",
    ).text
    assert "#00FF00" in prompt
    assert "#FF00FF" not in prompt


def test_up_direction_says_the_face_is_not_visible(knight) -> None:
    """背面身份一致性最难（A-7），至少要让模型知道看不到脸。"""
    prompt = compile_animation_prompt(
        knight, action="walk", direction="up", frames=8,
        layout=grid_for_frames(8), key_color="#FF00FF",
    ).text
    assert "directly behind" in prompt
    assert "no part of the face visible" in prompt


def test_no_prompt_ever_says_three_quarter(knight) -> None:
    """"3/4" 在游戏开发里指相机俯角，在角色美术里指角色转了 45°。

    模型按后者理解，于是每个朝向都被多叠一个 45° 转身 —— 背面走路能看见
    半张脸和斜肩线。这个词在 prompt 里出现一次就够毁掉整批朝向。
    """
    from pixel_asset_forge.prompts import compile_seed_prompt

    prompts = [compile_seed_prompt(knight, key_color="#FF00FF").text]
    for direction in ("down", "up", "left", "right"):
        prompts.append(compile_animation_prompt(
            knight, action="walk", direction=direction, frames=8,
            layout=grid_for_frames(8), key_color="#FF00FF",
        ).text)

    for prompt in prompts:
        assert "3/4" not in prompt
        assert "three-quarter" not in prompt.replace(
            "no three-quarter turn", ""
        ), "只允许出现在负面清单里"


def test_orientation_is_its_own_block_not_a_clause(knight) -> None:
    """朝向埋在 "..., {facing}, {perspective}." 的逗号中间时会被当修饰语。"""
    prompt = compile_animation_prompt(
        knight, action="walk", direction="left", frames=8,
        layout=grid_for_frames(8), key_color="#FF00FF",
    ).text
    assert "Body orientation" in prompt
    assert "must not rotate the body away from this orientation" in prompt
    assert "turned a full 90 degrees to the left" in prompt


def test_negative_rules_cover_the_costly_failures(knight) -> None:
    prompt = compile_animation_prompt(
        knight, action="walk", direction="down", frames=8,
        layout=grid_for_frames(8), key_color="#FF00FF",
    ).text
    for fragment in (
        "no text, no labels, no numbers",   # 会被切进帧里
        "no visible grid lines",            # 会被判为跨格连通域
        "no drop shadows",                  # 紧贴脚底，键控抠不掉
        "no glow",                          # 污染键控边缘与调色板
        "no background gradient",           # 摧毁色键的双峰前提
        "no anti-aliasing",
    ):
        assert fragment in prompt, f"缺少负面约束：{fragment}"


def test_seed_prompt_describes_the_subject_and_composition(knight) -> None:
    prompt = compile_seed_prompt(knight, key_color="#FF00FF")
    assert prompt.size == (1024, 1024)
    assert "forest knight" in prompt.text
    assert "full body is visible" in prompt.text
    assert f"{PROMPT_MARGIN_PERCENT}% empty background margin" in prompt.text


def test_seed_prompt_reflects_style_choices(minimal_request) -> None:
    minimal_request["style"]["outline"] = "none"
    minimal_request["style"]["shading"] = "three_tone"
    minimal_request["style"]["lighting"] = "none"
    prompt = compile_seed_prompt(parse_request(minimal_request), key_color="#FF00FF").text
    assert "no outline" in prompt
    assert "three-tone shading" in prompt
    assert "self-lit" in prompt


def test_directionless_animation_compiles(examples_dir) -> None:
    """爆炸是各向同性的，没有方向可言。"""
    fireball = load_request(examples_dir / "fireball.yaml")
    prompt = compile_animation_prompt(
        fireball, action="impact", direction=None, frames=6,
        layout=grid_for_frames(6), key_color="#FF00FF",
    ).text
    assert "fixed angle" in prompt


def test_prompt_size_matches_the_layout(knight) -> None:
    layout = grid_for_frames(12)
    prompt = compile_animation_prompt(
        knight, action="walk", direction="down", frames=12, layout=layout,
        key_color="#FF00FF",
    )
    assert prompt.size == layout.size


def test_the_sheet_is_described_as_one_continuous_sequence(knight) -> None:
    """模型把每个格子当独立立绘画时，同一个正面走路里有的格子身体略偏左、
    有的略偏右 —— 播放起来角色左右晃。用户实测到的就是这个。
    """
    prompt = compile_animation_prompt(
        knight, action="walk", direction="down", frames=8,
        layout=layout_for_frames(8), key_color="#FF00FF",
    ).text
    assert "ONE single complete animation sequence" in prompt
    assert "the two rows are one sequence, not two" in prompt
    assert "the body orientation is LOCKED across all cells" in prompt
    assert "while others lean or turn slightly right" in prompt


def test_a_single_row_strip_says_so_explicitly(knight) -> None:
    layout = layout_for_frames(6)
    assert (layout.cols, layout.rows) == (6, 1)
    prompt = compile_animation_prompt(
        knight, action="walk", direction="down", frames=6,
        layout=layout, key_color="#FF00FF",
    ).text
    assert "ONE single complete animation strip" in prompt
    assert "in ONE horizontal row" in prompt


def test_eight_frames_cannot_be_a_single_row(knight) -> None:
    """单行 N 格的整幅长宽比是 N × 格子宽高比，而 API 限制 ≤ 3。

    8 帧要单行就得把格子压到 0.375 宽高比 —— 装不下带跨步和佩剑的角色。
    这个比例与图放多大无关，所以放大整幅图并不能松绑。
    """
    from pixel_asset_forge.errors import GridLayoutError
    from pixel_asset_forge.planning import strip_for_frames

    with pytest.raises(GridLayoutError, match=r"0\.375"):
        strip_for_frames(8)
    assert layout_for_frames(8).rows == 2, "排不下单行时必须自动退回多行"


def test_fewer_frames_than_beats_picks_beats_instead_of_interpolating(knight) -> None:
    """插值的用途是"帧数多于节拍数"。帧数更少时还插值，会写出
    "about 33% of the way from DOWN to PASSING: starting from (...) and
    moving towards (...)" 这种描述 —— 图像模型据此画不出确定姿势，
    实测被平均成站姿，六格里腿完全不动。
    """
    seq = pose_sequence("walk", 6)          # walk 半周期 4 拍，每半只要 3 帧
    assert len(set(seq)) == 6
    assert not any("of the way from" in s for s in seq), "帧数不多于节拍数时不该插值"
    assert seq[0].startswith("CONTACT")
    assert seq[1].startswith("PASSING")


def test_the_prompt_demands_visible_motion_not_just_consistency(knight) -> None:
    """朝向锁死是为了不摇摆，但锁过头模型会交出一排几乎一样的站姿 ——
    Sprint 0 踩过一次，加了连续性约束后又踩了一次。必须有反向配重。
    """
    for direction, counterweight in [
        # 反向配重的**写法分视角**：侧视是拉开跨步，正视是抬脚加起伏
        ("left", "at least as wide as the character's shoulders"),
        ("down", "lifting one foot clear of the ground"),
    ]:
        prompt = compile_animation_prompt(
            knight, action="walk", direction=direction, frames=6,
            layout=layout_for_frames(6), key_color="#FF00FF",
        ).text
        assert "What is LOCKED is the orientation, NOT the motion" in prompt
        assert counterweight in prompt, direction
        assert "row of near-identical standing poses" in prompt


def test_a_leg_swap_is_not_a_mirror(knight) -> None:
    """步态第二个半周期把左右腿对调（"the RIGHT leg strides far forward"），
    模型常把这个对调理解成整体镜像 —— 剑换到另一只手、斗篷翻到另一边。
    用户实测在 walk_down 的后几帧上看到了。
    """
    prompt = compile_animation_prompt(
        knight, action="walk", direction="down", frames=6,
        layout=layout_for_frames(6), key_color="#FF00FF",
    ).text
    assert "ONLY the legs and arms swap roles" in prompt
    assert "The weapon stays in the same hand" in prompt


def test_the_prompt_tells_the_model_the_base_image_is_a_template(knight) -> None:
    """base image 是 anchor sheet 而不是空白画布 —— 不说这件事，
    模型会把每格已有的角色当成"要被替换掉的内容"。
    """
    prompt = compile_animation_prompt(
        knight, action="walk", direction="down", frames=6,
        layout=layout_for_frames(6), key_color="#FF00FF",
    ).text
    assert "The image you are editing is a template" in prompt
    assert "Change ONLY the pose in each cell" in prompt
    assert "Never zoom or resize a pose to fill its cell" in prompt


def test_swing_actions_are_body_only(knight) -> None:
    """脱体特效会把包围盒撑到远大于 idle，脚线锚点与跨动作缩放基准双双失准 ——
    角色在 attack 与 idle 之间忽大忽小、脚还离地。
    特效该单独出 fx 表在引擎里叠加（agent-sprite-forge 的 body-only 规则）。
    """
    attack = compile_animation_prompt(
        knight, action="attack", direction="down", frames=4,
        layout=layout_for_frames(4), key_color="#FF00FF",
    ).text
    assert "no detached slash arc, no weapon trail" in attack
    assert "no muzzle flash, no projectile" in attack

    walk = compile_animation_prompt(
        knight, action="walk", direction="down", frames=4,
        layout=layout_for_frames(4), key_color="#FF00FF",
    ).text
    assert "no detached slash arc" not in walk, "走路不该背这条约束"


def test_frontal_and_side_walks_use_different_beats(knight) -> None:
    """CONTACT / DOWN / PASSING / UP 是**侧视**行走周期，描述的是从侧面
    才看得见的上下起伏与蹬地。正面朝向镜头时"身体降到最低点、用脚尖蹬地"
    根本读不出来，模型只能瞎猜 —— 用户实测"动作很奇怪"就是这么来的。

    俯视 RPG 的正面行走靠另一套线索：哪只脚在前、双脚是否并拢
    （agent-sprite-forge 的四帧步）。
    """
    front = pose_sequence("walk", 4, "down")
    side = pose_sequence("walk", 4, "left")
    assert front != side

    assert front[0].startswith("NEUTRAL") and front[1].startswith("STRIDE")
    assert front[2].startswith("NEUTRAL") and front[3].startswith("STRIDE")
    assert not any("lowest point" in s for s in front), "正面看不出身体的上下起伏"
    assert all("PASSING" in s or "CONTACT" in s for s in side)


def test_the_back_view_walks_like_the_front_view(knight) -> None:
    """背面同样看不到侧向起伏。"""
    assert pose_sequence("walk", 4, "up") == pose_sequence("walk", 4, "down")


def test_a_cyclic_subsample_never_drops_the_passing_pose() -> None:
    """4 拍取 3 帧有三种取法，差别很实在：

        int   → CONTACT·DOWN·PASSING  没有最高点，只沉不起
        round → CONTACT·DOWN·UP       丢掉了 PASSING
        ceil  → CONTACT·PASSING·UP    交叉腿在，且是上升的

    PASSING 是双腿交叉那一拍，行走最强的可读线索 —— 宁可丢极值也不能丢它。
    """
    for frames in (4, 6, 8):
        side = pose_sequence("walk", frames, "left")
        assert any(s.startswith("PASSING") for s in side), f"{frames} 帧丢了 PASSING"


def test_the_neutral_beat_survives_the_left_right_swap() -> None:
    """half_cycle 靠左右互换生成后半周期。描述里没有 left/right 的拍子
    互换后与原文一字不差，与 "no two cells may be identical" 直接冲突。
    """
    front = pose_sequence("walk", 6, "down")
    assert len(set(front)) == 6
    assert front[0] != front[3]


# -- 移动形态 ---------------------------------------------------------------
#
# 用户报的"形象不统一"：史莱姆的 idle / attack / hurt / death 都是无腿圆团，
# 唯独 walk 被模型长出了两条腿和脚 —— 因为 walk 的节拍句句写"左脚 / 右脚"。


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("a small blue slime with a glossy highlight", "legless"),
        ("一只蓝色史莱姆，圆润有光泽", "legless"),
        ("a translucent ghost in tattered robes", "floating"),
        ("a grey wolf with bristling fur", "quadruped"),
        ("a hooded elf archer with a longbow", "biped"),
    ],
)
def test_locomotion_is_inferred_from_the_description(description, expected) -> None:
    assert infer_locomotion(description) == expected


def test_a_legless_walk_never_asks_for_feet() -> None:
    """没有腿的角色，走路的描述里就不能出现腿和脚。

    出现了模型就会把腿画出来 —— 这是"同一个角色四个动作一个样、
    第五个换了物种"的直接成因。
    """
    text = " ".join(pose_sequence("walk", 6, "down", None, "legless")).lower()
    for banned in ("foot", "feet", "leg", "heel", "toe", "knee"):
        assert banned not in text, f"无腿角色的走路描述里出现了 {banned!r}"


def test_a_legless_walk_still_shows_locomotion() -> None:
    """不能因为去掉了腿就退化成"原地待机"。"""
    text = " ".join(pose_sequence("walk", 6, "down", None, "legless")).lower()
    assert "ground" in text
    assert "squash" in text or "compress" in text
    assert "stretch" in text


def test_explicit_locomotion_beats_the_inferred_one(examples_dir) -> None:
    raw = yaml.safe_load((examples_dir / "knight.yaml").read_text(encoding="utf-8"))
    raw["locomotion"] = "legless"
    request = parse_request(raw)
    assert request.resolved_locomotion == "legless"

    prompt = compile_animation_prompt(
        request, action="walk", direction="down", frames=6,
        layout=layout_for_frames(6), key_color="#FF00FF",
    )
    assert "squashes" in prompt.text or "SQUASH" in prompt.text


def test_the_prompt_forbids_inventing_limbs(knight) -> None:
    prompt = compile_animation_prompt(
        knight, action="walk", direction="down", frames=6,
        layout=layout_for_frames(6), key_color="#FF00FF",
    )
    lowered = prompt.text.lower()
    assert "only the body parts the character in the template actually has" in lowered
    assert "do not invent them" in lowered


def test_the_prompt_pins_wings_and_cloaks_so_they_do_not_flap(knight) -> None:
    """小恶魔走路时腿几乎不动、翅膀每格换展幅 —— 播起来就是"鬼畜"。"""
    prompt = compile_animation_prompt(
        knight, action="walk", direction="down", frames=6,
        layout=layout_for_frames(6), key_color="#FF00FF",
    )
    lowered = prompt.text.lower()
    assert "the legs are what carry the motion" in lowered
    assert "same span" in lowered


def test_the_frontal_walk_states_the_body_height_every_beat() -> None:
    """正面走路的主线索是上下起伏。不写高度，模型会拿左右偏移凑帧间差异。"""
    beats = pose_sequence("walk", 4, "down")
    cues = ("lowest", "highest", "mid height", "lower")
    stated = [b for b in beats if any(cue in b.lower() for cue in cues)]
    assert len(stated) == len(beats), beats


# -- 正视步态不能用侧视措辞 --------------------------------------------------
#
# 用户报「走路朝向怎么是侧着的」：躯干、头、翅膀正对镜头，腿却是侧视的，
# 一条腿甩到身侧老远、脚尖朝外。成因是两条指令都在要求水平位移。


@pytest.mark.parametrize("direction", ["down", "up"])
def test_a_frontal_walk_never_asks_for_horizontal_travel(direction) -> None:
    """"向前迈""在后""跨到最开"都是**侧视**才成立的说法。

    正面看，往前迈是朝镜头走，画面上没有水平位移。要求它，模型只能把躯干
    画成正面、把腿画成侧视劈开。
    """
    text = " ".join(pose_sequence("walk", 6, direction)).lower()
    for banned in ("far forward", "far back", "steps forward", "well behind", "widest"):
        assert banned not in text, f"正视步态里出现了侧视措辞 {banned!r}"


def test_a_frontal_walk_pins_the_feet_inside_the_body(knight) -> None:
    prompt = compile_animation_prompt(
        knight, action="walk", direction="down", frames=6,
        layout=layout_for_frames(6), key_color="#FF00FF",
    ).text.lower()
    assert "never separate sideways by more than the character's hips" in prompt
    assert "toes pointing at the camera" in prompt
    # 那条"两脚拉开一肩宽"的规则只该出现在侧视里
    assert "at least as wide as the character's shoulders" not in prompt


def test_a_side_walk_still_demands_a_wide_stride(knight) -> None:
    prompt = compile_animation_prompt(
        knight, action="walk", direction="left", frames=6,
        layout=layout_for_frames(6), key_color="#FF00FF",
    ).text.lower()
    assert "at least as wide as the character's shoulders" in prompt
    assert "never separate sideways" not in prompt


def test_the_frontal_walk_still_reads_as_walking() -> None:
    """去掉水平位移之后，抬脚与起伏这两条线索必须还在 —— 否则就成了原地站着。"""
    text = " ".join(pose_sequence("walk", 6, "down")).lower()
    assert "lifts clear of the ground" in text
    assert "lowest" in text and "highest" in text


@pytest.mark.parametrize("locomotion", ["biped", "legless", "floating", "quadruped"])
@pytest.mark.parametrize("direction", ["down", "up", "left", "right"])
@pytest.mark.parametrize("frames", ALLOWED_FRAME_COUNTS)
def test_every_locomotion_produces_unique_poses(locomotion, direction, frames) -> None:
    """每个移动形态 × 每个方向 × 每个帧数档位都不能出现重复描述。

    ``half_cycle`` 的后半周期靠左右互换生成 —— 描述里一个 left/right 都没有的
    节拍，互换后与原文一字不差。实测四足的 NEUTRAL 与 SUSPEND 两拍都踩过，
    ``create-animation`` 直接报错。这条参数化就是为了别再靠人去逐拍检查。
    """
    poses = pose_sequence("walk", frames, direction, None, locomotion)
    assert len(set(poses)) == len(poses) == frames
