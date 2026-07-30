"""验证引擎与 Repair Planner（Sprint 5）。

围绕两条不可退让的规则：

- **验证失败时绝不标记为成功。**
- **本地可修的问题不重新调用 API。**
"""

from __future__ import annotations

import numpy as np
import pytest

from pixel_asset_forge.models.validation import (
    Check,
    CheckResult,
    Severity,
    ValidationReport,
)
from pixel_asset_forge.repair import RepairAction, plan_repairs
from pixel_asset_forge.validation import (
    anchor_measurement,
    exact_duplicates,
    height_variation,
    is_blank,
    measure_frame_order,
    silhouette_variation,
    transparent_rgb_residue,
)


def frame(size: int = 32, *, box: tuple[int, int, int, int] | None = None) -> np.ndarray:
    arr = np.zeros((size, size, 4), dtype=np.uint8)
    x0, y0, x1, y1 = box or (8, 8, 24, 32)
    arr[y0:y1, x0:x1, :3] = (120, 90, 40)
    arr[y0:y1, x0:x1, 3] = 255
    return arr


# -- 测量 -----------------------------------------------------------------


def test_blank_detection() -> None:
    assert is_blank(np.zeros((8, 8, 4), dtype=np.uint8))
    assert not is_blank(frame())


def test_transparent_rgb_residue_counts_offenders() -> None:
    arr = frame()
    arr[0, 0, :3] = (9, 9, 9)  # 透明但 RGB 非零
    assert transparent_rgb_residue(arr) == 1
    assert transparent_rgb_residue(frame()) == 0


def test_height_and_silhouette_variation() -> None:
    frames = [frame(box=(8, 8, 24, 32)), frame(box=(8, 4, 24, 32))]
    assert height_variation(frames) > 0
    assert silhouette_variation(frames) > 0
    assert height_variation([frame(), frame()]) == 0.0


def test_exact_duplicates_report_index_pairs() -> None:
    a = frame()
    assert exact_duplicates([a, frame(box=(4, 4, 20, 32)), a.copy()]) == [(0, 2)]


def test_anchor_measurement_splits_the_two_directions() -> None:
    """脚底漂移让角色上下抖、水平漂移让角色左右滑 —— 成因不同，不能混成一个数。"""
    frames = [frame(box=(8, 8, 24, 32)), frame(box=(2, 8, 18, 28))]
    measurement = anchor_measurement(frames)
    assert measurement.baseline_spread_px == 4
    assert measurement.horizontal_spread_px == 6


def test_anchor_measurement_ignores_blank_frames() -> None:
    measurement = anchor_measurement([frame(), np.zeros((32, 32, 4), dtype=np.uint8)])
    assert measurement.baseline_spread_px == 0


# -- 帧序（实测不可判定）---------------------------------------------------


def test_frame_order_only_reports_statistics() -> None:
    """判据实测不可区分正序与乱序，因此只报统计量、不下结论。"""
    frames = [frame(box=(8 + i, 8, 24 + i, 32)) for i in range(6)]
    stats = measure_frame_order(frames, loop=True)
    assert stats is not None
    assert len(stats.differences) == 6
    assert stats.local_outlier > 0


def test_frame_order_needs_enough_frames() -> None:
    assert measure_frame_order([frame(), frame()], loop=False) is None


# -- 报告结论 --------------------------------------------------------------


def report(*checks: Check) -> ValidationReport:
    return ValidationReport(asset_id="knight_01", checks=list(checks))


def test_fatal_failure_blocks_delivery() -> None:
    r = report(Check.make("cell_overflow", "walk_down", CheckResult.FAIL))
    assert r.passed is False


def test_medium_failure_does_not_block() -> None:
    r = report(Check.make("height_variation", "walk_down", CheckResult.FAIL))
    assert r.passed is True


# -- Repair Planner --------------------------------------------------------


def test_local_repair_is_preferred_and_costs_nothing() -> None:
    """能离线解决的就离线解决 —— 重生成既慢又花钱，而且拿到的是另一张图。"""
    plan = plan_repairs(
        report(
            Check.make("transparent_rgb_residue", "walk_down", CheckResult.FAIL),
            Check.make("anchor_drift", "walk_down", CheckResult.FAIL),
        )
    )
    assert len(plan.steps) == 1
    assert plan.steps[0].action is RepairAction.REPROCESS
    assert plan.steps[0].calls_api is False
    assert plan.api_calls == 0


def test_overflow_requires_regeneration() -> None:
    plan = plan_repairs(
        report(
            Check.make(
                "cell_overflow", "attack_down", CheckResult.FAIL, message="2 个连通域跨格"
            )
        )
    )
    assert plan.steps[0].action is RepairAction.REGENERATE_GRID
    assert plan.api_calls == 1


def test_regeneration_supersedes_local_repair_on_the_same_target() -> None:
    """重生成会重跑整条处理链，本地修复是它的子集 —— 再单列一步是白做。"""
    plan = plan_repairs(
        report(
            Check.make("anchor_drift", "walk_down", CheckResult.FAIL),
            Check.make("cell_overflow", "walk_down", CheckResult.FAIL, message="跨格"),
        )
    )
    assert [s.action for s in plan.steps] == [RepairAction.REGENERATE_GRID]


def test_only_the_failing_unit_is_repaired() -> None:
    """只重生成最小失败单元 —— 一个动作坏了不该把整个资产推倒重来。"""
    plan = plan_repairs(
        report(
            Check.make("cell_overflow", "walk_down", CheckResult.FAIL, message="跨格"),
            Check.make("frame_count", "idle_up", CheckResult.FAIL),
            Check.make("frame_count", "walk_left", CheckResult.PASS),
        )
    )
    assert {s.target for s in plan.steps} == {"walk_down", "idle_up"}


def test_aspect_driven_overflow_is_flagged_as_futile() -> None:
    """实测：同一 prompt 连续三次返回同样的错误比例、三次都越界。

    这种情况下推荐"重生成"只会烧完修复预算然后判 failed —— 不如直说换网格形状。
    """
    plan = plan_repairs(
        report(
            Check.make(
                "cell_overflow", "walk_down", CheckResult.FAIL,
                message="2 个连通域跨格；长短边比偏差 25.0%（越界高风险：格子被压扁）",
            )
        )
    )
    assert plan.steps[0].action is RepairAction.MANUAL
    assert plan.api_calls == 0
    assert "重生成不会解决它" in plan.steps[0].detail


def test_uncalibrated_medium_failures_stay_out_of_the_plan() -> None:
    """对着未校准的阈值提修复建议就是对着噪声行动，只会毁掉修复计划的信噪比。"""
    plan = plan_repairs(report(Check.make("height_variation", "walk_down", CheckResult.FAIL)))
    assert plan.empty


def test_blocking_check_without_an_action_surfaces_as_manual() -> None:
    plan = plan_repairs(report(Check.make("frame_size", "walk_down", CheckResult.FAIL)))
    # frame_size 属于本地可修
    assert plan.steps[0].action is RepairAction.REPROCESS


def test_exhausted_budget_stops_planning() -> None:
    """反复自动重生成很可能是在掩盖一个系统性问题。"""
    plan = plan_repairs(
        report(Check.make("cell_overflow", "walk_down", CheckResult.FAIL, message="跨格")),
        rounds_used=2,
        max_rounds=2,
    )
    assert plan.exhausted
    assert plan.empty


def test_passing_report_needs_no_repair() -> None:
    assert plan_repairs(report(Check.make("frame_count", "walk_down", CheckResult.PASS))).empty


def test_plan_serialises_with_cost_visible() -> None:
    plan = plan_repairs(
        report(Check.make("cell_overflow", "walk_down", CheckResult.FAIL, message="跨格"))
    )
    payload = plan.to_dict()
    assert payload["estimated_api_calls"] == 1
    assert payload["steps"][0]["calls_api"] is True


@pytest.mark.parametrize(
    ("check_id", "expected"),
    [
        ("transparent_rgb_residue", RepairAction.REPROCESS),
        ("palette_overflow", RepairAction.REPROCESS),
        ("anchor_drift", RepairAction.REPROCESS),
        ("frame_size", RepairAction.REPROCESS),
        ("frame_count", RepairAction.REGENERATE_GRID),
        ("blank_frame", RepairAction.REGENERATE_GRID),
        ("static_animation", RepairAction.REGENERATE_GRID),
    ],
)
def test_symptom_to_action_table(check_id: str, expected: RepairAction) -> None:
    """PLAN §9.3 的症状 → 动作对照表。"""
    plan = plan_repairs(report(Check.make(check_id, "walk_down", CheckResult.FAIL)))
    assert plan.steps[0].action is expected


def test_severity_of_a_step_is_the_worst_of_its_reasons() -> None:
    plan = plan_repairs(
        report(
            Check.make("anchor_drift", "walk_down", CheckResult.FAIL),      # high
            Check.make("palette_overflow", "walk_down", CheckResult.FAIL),  # medium
        )
    )
    assert plan.steps[0].severity is Severity.HIGH
