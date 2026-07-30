"""Validation Report 与 per-action 阈值（PLAN §9）。

两条铁律：
- **验证失败时绝不标记为成功。** ``passed`` 由 checks 推导，不接受外部赋值。
- **阈值必须按动作区分。** 统一阈值对 ``attack`` / ``death`` 必然误报，
  而一个天天误报的验证器最终一定会被开发者关掉，等于白做。
"""

from __future__ import annotations

from pathlib import Path

from pixel_asset_forge.constants import DIRECTION_MULTIPLIER
from pixel_asset_forge.models.validation import (
    Check,
    CheckResult,
    Severity,
    ValidationReport,
    thresholds_for,
)


def report(*checks: Check) -> ValidationReport:
    return ValidationReport(asset_id="knight_01", checks=list(checks))


def test_empty_report_passes() -> None:
    assert report().passed is True


def test_fatal_failure_blocks() -> None:
    r = report(Check.make("frame_count", "walk_down", CheckResult.FAIL,
                          measured=6, threshold=8))
    assert r.passed is False
    assert r.blocking_checks[0].id == "frame_count"


def test_high_failure_blocks() -> None:
    r = report(Check.make("anchor_drift", "walk_down", CheckResult.FAIL))
    assert r.passed is False


def test_frame_order_can_never_block() -> None:
    """实测不可判定，因此降为 low 并永不阻断（见 validation/frame_order.py）。

    一个会对**正确**产出误报的验证项，比没有这个验证项更糟 ——
    它最终一定会被开发者关掉，顺带把真有用的那些一起关掉。
    """
    r = report(Check.make("frame_order_continuity", "walk_down", CheckResult.FAIL))
    assert r.passed is True
    assert r.checks[0].severity is Severity.LOW


def test_medium_failure_does_not_block() -> None:
    """质量瑕疵交给用户判断，不该拦住交付。"""
    r = report(Check.make("palette_overflow", "walk_down", CheckResult.FAIL,
                          measured=0.05, threshold=0.02))
    assert r.passed is True


def test_warnings_and_skips_do_not_block() -> None:
    r = report(
        Check.make("duplicate_frame_approx", "walk_down", CheckResult.WARN),
        Check.make("height_variation", "death_down", CheckResult.SKIP,
                   skip_reason="action_exempt"),
    )
    assert r.passed is True


def test_severity_comes_from_the_check_id_not_the_caller() -> None:
    """严重度是检查项的属性 —— 不能由调用方随手降级来"让报告变绿"。"""
    assert Check.make("cell_overflow", "walk_down", CheckResult.FAIL).severity is Severity.FATAL
    assert Check.make("anchor_drift", "walk_down", CheckResult.FAIL).severity is Severity.HIGH


def test_summary_counts_every_bucket() -> None:
    r = report(
        Check.make("frame_count", "a", CheckResult.PASS),
        Check.make("frame_size", "b", CheckResult.FAIL),
        Check.make("duplicate_frame_approx", "c", CheckResult.WARN),
        Check.make("height_variation", "d", CheckResult.SKIP, skip_reason="action_exempt"),
    )
    assert r.summary() == {"total": 4, "passed": 1, "failed": 1, "warnings": 1, "skipped": 1}


def test_report_is_schema_valid_when_saved(tmp_path: Path) -> None:
    r = report(
        Check.make("frame_count", "walk_down", CheckResult.FAIL,
                   action="walk", direction="down", measured=6, threshold=8,
                   message="模型只画出 6 帧")
    )
    r.thresholds_used = {"walk": thresholds_for("walk", "down")}
    path = r.save(tmp_path / "validation-report.json")
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["passed"] is False
    assert payload["summary"]["failed"] == 1


def test_thresholds_are_not_yet_calibrated() -> None:
    """Sprint 4 前阈值没有真实数据支撑，报告必须诚实地说明这一点。"""
    assert report().thresholds_calibrated is False


# -- per-action 阈值 -------------------------------------------------------


def test_idle_is_stricter_than_walk() -> None:
    assert (
        thresholds_for("idle")["height_variation_max"]
        < thresholds_for("walk")["height_variation_max"]
    )


def test_attack_is_looser_than_walk() -> None:
    """挥剑前冲导致大幅形变；套 walk 的阈值必然误报。"""
    assert (
        thresholds_for("attack")["height_variation_max"]
        > thresholds_for("walk")["height_variation_max"]
    )
    assert thresholds_for("attack")["anchor_drift_max_px"] == 3


def test_death_disables_geometry_checks() -> None:
    """倒地是形变的极端情况，几何检查无意义，只做人工审核（PLAN §9.1）。"""
    t = thresholds_for("death")
    assert t["height_variation_max"] is None
    assert t["silhouette_variation_max"] is None
    assert t["anchor_drift_max_px"] is None


def test_up_direction_loosens_silhouette_thresholds() -> None:
    """背面缺少正面细节，轮廓天然更不稳定（假设 A-7）。"""
    down = thresholds_for("walk", "down")
    up = thresholds_for("walk", "up")
    assert up["silhouette_variation_max"] == round(
        down["silhouette_variation_max"] * DIRECTION_MULTIPLIER["up"], 4
    )


def test_anchor_drift_ignores_the_direction_multiplier() -> None:
    """背面再不稳定，脚也该踩在同一条基线上 —— 锚点是绝对像素量。"""
    assert (
        thresholds_for("walk", "up")["anchor_drift_max_px"]
        == thresholds_for("walk", "down")["anchor_drift_max_px"]
    )


def test_unknown_action_disables_geometry_rather_than_guessing() -> None:
    t = thresholds_for("moonwalk")
    assert t["height_variation_max"] is None


# -- 修复建议 -------------------------------------------------------------


def test_local_failures_are_separated_from_regeneration() -> None:
    """先判断是不是本地能修的 —— 能离线解决的就不要花钱重生成。"""
    r = report(
        Check.make("transparent_rgb_residue", "walk_down", CheckResult.FAIL),
        Check.make("palette_overflow", "walk_down", CheckResult.FAIL),
        Check.make("cell_overflow", "attack_down", CheckResult.FAIL),
    )
    hint = r.repair_hint()
    assert hint["local"] == ["walk_down"]
    assert hint["regenerate"] == ["attack_down"]


def test_passing_checks_produce_no_repair_work() -> None:
    r = report(Check.make("frame_count", "walk_down", CheckResult.PASS))
    assert r.repair_hint() == {"local": [], "regenerate": []}
