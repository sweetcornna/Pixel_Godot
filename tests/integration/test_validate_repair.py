"""validate / repair 的端到端行为（Sprint 5 退出门槛）。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import yaml
from PIL import Image
from typer.testing import CliRunner

from pixel_asset_forge.cli import EXIT_OK, EXIT_VALIDATION_FAILED, app
from pixel_asset_forge.config import Config
from pixel_asset_forge.errors import ProcessingError
from pixel_asset_forge.models.job import JobStatus
from pixel_asset_forge.models.manifest import AssetManifest
from pixel_asset_forge.pipelines import approve_seed, create_animation, create_character
from pixel_asset_forge.pipelines.static_asset import (
    create_static_asset,
    validate_and_export_static_asset,
)
from pixel_asset_forge.pipelines.validation import run_validation
from pixel_asset_forge.repair import RepairAction, execute_plan, plan_repairs, rounds_used
from pixel_asset_forge.storage import ArtifactStore
from pixel_asset_forge.validation import validate_asset

runner = CliRunner()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        provider="mock",
        model="mock-image",
        output_dir=tmp_path / "outputs",
        cache_dir=tmp_path / "cache",
        max_repair_rounds=2,
    )


@pytest.fixture
def asset(config: Config, tmp_path: Path, examples_dir: Path) -> ArtifactStore:
    request = tmp_path / "knight.yaml"
    request.write_text((examples_dir / "knight.yaml").read_text(encoding="utf-8"),
                       encoding="utf-8")
    create_character(request, config)
    store = ArtifactStore.for_asset(config.output_dir, "knight_01")
    approve_seed(store.root)
    create_animation(store.root, action="walk", direction="down", config=config)
    return store


def _static_request(asset_id: str) -> dict[str, object]:
    return {
        "schema_version": "1.1",
        "asset_id": asset_id,
        "asset_type": "prop",
        "description": "A clearly readable isolated wooden crate for a fantasy game inventory.",
        "style": {
            "perspective": "top_down_3_4",
            "target_size": [32, 32],
            "max_colors": 12,
            "outline": "single_pixel_dark",
            "shading": "two_tone",
            "antialiasing": False,
            "lighting": "fixed_top_left",
        },
        "background": {
            "mode": "chroma_key",
            "color": "#FF00FF",
            "fallback_colors": ["#00FF00", "#00FFFF"],
        },
        "export": {"targets": ["generic-json"]},
    }


@pytest.fixture
def static_asset(config: Config, tmp_path: Path) -> ArtifactStore:
    """一个走完生成 → 处理 → 验证 → 导出的静态资产（无动作网格）。"""
    request_path = tmp_path / "wooden_crate.yaml"
    request_path.write_text(
        yaml.safe_dump(_static_request("wooden_crate"), sort_keys=False), encoding="utf-8"
    )
    create_static_asset(request_path, config)
    store = ArtifactStore.for_asset(config.output_dir, "wooden_crate")
    validate_and_export_static_asset(store.root, targets=["generic-json"])
    return store


def _job_status(store: ArtifactStore, key: str) -> JobStatus:
    table = store.load_job_table()
    assert table is not None
    return next(job for job in table if job.key == key).status


# -- 正常产出 --------------------------------------------------------------


def test_clean_asset_passes(asset: ArtifactStore) -> None:
    """判据是"没有阻断项"，不是"零失败项"。

    中低严重度的几何变化量是拿**未校准阈值**判的（PLAN §9.1），
    断言它们必须全绿，等于把"合成数据恰好落在某个未校准阈值之下"当成不变量 ——
    阈值一调、缩放取整方式一改就会假失败。
    """
    report = validate_asset(asset.root)
    assert report.passed is True
    assert not report.blocking_checks


def test_report_is_schema_valid(asset: ArtifactStore, tmp_path: Path) -> None:
    report = validate_asset(asset.root)
    path = report.save(tmp_path / "validation-report.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["passed"] is True
    assert payload["summary"]["total"] > 0


def test_report_states_the_calibration_status(asset: ArtifactStore) -> None:
    """校准状态必须写进报告 —— 用户据此决定中低严重度告警要不要当真。

    五个角色动作已用 6 角色 × 5 动作的真实样本校准
    （docs/threshold-calibration.md）；cast / travel / impact / loop 仍是初始值。
    """
    assert validate_asset(asset.root).thresholds_calibrated is True


def test_frame_order_is_reported_but_never_blocks(asset: ArtifactStore) -> None:
    report = validate_asset(asset.root)
    order = [c for c in report.checks if c.id == "frame_order_continuity"]
    assert order, "缺了帧序检查项 —— 这条防线是缺的，必须在报告里可见"
    assert all(c.result.value == "skip" for c in order)
    assert "无法自动判定" in (order[0].message or "")


# -- 注入缺陷 --------------------------------------------------------------


def _corrupt(store: ArtifactStore, key: str, mutate) -> None:  # type: ignore[no-untyped-def]
    for path in sorted(store.frames_of(key).glob("*.png")):
        arr = np.array(Image.open(path).convert("RGBA"))
        Image.fromarray(mutate(arr), "RGBA").save(path)


def test_transparent_rgb_residue_is_caught_and_locally_repairable(
    asset: ArtifactStore,
) -> None:
    def mutate(arr: np.ndarray) -> np.ndarray:
        arr = arr.copy()
        arr[0, 0, :3] = (7, 7, 7)  # 透明却带 RGB
        return arr

    _corrupt(asset, "walk_down", mutate)
    report = validate_asset(asset.root)
    assert report.passed is False

    plan = plan_repairs(report)
    assert plan.steps[0].action is RepairAction.REPROCESS
    assert plan.api_calls == 0, "本地可修的问题不该重新调用 API"


def test_blank_frame_is_fatal(asset: ArtifactStore) -> None:
    path = next(iter(sorted(asset.frames_of("walk_down").glob("*.png"))))
    Image.fromarray(np.zeros((32, 32, 4), dtype=np.uint8), "RGBA").save(path)

    report = validate_asset(asset.root)
    blank = next(c for c in report.checks if c.id == "blank_frame")
    assert blank.result.value == "fail"
    assert report.passed is False


def test_missing_frame_file_is_caught(asset: ArtifactStore) -> None:
    next(iter(sorted(asset.frames_of("walk_down").glob("*.png")))).unlink()
    report = validate_asset(asset.root)
    assert report.passed is False


def test_frame_count_mismatch_requires_regeneration(asset: ArtifactStore) -> None:
    manifest = AssetManifest.load(asset.manifest_path)
    entry = manifest.animations["walk_down"]
    entry.frames = entry.frames[:5]
    manifest.save(asset.manifest_path)

    report = validate_asset(asset.root)
    assert report.passed is False
    plan = plan_repairs(report)
    assert plan.steps[0].action is RepairAction.REGENERATE_GRID


# -- 修复执行 --------------------------------------------------------------


def test_local_repair_runs_without_api(asset: ArtifactStore, config: Config) -> None:
    def mutate(arr: np.ndarray) -> np.ndarray:
        arr = arr.copy()
        arr[0, 0, :3] = (7, 7, 7)
        return arr

    _corrupt(asset, "walk_down", mutate)
    plan = plan_repairs(validate_asset(asset.root))

    outcomes = execute_plan(asset.root, plan, config, allow_api=False)
    assert outcomes[0].performed is True
    assert validate_asset(asset.root).passed is True, "离线重跑应当修好它"


def test_regeneration_is_withheld_without_explicit_consent(
    asset: ArtifactStore, config: Config
) -> None:
    """花钱的动作必须由用户显式同意，不该因为跑了一次 repair 就悄悄发生。"""
    manifest = AssetManifest.load(asset.manifest_path)
    manifest.animations["walk_down"].frames = manifest.animations["walk_down"].frames[:5]
    manifest.save(asset.manifest_path)

    plan = plan_repairs(validate_asset(asset.root))
    outcomes = execute_plan(asset.root, plan, config, allow_api=False)
    assert outcomes[0].performed is False
    assert "--allow-api" in outcomes[0].detail


def test_every_repair_is_logged(asset: ArtifactStore, config: Config) -> None:
    """修复是"系统自己改自己的产物"，不留痕就分不清当前产物是第几轮的结果。"""
    _corrupt(asset, "walk_down", lambda a: np.where(a == a, a, a))  # 无害改写，触发计划
    manifest = AssetManifest.load(asset.manifest_path)
    manifest.animations["walk_down"].frames = manifest.animations["walk_down"].frames[:5]
    manifest.save(asset.manifest_path)

    plan = plan_repairs(validate_asset(asset.root))
    execute_plan(asset.root, plan, config, allow_api=False)

    log = json.loads((asset.root / "repair-log.json").read_text(encoding="utf-8"))
    assert log[0]["round"] == 1
    assert log[0]["outcomes"][0]["target"] == "walk_down"
    assert rounds_used(asset.root) == 1


def test_repair_budget_is_enforced(asset: ArtifactStore, config: Config) -> None:
    from pixel_asset_forge.errors import RepairLimitExceededError

    manifest = AssetManifest.load(asset.manifest_path)
    manifest.animations["walk_down"].frames = manifest.animations["walk_down"].frames[:5]
    manifest.save(asset.manifest_path)

    report = validate_asset(asset.root)
    for _ in range(2):
        execute_plan(asset.root, plan_repairs(report), config, allow_api=False)

    exhausted = plan_repairs(report, rounds_used=2, max_rounds=2)
    assert exhausted.exhausted
    with pytest.raises(RepairLimitExceededError):
        execute_plan(asset.root, exhausted, config, allow_api=False)


# -- 修复之后还得能验证 ------------------------------------------------------


def _inject_local_defect(store: ArtifactStore, key: str) -> None:
    """透明像素带 RGB —— 本地可修，触发 REPROCESS 那条边。"""
    for path in sorted(store.frames_of(key).glob("*.png")):
        arr = np.array(Image.open(path).convert("RGBA"))
        arr[0, 0, :3] = (7, 7, 7)
        Image.fromarray(arr, "RGBA").save(path)


def test_local_repair_leaves_the_job_where_validate_can_pick_it_up(
    asset: ArtifactStore, config: Config
) -> None:
    """修复的下一步是重新验证 —— 这条环必须真的闭合。

    ``run_process`` 只跑像素，不碰任务表；修复推进到 ``processing`` 之后没人把它
    送到 ``processed``，任务就卡在那里。而 ``validate`` 只收 processed /
    validating / validated / validation_failed —— 实测报"没有可验证任务，当前状态：
    approved, planned, processing"，正是 repair 自己打印的下一步做不到。
    """
    _inject_local_defect(asset, "walk_down")
    report = run_validation(asset.root)
    assert report.passed is False
    assert _job_status(asset, "walk_down") is JobStatus.VALIDATION_FAILED

    execute_plan(asset.root, plan_repairs(report), config, allow_api=False)
    assert _job_status(asset, "walk_down") is JobStatus.PROCESSED

    assert run_validation(asset.root).passed is True
    assert _job_status(asset, "walk_down") is JobStatus.VALIDATED


def test_local_repair_marks_the_job_failed_when_the_source_is_gone(
    asset: ArtifactStore, config: Config
) -> None:
    """原图没了就离线补不回来 —— 状态得如实说，不能停在 ``processing`` 装作在跑。"""
    _inject_local_defect(asset, "walk_down")
    plan = plan_repairs(run_validation(asset.root))
    asset.source_path("walk_down").unlink()

    outcomes = execute_plan(asset.root, plan, config, allow_api=False)
    assert outcomes[0].performed is False
    assert "无法离线重跑" in outcomes[0].detail
    assert _job_status(asset, "walk_down") is JobStatus.FAILED


# -- 静态资产不是动作网格 ----------------------------------------------------


def _blank_static_image(store: ArtifactStore) -> None:
    Image.fromarray(np.zeros((32, 32, 4), dtype=np.uint8), "RGBA").save(store.frames / "static.png")


def test_static_repair_withholds_regeneration_and_names_the_right_thing(
    static_asset: ArtifactStore, config: Config
) -> None:
    _blank_static_image(static_asset)
    plan = plan_repairs(run_validation(static_asset.root))
    assert [(s.target, s.action) for s in plan.steps] == [
        ("static", RepairAction.REGENERATE_GRID)
    ]

    outcomes = execute_plan(static_asset.root, plan, config, allow_api=False)
    assert outcomes[0].performed is False
    assert "静态原图" in outcomes[0].detail, "静态资产没有动作网格，别照着动画的措辞说"
    assert "--allow-api" in outcomes[0].detail


def test_static_repair_regenerates_through_the_static_pipeline(
    static_asset: ArtifactStore, config: Config
) -> None:
    """静态任务的 ID 恰好等于按动画拼出来的 ID，于是它一直被当动作网格修。

    实测旧行为：任务被推到 ``generating``（"有一次调用在飞"的意思），
    ``create_animation`` 随后抛 input_fingerprint 冲突，``repair-log.json``
    连写都没写到 —— 而"所有修复操作有日志"是 Sprint 5 的退出门槛。
    """
    _blank_static_image(static_asset)
    plan = plan_repairs(run_validation(static_asset.root))

    outcomes = execute_plan(static_asset.root, plan, config, allow_api=True)

    assert outcomes[0].performed is True
    assert _job_status(static_asset, "static") is JobStatus.PROCESSED
    log = json.loads((static_asset.root / "repair-log.json").read_text(encoding="utf-8"))
    assert log[0]["outcomes"][0]["target"] == "static"
    # 修完照样得能验证、能导出 —— 与动画走同一条环。
    assert run_validation(static_asset.root).passed is True
    assert validate_and_export_static_asset(
        static_asset.root, targets=["generic-json"]
    ).passed is True


def test_static_repair_refuses_when_the_asset_dir_is_not_under_output_dir(
    static_asset: ArtifactStore, config: Config, tmp_path: Path
) -> None:
    """重生成按 ``config.output_dir`` 定位资产 —— 对不上就会修到另一个目录去。"""
    _blank_static_image(static_asset)
    plan = plan_repairs(run_validation(static_asset.root))
    elsewhere = Config(
        provider=config.provider,
        model=config.model,
        output_dir=tmp_path / "another-outputs",
        cache_dir=config.cache_dir,
        max_repair_rounds=config.max_repair_rounds,
    )

    with pytest.raises(ProcessingError, match="output_dir"):
        execute_plan(static_asset.root, plan, elsewhere, allow_api=True)


# -- CLI ------------------------------------------------------------------


def test_cli_validate_exits_nonzero_on_failure(
    asset: ArtifactStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**验证失败时绝不标记为成功** —— 退出码必须能被脚本感知。"""
    monkeypatch.chdir(tmp_path)
    path = next(iter(sorted(asset.frames_of("walk_down").glob("*.png"))))
    Image.fromarray(np.zeros((32, 32, 4), dtype=np.uint8), "RGBA").save(path)

    result = runner.invoke(app, ["validate", str(asset.root)])
    assert result.exit_code == EXIT_VALIDATION_FAILED
    assert "未通过" in result.stdout


def test_cli_validate_passes_on_clean_asset(
    asset: ArtifactStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["validate", str(asset.root)])
    assert result.exit_code == EXIT_OK
    assert (asset.root / "validation-report.json").exists()


def test_cli_validate_json_is_machine_readable(
    asset: ArtifactStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["validate", str(asset.root), "--json"])
    assert result.exit_code == EXIT_OK
    assert json.loads(result.stdout)["passed"] is True


def test_cli_repair_defaults_to_offline_only(
    asset: ArtifactStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pixel-asset.yaml").write_text(
        f"provider: mock\nmodel: mock-image\noutput_dir: {asset.root.parent}\n",
        encoding="utf-8",
    )
    manifest = AssetManifest.load(asset.manifest_path)
    manifest.animations["walk_down"].frames = manifest.animations["walk_down"].frames[:5]
    manifest.save(asset.manifest_path)

    result = runner.invoke(app, ["repair", str(asset.root)])
    assert result.exit_code == EXIT_OK
    assert "跳过" in result.stdout


def test_cli_repair_then_validate_is_a_closed_loop(
    asset: ArtifactStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """repair 打印的下一步是"修复后请重新 validate 确认" —— 照它敲必须真能跑通。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pixel-asset.yaml").write_text(
        f"provider: mock\nmodel: mock-image\noutput_dir: {asset.root.parent}\n",
        encoding="utf-8",
    )
    _inject_local_defect(asset, "walk_down")

    assert runner.invoke(app, ["validate", str(asset.root)]).exit_code == EXIT_VALIDATION_FAILED
    repaired = runner.invoke(app, ["repair", str(asset.root)])
    assert repaired.exit_code == EXIT_OK
    assert "重新 validate" in repaired.stdout

    revalidated = runner.invoke(app, ["validate", str(asset.root)])
    assert revalidated.exit_code == EXIT_OK, revalidated.stdout
