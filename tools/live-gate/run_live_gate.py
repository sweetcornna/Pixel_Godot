"""真实资产质量闸门（PLAN §9.4）。

固定跑 5 次生成：3 块 tile、1 张 canonical seed、1 组 walk_down。这个脚本只消费
公开流水线 API，不替验证引擎重算图像指标；它做的事是把既有 ``Check.id`` 投影到
§9.4 的硬阈值，并把 PASS / FAIL / ERROR 分开记账。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pixel_asset_forge.config import API_KEY_ENV_VARS, Config, load_config
from pixel_asset_forge.models.request import load_request
from pixel_asset_forge.models.validation import (
    Check,
    CheckResult,
    Severity,
    ValidationReport,
)
from pixel_asset_forge.pipelines import approve_seed, create_animation, create_character
from pixel_asset_forge.pipelines.tileset import create_tileset
from pixel_asset_forge.pipelines.validation import run_validation
from pixel_asset_forge.validation.engine import (
    TILE_BORDER_DEVIATION_MAX as TILE_BORDER_MAX,
)
from pixel_asset_forge.validation.engine import (
    TILE_SEAM_RATIO_MAX as TILE_SEAM_MAX,
)

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ERROR = 2

PLANNED_CALLS = 5
EXPECTED_ASSET_UNITS = 3
TILE_COUNT = 3
TEXT_SUFFIXES = frozenset({".json", ".jsonl", ".log", ".md", ".txt", ".yaml", ".yml"})

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
EXAMPLES_DIR = REPO_ROOT / "examples"
RUNS_DIR = SCRIPT_DIR / "runs"
REPORTS_DIR = SCRIPT_DIR / "reports"


class GateState(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"

    @property
    def exit_code(self) -> int:
        return {
            GateState.PASS: EXIT_PASS,
            GateState.FAIL: EXIT_FAIL,
            GateState.ERROR: EXIT_ERROR,
        }[self]


class BudgetExceededError(RuntimeError):
    """规划调用数超过用户给 gate 的硬顶。"""


class GateDataError(RuntimeError):
    """验证引擎没有交出 gate 契约要求的数据。"""


class SecretLeakError(RuntimeError):
    """落盘自检发现 API Key 明文。"""


@dataclass(frozen=True)
class GateMetric:
    """§9.4 的一个量化判据；``check_id`` 必须来自验证引擎。"""

    asset: str
    check_id: str
    target: str
    measured: float | int
    threshold: float | int
    comparison: str = "<="

    @property
    def passed(self) -> bool:
        if self.comparison != "<=":  # pragma: no cover - 目前契约只有上限型指标
            raise GateDataError(f"不支持的比较方式：{self.comparison}")
        return self.measured <= self.threshold

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "check_id": self.check_id,
            "target": self.target,
            "measured": self.measured,
            "threshold": self.threshold,
            "comparison": self.comparison,
            "passed": self.passed,
        }


@dataclass(frozen=True)
class AssetAssessment:
    """三个质量单元之一；seed 没有上游 ValidationReport，来源会明确标注。"""

    name: str
    validation_passed: bool
    validation_source: str
    metrics: tuple[GateMetric, ...] = ()
    checks: tuple[Check, ...] = ()

    @property
    def passed(self) -> bool:
        return self.validation_passed and all(metric.passed for metric in self.metrics)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "validation_passed": self.validation_passed,
            "validation_source": self.validation_source,
            "metrics": [metric.to_dict() for metric in self.metrics],
            "checks": [check.model_dump(mode="json", exclude_none=True) for check in self.checks],
        }


@dataclass
class ExecutionData:
    """流水线边跑边写进来的内存状态；中途异常时仍能报告已完成部分。"""

    assessments: list[AssetAssessment] = field(default_factory=list)
    validation_reports: list[ValidationReport] = field(default_factory=list)
    completed_calls: int = 0
    phase: str = "初始化"


def enforce_budget(max_calls: int, planned_calls: int = PLANNED_CALLS) -> None:
    """在创建 run 目录、更不能在调用 provider 之后才检查预算。"""
    if planned_calls > max_calls:
        raise BudgetExceededError(
            f"规划调用 {planned_calls} 次，超过 --max-calls={max_calls}；拒绝执行"
        )


def redact_text(text: str, secrets: Iterable[str]) -> str:
    """输出前删掉环境里的 Key；长 Key 先替换，避免短值破坏长值匹配。"""
    redacted = text
    for secret in sorted({value for value in secrets if value}, key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _redact_value(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, dict):
        return {key: _redact_value(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item, secrets) for item in value)
    return value


def api_key_values(env: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(value for name in API_KEY_ENV_VARS if (value := env.get(name, "").strip()))


def install_log_redaction(secrets: Sequence[str]) -> None:
    """第三方异常也走 LogRecordFactory，不能只保护本脚本的 ``print``。"""
    if not secrets:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        try:
            rendered = record.getMessage()
        except (TypeError, ValueError):
            rendered = str(record.msg)
        record.msg = redact_text(rendered, secrets)
        record.args = ()
        return record

    logging.setLogRecordFactory(factory)


def assert_no_secrets(
    paths: Iterable[Path],
    secrets: Sequence[str],
    *,
    cleanup_paths: Iterable[Path] | None = None,
) -> None:
    """逐字节读回落盘文件；发现 Key 就删掉本次报告并转成 ERROR。"""
    candidates = tuple(dict.fromkeys(path for path in paths if path.is_file()))
    secret_bytes = tuple(value.encode("utf-8") for value in secrets if value)
    leaked = any(secret in path.read_bytes() for path in candidates for secret in secret_bytes)
    if not leaked:
        return

    # 一份报告的 JSON 与 Markdown 是同一个结论，任一受污染就不能留下另一半。
    for path in tuple(cleanup_paths or candidates):
        path.unlink(missing_ok=True)
    raise SecretLeakError("落盘自检发现 API Key 明文；本次报告已删除")


def classify_result(
    assessments: Sequence[AssetAssessment], *, error: str | None = None
) -> GateState:
    if error is not None:
        return GateState.ERROR
    if len(assessments) != EXPECTED_ASSET_UNITS:
        return GateState.ERROR
    return GateState.PASS if all(asset.passed for asset in assessments) else GateState.FAIL


def _checks_for(
    report: ValidationReport,
    check_id: str,
    *,
    target: str | None = None,
    expected: int,
) -> list[Check]:
    checks = [
        check
        for check in report.checks
        if check.id == check_id and (target is None or check.target == target)
    ]
    if len(checks) != expected:
        target_note = f" target={target}" if target else ""
        raise GateDataError(
            f"验证报告中的 {check_id}{target_note} 应有 {expected} 条，实际 {len(checks)} 条"
        )
    return checks


def _metric_from_check(
    asset: str,
    check: Check,
    *,
    threshold: float | int | None = None,
) -> GateMetric:
    if not isinstance(check.measured, (int, float)) or isinstance(check.measured, bool):
        raise GateDataError(f"{check.id}/{check.target} 没有数值 measured")
    bound = check.threshold if threshold is None else threshold
    if not isinstance(bound, (int, float)) or isinstance(bound, bool):
        raise GateDataError(f"{check.id}/{check.target} 没有数值 threshold")
    return GateMetric(
        asset=asset,
        check_id=check.id,
        target=check.target,
        measured=check.measured,
        threshold=bound,
    )


def _palette_metric(asset: str, target: str, colors: Sequence[str], max_colors: int) -> GateMetric:
    # 验证引擎的同名检查允许少量像素越界；9.4 明确收紧为颜色数不得超过 request。
    # 仍沿用 palette_overflow id，报告不会出现一套只有 live gate 认识的新指标名。
    return GateMetric(
        asset=asset,
        check_id="palette_overflow",
        target=target,
        measured=len(colors),
        threshold=max_colors,
    )


def assess_tileset(
    report: ValidationReport, *, palette: Sequence[str], max_colors: int
) -> AssetAssessment:
    metrics = [
        *(
            _metric_from_check("grass_field", check, threshold=TILE_SEAM_MAX)
            for check in _checks_for(report, "tile_seam", expected=TILE_COUNT)
        ),
        *(
            _metric_from_check("grass_field", check, threshold=TILE_BORDER_MAX)
            for check in _checks_for(report, "tile_border", expected=TILE_COUNT)
        ),
        _palette_metric("grass_field", "tileset_palette", palette, max_colors),
    ]
    return AssetAssessment(
        name="grass_field",
        validation_passed=report.passed,
        validation_source="ValidationReport.passed",
        metrics=tuple(metrics),
        checks=tuple(report.checks),
    )


def assess_seed(*, palette: Sequence[str], max_colors: int) -> AssetAssessment:
    return AssetAssessment(
        name="knight_01_seed",
        validation_passed=True,
        validation_source="create_character completed; upstream has no seed ValidationReport",
        metrics=(_palette_metric("knight_01_seed", "seed_palette", palette, max_colors),),
    )


def assess_animation(
    report: ValidationReport, *, palette: Sequence[str], max_colors: int
) -> AssetAssessment:
    checks = tuple(check for check in report.checks if check.target == "walk_down")
    if not checks:
        raise GateDataError("角色验证报告里没有 walk_down 检查项")
    animation_report = ValidationReport(
        asset_id="knight_01_walk_down",
        checks=list(checks),
        thresholds_used=report.thresholds_used,
    )
    anchor = _checks_for(animation_report, "anchor_drift", target="walk_down", expected=1)[0]
    return AssetAssessment(
        name="knight_01_walk_down",
        validation_passed=animation_report.passed,
        validation_source="ValidationReport.passed (target=walk_down)",
        metrics=(
            _metric_from_check("knight_01_walk_down", anchor),
            _palette_metric(
                "knight_01_walk_down", "walk_down_palette", palette, max_colors
            ),
        ),
        checks=checks,
    )


def _copy_requests(run_dir: Path) -> tuple[Path, Path]:
    request_dir = run_dir / "requests"
    request_dir.mkdir(parents=True)
    grass = request_dir / "grass_field.yaml"
    knight = request_dir / "knight.yaml"
    shutil.copy2(EXAMPLES_DIR / grass.name, grass)
    shutil.copy2(EXAMPLES_DIR / knight.name, knight)
    return grass, knight


def _load_live_config(run_dir: Path) -> Config:
    # user/project config 会把这次人工校准悄悄变成另一组参数；live gate 只认环境变量。
    # output/cache 是 gate 自己的隔离边界，不是用户可调的生成参数。
    return load_config(
        project_config=run_dir / ".no-project-config.yaml",
        user_config=run_dir / ".no-user-config.yaml",
        overrides={
            "output_dir": run_dir / "assets",
            "cache_dir": run_dir / "cache",
            "cache_enabled": False,
        },
        env=dict(os.environ),
    )


def execute_live_gate(run_dir: Path, data: ExecutionData) -> tuple[Config, Path, Path]:
    grass_request_path, knight_request_path = _copy_requests(run_dir)
    grass_request = load_request(grass_request_path)
    knight_request = load_request(knight_request_path)
    config = _load_live_config(run_dir)
    if config.provider != "mock":
        # 缺 Key 必须在花第一笔钱之前变成 ERROR，不能等 provider 内部跑到一半。
        config.require_api_key()

    grass_dir = config.asset_dir("grass_field")
    knight_dir = config.asset_dir("knight_01")

    data.phase = "生成 grass_field tileset"
    tileset = create_tileset(grass_request_path, config)
    data.completed_calls += tileset.generated

    data.phase = "验证 grass_field tileset"
    grass_report = run_validation(grass_dir)
    data.validation_reports.append(grass_report)
    data.assessments.append(
        assess_tileset(
            grass_report,
            palette=tileset.palette,
            max_colors=grass_request.style.max_colors,
        )
    )

    data.phase = "生成 knight_01 seed"
    seed = create_character(knight_request_path, config)
    data.completed_calls += int(not seed.cached)
    data.assessments.append(
        assess_seed(palette=seed.palette, max_colors=knight_request.style.max_colors)
    )

    data.phase = "批准 knight_01 seed"
    approve_seed(knight_dir)

    data.phase = "生成 knight_01 walk_down"
    animation = create_animation(
        knight_dir,
        action="walk",
        direction="down",
        config=config,
    )
    if animation.frames != 8:
        raise GateDataError(f"walk_down 期望 8 帧，流水线返回 {animation.frames} 帧")
    data.completed_calls += int(animation.calls_api and not animation.cached)

    data.phase = "验证 knight_01 walk_down"
    knight_report = run_validation(knight_dir)
    data.validation_reports.append(knight_report)
    data.assessments.append(
        assess_animation(
            knight_report,
            palette=animation.palette,
            max_colors=knight_request.style.max_colors,
        )
    )
    data.phase = "汇总报告"
    return config, grass_dir, knight_dir


def _report_payload(
    *,
    timestamp: str,
    run_dir: Path,
    provider: str,
    model: str,
    data: ExecutionData,
    error: str | None,
) -> dict[str, Any]:
    state = classify_result(data.assessments, error=error)
    all_checks = [check for report in data.validation_reports for check in report.checks]
    blocking = [
        check
        for check in all_checks
        if check.result is CheckResult.FAIL
        and check.severity in (Severity.FATAL, Severity.HIGH)
    ]
    metrics = [metric for asset in data.assessments for metric in asset.metrics]
    check_summary = {
        "total": len(all_checks),
        "passed": sum(check.result is CheckResult.PASS for check in all_checks),
        "failed": sum(check.result is CheckResult.FAIL for check in all_checks),
        "warnings": sum(check.result is CheckResult.WARN for check in all_checks),
        "skipped": sum(check.result is CheckResult.SKIP for check in all_checks),
    }
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "gate": "live",
        "timestamp_utc": timestamp,
        "status": state.value,
        "exit_code": state.exit_code,
        "provider": provider,
        "model": model,
        "run_dir": str(run_dir.relative_to(REPO_ROOT)),
        "budget": {
            "planned_calls": PLANNED_CALLS,
            "completed_calls": data.completed_calls,
            "assets": [
                {"name": "grass_field tileset", "calls": 3},
                {"name": "knight_01 seed", "calls": 1},
                {"name": "knight_01 walk_down", "calls": 1},
            ],
        },
        "summary": {
            "assets_passed": sum(asset.passed for asset in data.assessments),
            "assets_total": EXPECTED_ASSET_UNITS,
            "fatal_high_failures": len(blocking),
            "metrics_passed": sum(metric.passed for metric in metrics),
            "metrics_total": len(metrics),
            "checks": check_summary,
        },
        "assets": [asset.to_dict() for asset in data.assessments],
        "metrics": [metric.to_dict() for metric in metrics],
        # 原始报告不裁剪：medium / low 的 FAIL、WARN、SKIP 都是校准样本。
        "validation_reports": [report.to_dict() for report in data.validation_reports],
    }
    if error is not None:
        payload["error"] = {"phase": data.phase, "message": error}
    return payload


def _markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    budget = payload["budget"]
    lines = [
        "# Live gate 报告",
        "",
        f"- 结果：**{payload['status']}**（退出码 {payload['exit_code']}）",
        f"- UTC：`{payload['timestamp_utc']}`",
        f"- Provider：`{payload['provider']}` / `{payload['model']}`",
        f"- 调用：规划 {budget['planned_calls']}，完成 {budget['completed_calls']}",
        f"- 资产级：{summary['assets_passed']}/{summary['assets_total']}",
        f"- fatal / high 失败：{summary['fatal_high_failures']}",
        "",
        "## 资产结论",
        "",
        "| 质量单元 | 资产级验证来源 | 结果 |",
        "|---|---|---|",
    ]
    for asset in payload["assets"]:
        source = str(asset["validation_source"]).replace("|", "\\|")
        result = "PASS" if asset["passed"] else "FAIL"
        lines.append(f"| `{asset['name']}` | {source} | **{result}** |")

    lines.extend(
        [
            "",
            "## 量化指标",
            "",
            "| 资产 | 检查项 id | target | 实测 | 阈值 | 结果 |",
            "|---|---|---|---:|---:|---|",
        ]
    )
    for metric in payload["metrics"]:
        result = "PASS" if metric["passed"] else "FAIL"
        lines.append(
            f"| `{metric['asset']}` | `{metric['check_id']}` | `{metric['target']}` | "
            f"{metric['measured']} | {metric['comparison']} {metric['threshold']} | {result} |"
        )

    lines.extend(
        [
            "",
            "## 验证检查汇总",
            "",
            "| total | pass | fail | warn | skip |",
            "|---:|---:|---:|---:|---:|",
            f"| {summary['checks']['total']} | {summary['checks']['passed']} | "
            f"{summary['checks']['failed']} | {summary['checks']['warnings']} | "
            f"{summary['checks']['skipped']} |",
            "",
            "> medium / low 不阻断，但原始检查已完整保存在 report.json 的 "
            "`validation_reports`。",
        ]
    )
    if "error" in payload:
        message = str(payload["error"]["message"]).replace("\n", " ")
        lines.extend(["", "## 执行错误", "", f"`{payload['error']['phase']}`：{message}"])
    return "\n".join(lines) + "\n"


def write_reports(
    report_dir: Path, payload: dict[str, Any], *, secrets: Sequence[str], run_dir: Path
) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=False)
    safe_payload = _redact_value(payload, secrets)
    json_path = report_dir / "report.json"
    md_path = report_dir / "report.md"
    json_path.write_text(
        json.dumps(safe_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_path.write_text(_markdown(safe_payload), encoding="utf-8")

    run_text_files = (
        path
        for path in run_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES
    )
    assert_no_secrets(
        [json_path, md_path, *run_text_files],
        secrets,
        cleanup_paths=(json_path, md_path),
    )
    return json_path, md_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行 PLAN §9.4 真实资产质量闸门")
    parser.add_argument(
        "--max-calls",
        type=int,
        default=PLANNED_CALLS,
        help=f"允许的调用硬顶（默认 {PLANNED_CALLS}；固定资产集规划 {PLANNED_CALLS}）",
    )
    return parser


def _emit(message: str, secrets: Sequence[str]) -> None:
    print(redact_text(message, secrets), flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    secrets = api_key_values(os.environ)
    install_log_redaction(secrets)
    _emit(
        "预算：grass_field tileset 3 次 + knight_01 seed 1 次 + "
        f"knight_01 walk_down 1 次 = {PLANNED_CALLS} 次；--max-calls={args.max_calls}",
        secrets,
    )
    try:
        enforce_budget(args.max_calls)
    except BudgetExceededError as exc:
        _emit(f"RESULT ERROR（退出码 {EXIT_ERROR}）：{exc}", secrets)
        return EXIT_ERROR

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = RUNS_DIR / timestamp
    report_dir = REPORTS_DIR / timestamp
    run_dir.mkdir(parents=True, exist_ok=False)
    data = ExecutionData()
    config: Config | None = None
    error: str | None = None
    try:
        config, _grass_dir, _knight_dir = execute_live_gate(run_dir, data)
    except Exception as exc:  # ERROR 必须兜住端点、Key、处理中断和 gate 自身异常
        error = redact_text(f"{type(exc).__name__}: {exc}", secrets)

    provider = config.provider if config is not None else os.environ.get(
        "PIXEL_ASSET_PROVIDER", "openai"
    )
    model = config.model if config is not None else os.environ.get(
        "PIXEL_ASSET_MODEL", "gpt-image-2"
    )
    payload = _report_payload(
        timestamp=timestamp,
        run_dir=run_dir,
        provider=provider,
        model=model,
        data=data,
        error=error,
    )
    state = GateState(payload["status"])
    try:
        json_path, md_path = write_reports(
            report_dir, payload, secrets=secrets, run_dir=run_dir
        )
    except Exception as exc:
        _emit(
            f"RESULT ERROR（退出码 {EXIT_ERROR}）：报告落盘/脱敏自检失败："
            f"{type(exc).__name__}: {exc}",
            secrets,
        )
        return EXIT_ERROR

    _emit(
        f"报告：{json_path.relative_to(REPO_ROOT)}；{md_path.relative_to(REPO_ROOT)}",
        secrets,
    )
    _emit(
        f"汇总：资产 {payload['summary']['assets_passed']}/"
        f"{payload['summary']['assets_total']}，fatal/high 失败 "
        f"{payload['summary']['fatal_high_failures']}，量化指标 "
        f"{payload['summary']['metrics_passed']}/{payload['summary']['metrics_total']}",
        secrets,
    )
    if error is not None:
        _emit(f"错误阶段：{data.phase}；{error}", secrets)
    _emit(f"RESULT {state.value}（退出码 {state.exit_code}）", secrets)
    return state.exit_code


if __name__ == "__main__":
    sys.exit(main())
