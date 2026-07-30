"""Asset Request 解析。

重点是 Sprint 1 的退出门槛：**Schema 错误能准确指出字段路径**。
只报"校验失败"的解析器等于把调试成本全推给用户。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pixel_asset_forge.constants import DEFAULT_TARGET_SIZE, DIRECTIONS
from pixel_asset_forge.errors import RequestValidationError, SchemaVersionError
from pixel_asset_forge.models import load_request, parse_request


@pytest.mark.parametrize("name", ["knight", "slime", "fireball"])
def test_examples_parse(examples_dir: Path, name: str) -> None:
    request = load_request(examples_dir / f"{name}.yaml")
    assert request.asset_id == f"{name}_01"
    # 48 而非 32：实测 gpt-image-2 画出的角色原生约 80 逻辑像素高，
    # 压到 32 要丢六成细节（见 processing/pixel_grid.py）。
    assert request.style.target_size == DEFAULT_TARGET_SIZE


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
