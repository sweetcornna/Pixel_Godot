"""Live gate 的离线判定层；真实 provider 调用只允许人工执行。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from pixel_asset_forge.models.validation import (
    Check,
    CheckResult,
    ValidationReport,
)

LIVE_GATE_PATH = Path(__file__).parents[2] / "tools" / "live-gate" / "run_live_gate.py"
SPEC = importlib.util.spec_from_file_location("test_live_gate_module", LIVE_GATE_PATH)
assert SPEC is not None and SPEC.loader is not None
live_gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = live_gate
SPEC.loader.exec_module(live_gate)


def _assets(*passed: bool) -> list[live_gate.AssetAssessment]:
    return [
        live_gate.AssetAssessment(
            name=f"asset-{index}",
            validation_passed=value,
            validation_source="test",
        )
        for index, value in enumerate(passed)
    ]


def test_quantitative_threshold_is_inclusive_and_blocks_overflow() -> None:
    at_limit = live_gate.GateMetric("asset", "tile_seam", "tile", 3.0, 3.0)
    over_limit = live_gate.GateMetric("asset", "tile_border", "tile", 2.001, 2.0)

    assert at_limit.passed is True
    assert over_limit.passed is False


def test_classifies_pass() -> None:
    assert live_gate.classify_result(_assets(True, True, True)) is live_gate.GateState.PASS


def test_classifies_fail() -> None:
    assert live_gate.classify_result(_assets(True, False, True)) is live_gate.GateState.FAIL


def test_classifies_runtime_error_separately_from_quality_failure() -> None:
    state = live_gate.classify_result(_assets(True, False, True), error="endpoint unreachable")

    assert state is live_gate.GateState.ERROR
    assert state.exit_code == 2


def test_fatal_failure_counterexample_must_make_the_gate_fail() -> None:
    """反例已先用脚本探针确认会红，再固化：fatal 绝不能被 3/3 汇总吞掉。"""
    fatal = Check.make(
        "frame_count",
        "walk_down",
        CheckResult.FAIL,
        measured=7,
        threshold=8,
    )
    report = ValidationReport(asset_id="knight_01", checks=[fatal])
    data = live_gate.ExecutionData(
        assessments=[
            live_gate.AssetAssessment(
                "knight_01_walk_down",
                report.passed,
                "ValidationReport.passed",
                checks=(fatal,),
            ),
            *_assets(True, True),
        ],
        validation_reports=[report],
    )

    payload = live_gate._report_payload(
        timestamp="20260802T000000.000000Z",
        run_dir=live_gate.RUNS_DIR / "test",
        provider="mock",
        model="mock-image",
        data=data,
        error=None,
    )

    assert payload["status"] == "FAIL"
    assert payload["exit_code"] == 1
    assert payload["summary"]["fatal_high_failures"] == 1


def test_report_aggregation_keeps_medium_and_low_checks_and_metric_ids() -> None:
    medium = Check.make(
        "palette_overflow",
        "walk_down",
        CheckResult.FAIL,
        measured=0.03,
        threshold=0.02,
    )
    low = Check.make(
        "duplicate_frame_approx",
        "walk_down",
        CheckResult.WARN,
        measured=1,
        threshold=0,
    )
    report = ValidationReport(asset_id="knight_01", checks=[medium, low])
    metric = live_gate.GateMetric(
        "knight_01_walk_down", "anchor_drift", "walk_down", 0.5, 1
    )
    data = live_gate.ExecutionData(
        assessments=[
            live_gate.AssetAssessment(
                "knight_01_walk_down",
                report.passed,
                "ValidationReport.passed",
                metrics=(metric,),
                checks=(medium, low),
            ),
            *_assets(True, True),
        ],
        validation_reports=[report],
    )

    payload = live_gate._report_payload(
        timestamp="20260802T000000.000000Z",
        run_dir=live_gate.RUNS_DIR / "test",
        provider="mock",
        model="mock-image",
        data=data,
        error=None,
    )

    raw_results = {
        (check["id"], check["result"])
        for check in payload["validation_reports"][0]["checks"]
    }
    assert ("palette_overflow", "fail") in raw_results
    assert ("duplicate_frame_approx", "warn") in raw_results
    assert all("check_id" in item for item in payload["metrics"])
    assert payload["status"] == "PASS"


def test_redact_text_replaces_every_api_key_value() -> None:
    text = "first=sk-first second=sk-second"

    assert live_gate.redact_text(text, ["sk-first", "sk-second"]) == (
        "first=[REDACTED] second=[REDACTED]"
    )


def test_secret_self_check_counterexample_deletes_both_reports(tmp_path: Path) -> None:
    """反例已先用临时文件确认会触发删除，再固化，防止自检退化成空操作。"""
    fake_key = "sk-live-gate-counterexample"
    json_path = tmp_path / "report.json"
    md_path = tmp_path / "report.md"
    json_path.write_text(f'{{"leak": "{fake_key}"}}', encoding="utf-8")
    md_path.write_text("clean sibling", encoding="utf-8")

    with pytest.raises(live_gate.SecretLeakError, match="报告已删除"):
        live_gate.assert_no_secrets(
            [json_path, md_path],
            [fake_key],
            cleanup_paths=[json_path, md_path],
        )

    assert not json_path.exists()
    assert not md_path.exists()


def test_budget_overflow_refuses_execution_before_creating_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def must_not_run(*_args: object, **_kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(live_gate, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(live_gate, "execute_live_gate", must_not_run)

    assert live_gate.main(["--max-calls", "4"]) == live_gate.EXIT_ERROR
    assert called is False
    assert not (tmp_path / "runs").exists()
