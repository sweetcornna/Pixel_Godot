"""Asset Request 解析。

重点是 Sprint 1 的退出门槛：**Schema 错误能准确指出字段路径**。
只报"校验失败"的解析器等于把调试成本全推给用户。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pixel_asset_forge.constants import DEFAULT_TARGET_SIZE, DIRECTIONS, LOGICAL_SIZES
from pixel_asset_forge.errors import RequestValidationError, SchemaVersionError
from pixel_asset_forge.models import load_request, parse_request

#: 每个示例的目标尺寸按**它自己的细节密度**定，不统一锁在默认常量上。
#:
#: 32 → 48 是早先的实测结论（角色原生约 80 逻辑像素高，压到 32 丢六成细节）。
#: knight 再往上到 96 是 2026-08-02 的真实生成实测：带剑人形是本项目细节最密的
#: 一类，48px 下脸糊成一团、剑只剩一条线、连迈步动作都看不出来；96px 下五官、
#: 剑柄剑刃、跨步全都清晰（见 docs/static-family-review.md §7.2）。
EXAMPLE_TARGET_SIZES = {
    "knight": (96, 96),
    "slime": DEFAULT_TARGET_SIZE,
    "fireball": DEFAULT_TARGET_SIZE,
}


@pytest.mark.parametrize("name", ["knight", "slime", "fireball"])
def test_examples_parse(examples_dir: Path, name: str) -> None:
    request = load_request(examples_dir / f"{name}.yaml")
    assert request.asset_id == f"{name}_01"
    assert request.style.target_size == EXAMPLE_TARGET_SIZES[name]
    width, height = request.style.target_size
    assert width in LOGICAL_SIZES and height in LOGICAL_SIZES, "必须是受支持的逻辑尺寸档位"


def test_knight_is_not_mirrorable(examples_dir: Path) -> None:
    # 持剑破坏左右对称 —— 镜像会把角色变成左撇子（ADR-006）。
    assert load_request(examples_dir / "knight.yaml").mirroring_enabled is False


def test_slime_is_mirrorable(examples_dir: Path) -> None:
    request = load_request(examples_dir / "slime.yaml")
    assert request.mirroring_enabled is True
    assert request.mirror_source == "left"


def test_strict_lighting_overrides_mirroring(minimal_request: dict) -> None:
    minimal_request["mirroring"] = {"enabled": True}
    minimal_request["style"]["strict_lighting"] = True
    request = parse_request(minimal_request)
    assert request.mirroring_enabled is False


def test_unknown_field_reports_exact_path(minimal_request: dict) -> None:
    minimal_request["style"]["max_color"] = 24  # 少了个 s
    with pytest.raises(RequestValidationError) as exc:
        parse_request(minimal_request)
    paths = [e["path"] for e in exc.value.errors]
    assert "style" in paths


def test_illegal_frame_count_reports_path(minimal_request: dict) -> None:
    minimal_request["animations"][0]["frames"] = 7
    with pytest.raises(RequestValidationError) as exc:
        parse_request(minimal_request)
    assert any(e["path"] == "animations.0.frames" for e in exc.value.errors)


def test_illegal_logical_size_reports_path(minimal_request: dict) -> None:
    minimal_request["style"]["target_size"] = [33, 32]
    with pytest.raises(RequestValidationError) as exc:
        parse_request(minimal_request)
    assert any(e["path"].startswith("style.target_size") for e in exc.value.errors)


def test_character_requires_animations(minimal_request: dict) -> None:
    del minimal_request["animations"]
    with pytest.raises(RequestValidationError):
        parse_request(minimal_request)


def test_future_major_schema_is_rejected(minimal_request: dict) -> None:
    minimal_request["schema_version"] = "2.0"
    with pytest.raises(SchemaVersionError):
        parse_request(minimal_request)


def test_future_minor_schema_is_accepted(minimal_request: dict) -> None:
    # 更高 MINOR 只意味着新增了可选字段，向前兼容必须成立（PLAN §5.4）。
    minimal_request["schema_version"] = "1.9"
    assert parse_request(minimal_request).schema_version == "1.9"


def test_character_directions_default_to_all_four(minimal_request: dict) -> None:
    del minimal_request["animations"][0]["directions"]
    request = parse_request(minimal_request)
    assert request.animations[0].resolved_directions("character") == DIRECTIONS


def test_impact_has_no_directions(examples_dir: Path) -> None:
    # 爆炸是各向同性的，不该被强行摊成四个方向。
    request = load_request(examples_dir / "fireball.yaml")
    impact = next(a for a in request.animation_list() if a.name == "impact")
    assert impact.resolved_directions(request.asset_type) == ()


def test_missing_file_is_a_request_error(tmp_path: Path) -> None:
    with pytest.raises(RequestValidationError):
        load_request(tmp_path / "nope.yaml")


def test_malformed_yaml_is_a_request_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("asset_id: [unclosed\n", encoding="utf-8")
    with pytest.raises(RequestValidationError):
        load_request(path)


# -- 命名风格档位（对标 Kenney CC0 实测分布）-----------------------------


def test_style_preset_fills_in_the_missing_fields() -> None:
    """档位把一组配套的 style 字段固化成一行 —— 此前只能逐个猜。"""
    from pixel_asset_forge.models.request import StyleSpec

    style = StyleSpec(perspective="side_view", style_preset="chunky_icon")

    assert style.target_size == (24, 24)
    assert style.max_colors == 6
    assert style.shading == "flat"
    assert style.outline == "single_pixel_dark"


def test_explicit_values_always_beat_the_preset() -> None:
    """档位**只填空缺**。反过来会让"我明明写了却没生效"变成查不出来的问题。"""
    from pixel_asset_forge.models.request import StyleSpec

    style = StyleSpec(
        perspective="side_view", style_preset="chunky_icon",
        max_colors=16, shading="three_tone",
    )

    assert style.max_colors == 16
    assert style.shading == "three_tone"
    assert style.target_size == (24, 24)  # 没写的那项仍由档位补


def test_a_request_without_a_preset_keeps_the_old_behaviour() -> None:
    """不给档位时一个字节都不该变。"""
    from pixel_asset_forge.models.request import StyleSpec

    style = StyleSpec(perspective="side_view", target_size=(64, 64), max_colors=32)

    assert style.target_size == (64, 64)
    assert style.max_colors == 32
    assert style.shading == "two_tone"
    assert style.style_preset is None


def test_an_unknown_preset_is_refused_with_the_valid_names() -> None:
    """静默忽略会让"我换了档位却没反应"查不出来。"""
    from pydantic import ValidationError

    from pixel_asset_forge.models.request import StyleSpec

    with pytest.raises(ValidationError) as exc:
        StyleSpec(perspective="side_view", style_preset="not_a_preset")
    assert "chunky_icon" in str(exc.value)


def test_the_schema_requires_size_and_colors_only_without_a_preset() -> None:
    """schema 跑在 pydantic 之前，它也必须认这条 —— 否则档位在对外契约那层就被拒了。"""
    from pixel_asset_forge.errors import RequestValidationError
    from pixel_asset_forge.schema_registry import validate_against

    base = {
        "schema_version": "1.0", "asset_id": "t", "asset_type": "character",
        "description": "a test subject long enough",
        "export": {"targets": ["generic-json"]},
        "animations": [{"name": "idle", "frames": 4, "fps": 8, "loop": True}],
    }
    # 有档位：两个字段可省
    validate_against(
        "asset-request",
        {**base, "style": {"perspective": "side_view", "style_preset": "chunky_icon"}},
        what="t",
    )
    # 无档位：仍然必填
    with pytest.raises(RequestValidationError):
        validate_against(
            "asset-request", {**base, "style": {"perspective": "side_view"}}, what="t"
        )
