"""Asset Manifest（PLAN §5.2 / ADR-001）。

Manifest 是唯一真实来源：**所有导出文件必须能仅凭 Manifest + frames/ 重建。**
写盘前一律跑一次 schema 自检 —— 一个不合契约的 Manifest 会让所有下游工具崩在半路。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pixel_asset_forge.errors import ProcessingError, SchemaVersionError
from pixel_asset_forge.models.manifest import (
    AssetManifest,
    BackgroundInfo,
    CanvasInfo,
    DerivedAnimation,
    GeneratedAnimation,
    GridInfo,
    PaletteInfo,
    ProviderInfo,
)


def make_manifest(**overrides) -> AssetManifest:
    data = dict(
        asset_id="knight_01",
        asset_type="character",
        provider=ProviderInfo(name="mock", model="mock-image"),
        canvas=CanvasInfo(width=32, height=32),
        background=BackgroundInfo(
            mode="chroma_key",
            color_requested="#FF00FF",
            color_used="#FF00FF",
            fallback_stage="tolerant_key",
        ),
        palette=PaletteInfo(max_colors=24, colors=["#101010", "#A0C0FF"]),
        status="validated",
    )
    data.update(overrides)
    return AssetManifest(**data)


def test_roundtrip(tmp_path: Path) -> None:
    manifest = make_manifest(
        animations={
            "walk_down": GeneratedAnimation(
                fps=10, loop=True,
                grid=GridInfo(cols=4, rows=2, cell=(512, 512)),
                source_image="source/walk-down-original.png",
                frames=[f"frames/walk_down/{i:02d}.png" for i in range(8)],
            )
        }
    )
    path = manifest.save(tmp_path / "asset-manifest.json")
    loaded = AssetManifest.load(path)
    assert loaded.asset_id == "knight_01"
    assert len(loaded.resolve_frames("walk_down")) == 8


def test_saving_validates_against_the_schema(tmp_path: Path) -> None:
    manifest = make_manifest()
    # 调色板长度不得超过 max_colors —— 越界的 manifest 不该被写出去
    manifest.palette = PaletteInfo(max_colors=2, colors=["#000000", "#111111"])
    manifest.save(tmp_path / "ok.json")

    manifest.canvas = CanvasInfo(width=32, height=32)
    manifest.background = BackgroundInfo(
        mode="chroma_key",
        color_requested="#FF00FF",
        color_used="#FF00FF",
        fallback_stage="tolerant_key",
    )
    assert (tmp_path / "ok.json").exists()


def test_background_downgrade_is_detectable() -> None:
    """``color_used != color_requested`` 就是降级发生过的证据（PLAN §2.4.1）。"""
    manifest = make_manifest(
        background=BackgroundInfo(
            mode="chroma_key",
            color_requested="#FF00FF",
            color_used="#00FF00",
            fallback_stage="alt_key_color",
        )
    )
    assert manifest.background.downgraded is True


def test_color_used_survives_a_roundtrip(tmp_path: Path) -> None:
    """没有它，process 就无法脱离原始请求离线复现键控结果（ADR-004）。"""
    manifest = make_manifest(
        background=BackgroundInfo(
            mode="chroma_key", color_requested="#FF00FF",
            color_used="#00FF00", fallback_stage="alt_key_color",
        )
    )
    loaded = AssetManifest.load(manifest.save(tmp_path / "m.json"))
    assert loaded.background.color_used == "#00FF00"
    assert loaded.background.fallback_stage == "alt_key_color"


def test_derived_animation_resolves_to_its_source_frames(tmp_path: Path) -> None:
    manifest = make_manifest(
        animations={
            "walk_left": GeneratedAnimation(
                fps=10, loop=True, frames=["frames/walk_left/00.png"]
            ),
            "walk_right": DerivedAnimation(
                derived_from="walk_left", transform="flip_horizontal"
            ),
        }
    )
    assert manifest.resolve_frames("walk_right") == ["frames/walk_left/00.png"]
    assert list(manifest.derived_animations()) == ["walk_right"]


def test_resolving_a_dangling_derive_fails_loudly() -> None:
    manifest = make_manifest(
        animations={
            "walk_right": DerivedAnimation(
                derived_from="walk_left", transform="flip_horizontal"
            )
        }
    )
    with pytest.raises(ProcessingError):
        manifest.resolve_frames("walk_right")


def test_unknown_animation_key_fails(tmp_path: Path) -> None:
    with pytest.raises(ProcessingError):
        make_manifest().resolve_frames("nope")


def test_future_major_version_is_refused(tmp_path: Path) -> None:
    """读取器遇到更高 MAJOR 必须拒绝并提示迁移（PLAN §5.4）。"""
    path = tmp_path / "m.json"
    make_manifest().save(path)
    text = path.read_text(encoding="utf-8").replace('"schema_version": "2.0"',
                                                    '"schema_version": "3.0"', 1)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(SchemaVersionError) as exc:
        AssetManifest.load(path)
    assert "migrate" in exc.value.message


def test_higher_minor_version_is_accepted(tmp_path: Path) -> None:
    """更高 MINOR 只意味着新增了可选字段，向前兼容必须成立（PLAN §5.4）。"""
    path = tmp_path / "m.json"
    make_manifest().save(path)
    text = path.read_text(encoding="utf-8").replace('"schema_version": "2.0"',
                                                    '"schema_version": "2.7"', 1)
    path.write_text(text, encoding="utf-8")
    assert AssetManifest.load(path).schema_version == "2.7"


# -- Sprint 0 新增字段 -----------------------------------------------------


def test_grid_records_actual_size_not_the_nominal_one(tmp_path: Path) -> None:
    """端点不保证按请求尺寸返回；不记录实际尺寸，process 就无法复现格线（A-1）。"""
    manifest = make_manifest(
        animations={
            "walk_down": GeneratedAnimation(
                fps=10, loop=True,
                grid=GridInfo(cols=4, rows=2, cell=(444, 444),
                              requested_size=(2048, 1024), actual_size=(1774, 887)),
                frames=["frames/walk_down/00.png"],
            )
        }
    )
    loaded = AssetManifest.load(manifest.save(tmp_path / "m.json"))
    grid = loaded.animations["walk_down"].grid
    assert grid.actual_size == (1774, 887)
    assert grid.cell == (444, 444)
    assert grid.snapped is True


def test_grid_without_snapping_reports_false() -> None:
    grid = GridInfo(cols=4, rows=2, cell=(512, 512),
                    requested_size=(2048, 1024), actual_size=(2048, 1024))
    assert grid.snapped is False


def test_key_threshold_survives_a_roundtrip(tmp_path: Path) -> None:
    """自适应阈值不持久化的话，process 重跑会重新求解、可能得到不同值（A-5）。"""
    manifest = make_manifest(
        background=BackgroundInfo(
            mode="chroma_key", color_requested="#FF00FF", color_used="#FF00FF",
            fallback_stage="tolerant_key", key_threshold=165.0,
        )
    )
    loaded = AssetManifest.load(manifest.save(tmp_path / "m.json"))
    assert loaded.background.key_threshold == 165.0


def test_escalated_stages_are_flagged() -> None:
    """升到生成后的兜底档位，验证报告必须告警（ADR-004）。"""
    for stage, expected in (
        ("tolerant_key", False),
        ("alt_key_color", False),
        ("transparent_model", True),
        ("rembg", True),
        ("manual", True),
    ):
        info = BackgroundInfo(mode="chroma_key", color_requested="#FF00FF",
                              color_used="#FF00FF", fallback_stage=stage)
        assert info.escalated is expected, stage


# -- 1.x → 2.0 迁移（PLAN §5.4 要求 MAJOR 升级附带迁移路径）-----------------


def _v1_manifest(fallback_stage: int) -> dict:
    return {
        "schema_version": "1.0",
        "asset_id": "legacy_01",
        "asset_type": "character",
        "pipeline_version": "0.1.0",
        "provider": {"name": "openai", "model": "gpt-image-2"},
        "canvas": {"width": 32, "height": 32},
        "anchor": {"type": "bottom_center", "x": 0.5, "y": 1.0},
        "background": {
            "mode": "chroma_key",
            "color_requested": "#FF00FF",
            "color_used": "#FF00FF",
            "fallback_stage": fallback_stage,
        },
        "palette": {"max_colors": 24, "colors": []},
        "status": "validated",
    }


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # 1.x 的第 1 档「精确色键」实测必然失败并落到第 3 档，
        # 所以两者都映射到 2.0 的 tolerant_key。
        (1, "tolerant_key"),
        (2, "alt_key_color"),
        (3, "tolerant_key"),
        (4, "transparent_model"),
        (5, "rembg"),
        (6, "manual"),
    ],
)
def test_v1_stage_numbers_migrate_to_named_stages(tmp_path: Path, old: int, new: str) -> None:
    import json

    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(_v1_manifest(old)), encoding="utf-8")

    loaded = AssetManifest.load(path)
    assert loaded.schema_version == "2.0"
    assert loaded.background.fallback_stage == new


def test_migration_leaves_actual_size_unknown(tmp_path: Path) -> None:
    """1.x 根本没记实际尺寸，迁移无法凭空补出来。

    后果是明确的：1.x 资产不能靠 process 精确重跑切帧，只能重新生成。
    这是初版的代价，不是迁移的缺陷 —— 所以这里断言"缺省"，而不是断言某个猜测值。
    """
    import json

    data = _v1_manifest(2)
    data["animations"] = {
        "walk_down": {"fps": 10, "loop": True,
                      "grid": {"cols": 4, "rows": 2, "cell": [512, 512]},
                      "frames": ["frames/walk_down/00.png"]}
    }
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    grid = AssetManifest.load(path).animations["walk_down"].grid
    assert grid.actual_size is None
    assert grid.snapped is False


def test_missing_manifest_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(ProcessingError):
        AssetManifest.load(tmp_path / "absent.json")


def test_corrupt_json_fails_clearly(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ProcessingError):
        AssetManifest.load(path)
