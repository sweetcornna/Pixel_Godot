"""任意动作（Sprint 6.8.4）。

内置动作只有九个，用户想要的远不止。让自描述的动作走**同一条**
「节拍 → 逐帧描述」编译链 —— 节拍由请求给出，不由代码猜。

这一条与 `pose_sequence` 对未知动作抛错是同一个原则：拿不准就报错。
静默退回泛泛描述会产出一排几乎一样的站姿（Sprint 0 / A-2）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pixel_asset_forge.errors import PlanError, RequestValidationError
from pixel_asset_forge.models import parse_request
from pixel_asset_forge.models.validation import thresholds_for
from pixel_asset_forge.planning import layout_for_frames
from pixel_asset_forge.prompts import compile_animation_prompt
from pixel_asset_forge.prompts.poses import cycle_from_beats, pose_sequence

ROLL_BEATS = [
    {"name": "CROUCH", "description": "the knees bend deeply, the arms tuck in"},
    {"name": "TUCK", "description": "the body curls into a tight ball, shoulder leading"},
    {"name": "ROLL", "description": "upside down mid-roll, the feet up overhead"},
    {"name": "RISE", "description": "coming out of the roll onto one knee, torso rising"},
]


def request_data(examples_dir: Path, animation: dict) -> dict:
    import yaml

    data = yaml.safe_load((examples_dir / "knight.yaml").read_text(encoding="utf-8"))
    data["animations"] = [animation]
    return data


# -- 拒绝猜测 --------------------------------------------------------------


def test_an_unknown_action_without_beats_is_refused(examples_dir: Path) -> None:
    """代码不替用户猜 dodge_roll 该怎么画。"""
    with pytest.raises(RequestValidationError) as exc:
        parse_request(request_data(examples_dir, {
            "name": "dodge_roll", "directions": ["down"],
            "frames": 4, "fps": 14, "loop": False,
        }))
    assert "beats" in str(exc.value) or "beats" in repr(exc.value.errors)


def test_a_builtin_action_may_not_override_its_beats(examples_dir: Path) -> None:
    """内置动作已有模板。允许覆盖会让"walk 到底是哪套节拍"变成薛定谔的。"""
    with pytest.raises(RequestValidationError):
        parse_request(request_data(examples_dir, {
            "name": "walk", "directions": ["down"],
            "frames": 4, "fps": 10, "loop": True, "beats": ROLL_BEATS,
        }))


def test_a_single_beat_is_not_an_animation(examples_dir: Path) -> None:
    with pytest.raises(RequestValidationError):
        parse_request(request_data(examples_dir, {
            "name": "wave", "directions": ["down"], "frames": 4, "fps": 8,
            "beats": ROLL_BEATS[:1],
        }))


def test_an_action_name_may_not_contain_a_separator(examples_dir: Path) -> None:
    """动作键是 ``{action}_{direction}``。名字里再有分隔符就反解不出方向了。"""
    with pytest.raises(RequestValidationError):
        parse_request(request_data(examples_dir, {
            "name": "dodge-roll", "directions": ["down"], "frames": 4, "fps": 8,
            "beats": ROLL_BEATS,
        }))


def test_a_gait_needs_sides_in_every_beat(examples_dir: Path) -> None:
    """gait 的后半周期靠左右互换生成。没有 left/right 的拍互换后与原文一字不差，
    与 prompt 里 "no two cells may be identical" 直接冲突。
    """
    with pytest.raises(RequestValidationError) as exc:
        parse_request(request_data(examples_dir, {
            "name": "limp", "directions": ["down"], "frames": 4, "fps": 8,
            "cycle": "gait",
            "beats": [
                {"name": "DRAG", "description": "one leg drags behind the body"},
                {"name": "HOP", "description": "a short hop forward on the good leg"},
            ],
        }))
    assert "left/right" in str(exc.value) or "DRAG" in repr(exc.value.errors)


# -- 编译 ------------------------------------------------------------------


def test_a_custom_action_compiles_through_the_same_chain(examples_dir: Path) -> None:
    request = parse_request(request_data(examples_dir, {
        "name": "dodge_roll", "directions": ["down"], "frames": 4, "fps": 14,
        "loop": False, "cycle": "one_shot", "beats": ROLL_BEATS,
    }))
    prompt = compile_animation_prompt(
        request, action="dodge_roll", direction="down", frames=4,
        layout=layout_for_frames(4), key_color="#FF00FF",
    ).text

    # 逐格姿势写死 —— 与内置动作同等待遇
    for beat in ROLL_BEATS:
        assert beat["name"] in prompt
        assert beat["description"] in prompt
    # 其余约束一条不少
    assert "Body orientation" in prompt
    assert "the body orientation is LOCKED across all cells" in prompt
    assert "no cell may be empty" in prompt


def test_one_shot_custom_actions_keep_their_first_and_last_beat() -> None:
    """丢了起手或收势，一次性动作就不成立了。"""
    cycle = cycle_from_beats([(b["name"], b["description"]) for b in ROLL_BEATS],
                             "one_shot")
    seq = pose_sequence("dodge_roll", 2, "down", cycle)
    assert seq[0].startswith("CROUCH")
    assert seq[-1].startswith("RISE")


def test_a_custom_loop_samples_cyclically() -> None:
    cycle = cycle_from_beats([(b["name"], b["description"]) for b in ROLL_BEATS], "loop")
    seq = pose_sequence("hover", 4, "down", cycle)
    assert len(set(seq)) == 4


def test_a_custom_gait_mirrors_the_second_half() -> None:
    cycle = cycle_from_beats(
        [("PUSH", "the left leg pushes off hard"),
         ("GLIDE", "gliding on the right leg, the left tucked up")],
        "gait",
    )
    seq = pose_sequence("skate", 4, "left", cycle)
    assert len(set(seq)) == 4
    assert "left leg pushes" in seq[0] and "right leg pushes" in seq[2]


def test_frames_beyond_the_beat_count_still_interpolate() -> None:
    """自定义动作也吃得下"帧数多于节拍数"这条既有路径。"""
    cycle = cycle_from_beats([(b["name"], b["description"]) for b in ROLL_BEATS],
                             "one_shot")
    seq = pose_sequence("dodge_roll", 8, "down", cycle)
    assert len(set(seq)) == 8
    assert any("% of the way from" in s for s in seq)


# -- 验证阈值 --------------------------------------------------------------


def test_a_custom_action_has_no_geometry_thresholds() -> None:
    """不知道一个 dodge_roll 该有多大高度变化，猜一个数只会产出无意义的红叉。"""
    limits = thresholds_for("dodge_roll", "down")
    assert limits["height_variation_max"] is None
    assert limits["silhouette_variation_max"] is None
    assert limits["anchor_drift_max_px"] is None
    # 调色板越界与动作无关，仍然要查
    assert limits["palette_overflow_max"] is not None


def test_the_two_kinds_of_skip_are_distinguished() -> None:
    """``death`` 是**刻意豁免**（倒地的几何检查本就无意义）；
    自定义动作是**根本没有阈值**。混成一个理由，用户会以为后者也不必查。
    """
    from pixel_asset_forge.validation.engine import _skip_reason

    assert _skip_reason("death") == "action_exempt"
    assert _skip_reason("impact") == "action_exempt"
    assert _skip_reason("dodge_roll") == "custom_action_unthresholded"


def test_pose_sequence_still_refuses_a_bare_unknown_action() -> None:
    """没有内置模板、也没给节拍时，仍然报错而不是兜底。"""
    with pytest.raises(PlanError, match="也没有给节拍"):
        pose_sequence("moonwalk", 8, "down")
