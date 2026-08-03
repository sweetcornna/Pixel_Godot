"""校准 harness 的完整离线链路。"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from statistics import fmean

import pytest

CALIBRATION_PATH = (
    Path(__file__).parents[2] / "tools" / "calibration" / "run_calibration.py"
)
SPEC = importlib.util.spec_from_file_location("test_calibration_module", CALIBRATION_PATH)
assert SPEC is not None and SPEC.loader is not None
calibration = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = calibration
SPEC.loader.exec_module(calibration)


@pytest.fixture
def small_matrix() -> calibration.CalibrationMatrix:
    return calibration.CalibrationMatrix(
        assets=(
            calibration.AssetSample(
                asset_id="test_cal_knight",
                label="测试骑士",
                asset_type="character",
                description=(
                    "Slim test knight with a short sword, leather armor and a narrow human "
                    "silhouette. No shield and no cape."
                ),
                actions=(calibration.ActionSample("cast", "down"),),
                target_size=(48, 48),
                max_colors=16,
            ),
            calibration.AssetSample(
                asset_id="test_cal_fireball",
                label="测试火球",
                asset_type="projectile",
                description=(
                    "Compact orange fireball with a bright core and an expanding impact flash. "
                    "No character, ground or shadow."
                ),
                actions=(calibration.ActionSample("impact", None),),
                target_size=(48, 48),
                max_colors=16,
                outline="none",
                shading="three_tone",
                lighting="none",
            ),
        )
    )


def _mock_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PIXEL_ASSET_PROVIDER"] = "mock"
    env["PIXEL_ASSET_MODEL"] = "mock-image"
    env.pop("PIXEL_ASSET_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)
    return env


def test_budget_rejects_before_run_and_a_larger_cap_never_expands_matrix(
    tmp_path: Path, small_matrix: calibration.CalibrationMatrix
) -> None:
    run_dir = tmp_path / "rejected"

    with pytest.raises(calibration.BudgetExceededError, match="规划调用 4"):
        calibration.execute_calibration(
            run_dir,
            matrix=small_matrix,
            max_calls=3,
            env=_mock_env(),
        )

    assert not run_dir.exists()
    calibration.enforce_budget(40, small_matrix)
    assert small_matrix.planned_calls == 4
    assert small_matrix.sample_count == 2
    assert sum(row["calls"] for row in calibration.budget_rows(small_matrix)) == 4

    with pytest.raises(calibration.BudgetReconciliationError, match="规划 4 次，实际完成 3 次"):
        calibration.reconcile_budget(4, 3)


def test_mock_pipeline_writes_recomputable_metrics_and_preserves_constants(
    tmp_path: Path, small_matrix: calibration.CalibrationMatrix
) -> None:
    constants_before = calibration.CONSTANTS_PATH.read_bytes()
    reports_dir = tmp_path / "reports"
    run = calibration.execute_calibration(
        tmp_path / "run",
        matrix=small_matrix,
        max_calls=10,
        env=_mock_env(),
        timestamp="2026-08-03T00:00:00.000000Z",
        emit=lambda _message: None,
        reports_dir=reports_dir,
    )

    # 量化五件镜像到 reports/（入库目录），与 runs/ 里的原件逐字节一致
    for artifact in (
        run.raw_metrics_path,
        run.aggregates_path,
        run.report_path,
        run.previews_path,
        run.manifest_path,
    ):
        mirrored = reports_dir / artifact.name
        assert mirrored.is_file()
        assert mirrored.read_bytes() == artifact.read_bytes()

    assert run.provider == "mock"
    assert run.planned_calls == run.completed_calls == 4
    assert calibration.CONSTANTS_PATH.read_bytes() == constants_before

    raw = json.loads(run.raw_metrics_path.read_text(encoding="utf-8"))
    assert len(raw) == 2
    assert {item["key"] for item in raw} == {"cast_down", "impact"}
    assert all(
        set(item)
        == {
            "asset",
            "key",
            "anchor_drift",
            "height_variation",
            "silhouette_variation",
        }
        for item in raw
    )
    assert all(
        isinstance(item[name], (int, float))
        for item in raw
        for name in calibration.METRIC_NAMES
    )

    aggregates = json.loads(run.aggregates_path.read_text(encoding="utf-8"))
    assert aggregates["schema_version"] == "1.0"
    assert aggregates["sample_count"] == len(raw)
    assert set(aggregates["by_action"]) == {"cast", "impact"}
    assert set(aggregates["by_action_direction"]) == {"cast_down", "impact"}

    for action, key in (("cast", "cast_down"), ("impact", "impact")):
        matching = [item for item in raw if item["key"] == key]
        group = aggregates["by_action"][action]
        assert group["sample_count"] == len(matching)
        for metric in calibration.METRIC_NAMES:
            values = [float(item[metric]) for item in matching]
            assert group[metric]["min"] == pytest.approx(min(values))
            assert group[metric]["max"] == pytest.approx(max(values))
            assert group[metric]["mean"] == pytest.approx(fmean(values))

    previews = json.loads(run.previews_path.read_text(encoding="utf-8"))
    assert len(previews) == 2
    for asset in previews:
        assert (run.run_dir / asset["contact_sheet"]).is_file()
        assert len(asset["gifs"]) == 1
        assert all((run.run_dir / path).is_file() for path in asset["gifs"])

    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    assert manifest["cache_enabled"] is False
    assert manifest["budget"] == {
        "max_calls": 10,
        "planned_calls": 4,
        "completed_calls": 4,
        "reconciled": True,
    }
    assert manifest["constants"]["unchanged"] is True
    assert manifest["constants"]["sha256_before"] == manifest["constants"]["sha256_after"]
    assert "未修改 `constants.py`" in run.report_path.read_text(encoding="utf-8")
