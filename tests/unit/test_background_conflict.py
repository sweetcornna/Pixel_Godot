"""背景键控色冲突预检（PLAN §2.4.1 / ADR-004）。

这是 v1 规划稿的致命缺陷所在：硬编码 ``#FF00FF`` 会把粉紫色角色本体一起抠掉。
Slime 用例就是专门用来暴露它的，所以这里必须锁死期望行为。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pixel_asset_forge.errors import PlanError
from pixel_asset_forge.models import load_request
from pixel_asset_forge.processing import resolve_key_color


def test_no_conflict_keeps_requested_color() -> None:
    decision = resolve_key_color("Young forest knight, green cloak, leather armor")
    assert decision.color_used == "#FF00FF"
    assert decision.fallback_stage == "tolerant_key"
    assert decision.downgraded is False


def test_magenta_character_downgrades_to_green(examples_dir: Path) -> None:
    request = load_request(examples_dir / "slime.yaml")
    decision = resolve_key_color(
        request.description,
        requested=request.background.color,
        fallbacks=request.background.fallback_colors,
        conflict_hint=request.background.conflict_hint,
    )
    # slime.yaml 头注释里写明的期望行为。
    assert decision.color_used == "#00FF00"
    assert decision.fallback_stage == "alt_key_color"
    assert "magenta" in decision.conflicts


def test_slime_does_not_falsely_match_lime() -> None:
    """``slime`` 含子串 ``lime`` —— 无词边界匹配会把它误判为与纯绿冲突。

    后果很具体：史莱姆本该降级到的正是纯绿，误判会让它一路降到青色。
    """
    decision = resolve_key_color("Small round slime creature, dark eyes")
    assert decision.color_used == "#FF00FF"
    assert decision.fallback_stage == "tolerant_key"


def test_negation_is_not_a_conflict_source() -> None:
    # "translucent" 里含 "lucent"，但不含任何颜色词；确认不会误伤。
    decision = resolve_key_color("Translucent crystal shard, pale blue core")
    assert decision.color_used == "#FF00FF"


def test_palette_exact_match_beats_keywords() -> None:
    """描述里一个颜色词都没有，但调色板里就有键控色本身 —— 必须换色。"""
    decision = resolve_key_color(
        "A neon signboard prop", palette=("#FF00FF", "#101010")
    )
    assert decision.color_used == "#00FF00"
    assert decision.fallback_stage == "alt_key_color"


def test_all_candidates_conflicting_raises_actionable_error() -> None:
    with pytest.raises(PlanError) as exc:
        resolve_key_color("A magenta, green and cyan rainbow slime with violet trim")
    assert "fallback_colors" in exc.value.message


def test_conflict_hint_participates_in_detection() -> None:
    decision = resolve_key_color(
        "A creature",
        conflict_hint="body is magenta/violet, overlaps default keying color",
    )
    assert decision.downgraded is True


def test_explain_mentions_both_colors() -> None:
    decision = resolve_key_color("A magenta wizard robe")
    assert "#FF00FF" in decision.explain()
    assert decision.color_used in decision.explain()
