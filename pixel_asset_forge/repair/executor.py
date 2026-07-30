"""执行修复计划。

**所有修复操作都要留日志**（Sprint 5 退出门槛）—— 写进 job history 与
``repair-log.json``。修复是"系统自己改自己的产物"，不留痕的话，
出问题时根本分不清当前产物是第几轮修复的结果。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Config
from ..errors import ProcessingError, RepairLimitExceededError
from ..logging_utils import get_logger
from ..models.job import JobEvent, JobKind, JobStatus, make_job_id
from ..storage.artifacts import ArtifactStore
from .planner import RepairAction, RepairPlan, RepairStep

logger = get_logger("repair")

REPAIR_LOG_FILE = "repair-log.json"


@dataclass
class RepairOutcome:
    step: RepairStep
    performed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {**self.step.to_dict(), "performed": self.performed, "outcome": self.detail}


def _split_key(key: str) -> tuple[str, str | None]:
    parts = key.split("_", 1)
    if len(parts) == 1:
        return (parts[0], None)
    direction = parts[1] if parts[1] in ("down", "left", "right", "up") else None
    return (parts[0], direction)


def execute_plan(
    asset_dir: str | Path,
    plan: RepairPlan,
    config: Config,
    *,
    allow_api: bool = False,
) -> list[RepairOutcome]:
    """执行修复计划。

    ``allow_api=False``（默认）时只跑本地修复 —— 需要重生成的步骤会被列出但不执行。
    这是刻意的：**花钱的动作必须由用户显式同意**，不该因为跑了一次 repair 就悄悄发生。
    """
    store = ArtifactStore(root=Path(asset_dir))
    if plan.exhausted:
        raise RepairLimitExceededError(
            f"{plan.asset_id} 已用满 {plan.max_rounds} 轮修复。"
            "继续修复前请人工检查 —— 反复自动重生成很可能是在掩盖一个系统性问题。"
        )

    outcomes: list[RepairOutcome] = []
    for step in plan.steps:
        if step.action is RepairAction.REPROCESS:
            outcomes.append(_reprocess(store, step))
        elif step.action is RepairAction.REGENERATE_GRID:
            outcomes.append(_regenerate(store, step, config, allow_api=allow_api))
        else:
            outcomes.append(
                RepairOutcome(step, False, "需要人工介入，未自动执行")
            )

    _write_log(store, plan, outcomes)
    return outcomes


def _reprocess(store: ArtifactStore, step: RepairStep) -> RepairOutcome:
    """本地重跑处理链。不调用 API。"""
    from ..pipelines.process import run_process

    _advance_job(store, step, JobEvent.REPAIR_LOCAL)
    summaries = run_process(store.root, only=step.target)
    if not summaries:
        return RepairOutcome(step, False, f"没有找到 {step.target} 的原图，无法离线重跑")
    return RepairOutcome(step, True, f"已离线重跑处理链（未调用 API）：{step.target}")


def _regenerate(
    store: ArtifactStore, step: RepairStep, config: Config, *, allow_api: bool
) -> RepairOutcome:
    if not allow_api:
        return RepairOutcome(
            step,
            False,
            "需要重生成动作网格（会调用 API）。确认后加 --allow-api 执行。",
        )

    from ..pipelines.animation import create_animation

    action, direction = _split_key(step.target)
    _advance_job(store, step, JobEvent.REPAIR_REGENERATE_GRID)
    create_animation(
        store.root,
        action=action,
        direction=direction,  # type: ignore[arg-type]
        config=config,
        regenerate=True,
    )
    return RepairOutcome(step, True, f"已重生成动作网格：{step.target}")


def _advance_job(store: ArtifactStore, step: RepairStep, event: JobEvent) -> None:
    """把任务推过 ``validation_failed → repairing → …`` 并留痕。

    状态机在这里不是装饰：``repairing`` 的三条出边分别对应"本地可修"、
    "这次生成废了"、"身份基准废了"三种严重程度，走哪条边本身就是修复决策的记录。
    """
    table = store.load_job_table()
    if table is None:
        return

    action, direction = _split_key(step.target)
    job_id = make_job_id(table.asset_id, JobKind.ANIMATION, action, direction)
    if job_id not in table:
        return

    job = table.get(job_id)
    if job.status is not JobStatus.VALIDATION_FAILED:
        job.status = JobStatus.VALIDATION_FAILED
    job.fire(JobEvent.PLAN_REPAIR, detail=f"失败项：{', '.join(step.reasons)}")
    job.fire(event, detail=step.detail)
    store.save_job_table(table)


def _write_log(store: ArtifactStore, plan: RepairPlan, outcomes: list[RepairOutcome]) -> None:
    path = store.root / REPAIR_LOG_FILE
    history: list[dict[str, Any]] = []
    if path.exists():
        try:
            history = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            history = []

    history.append(
        {
            "round": plan.rounds_used + 1,
            "max_rounds": plan.max_rounds,
            "outcomes": [o.to_dict() for o in outcomes],
        }
    )
    path.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def rounds_used(asset_dir: str | Path) -> int:
    """已用的修复轮次。用于 ``max_repair_rounds`` 判定。"""
    path = Path(asset_dir) / REPAIR_LOG_FILE
    if not path.exists():
        return 0
    try:
        return len(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        raise ProcessingError(f"{path} 不是合法 JSON") from None
