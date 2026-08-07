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
from ..constants import split_animation_key
from ..errors import ProcessingError, RepairLimitExceededError
from ..logging_utils import get_logger
from ..models.job import Job, JobEvent, JobKind, JobStatus, JobTable
from ..models.request import load_request
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
    return split_animation_key(key)


def _find_job(store: ArtifactStore, target: str) -> tuple[JobTable | None, Job | None]:
    """按验证目标找任务 —— 判据是 ``Job.key``，不是"拼一个动画 ID 出来"。

    原来这里拼 ``make_job_id(..., JobKind.ANIMATION, action, direction)``。
    静态资产的验证目标恰好是 ``static``，拼出来的 ``<asset>:static`` 与静态任务
    的真实 ID **一字不差** —— 于是静态任务被当成动作网格推进，实测一路推到
    ``generating``（那是"有一次调用在飞"的意思），随后 ``create_animation``
    抛指纹冲突，连 ``repair-log.json`` 都没来得及写。

    ``Job.key`` 是验证目标的来源本身（静态图 ``static``、动作网格 ``walk_down``、
    tile ``grass``），照它反查就不会再把任务类型猜错。
    """
    table = store.load_job_table()
    if table is None:
        return None, None
    return table, next((job for job in table if job.key == target), None)


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
        _settle_reprocess(store, step, JobEvent.PROCESSING_ERROR, f"没有找到 {step.target} 的原图")
        return RepairOutcome(step, False, f"没有找到 {step.target} 的原图，无法离线重跑")
    _settle_reprocess(store, step, JobEvent.PROCESSING_DONE, "离线重跑处理链完成")
    return RepairOutcome(step, True, f"已离线重跑处理链（未调用 API）：{step.target}")


def _regenerate(
    store: ArtifactStore, step: RepairStep, config: Config, *, allow_api: bool
) -> RepairOutcome:
    _table, job = _find_job(store, step.target)
    is_static = job is not None and job.kind is JobKind.STATIC
    what = "静态原图" if is_static else "动作网格"

    if not allow_api:
        return RepairOutcome(
            step,
            False,
            f"需要重生成{what}（会调用 API）。确认后加 --allow-api 执行。",
        )

    if is_static:
        return _regenerate_static(store, step, config)

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


def _regenerate_static(
    store: ArtifactStore, step: RepairStep, config: Config
) -> RepairOutcome:
    """重生成静态资产的那一张原图。

    静态资产没有动作网格，走 ``create_animation`` 只会先撞上人工闸门或指纹冲突 ——
    而任务此时已经被推进过了。改走它自己的流水线：``create_static_asset`` 的
    ``regenerate=True`` 就是为"这次生成废了"准备的（pack 协调器也用它）。
    """
    from ..pipelines.static_asset import create_static_asset

    request_path = store.request_path
    if not request_path.exists():
        raise ProcessingError(
            f"{store.root} 下没有 request.yaml —— 静态资产无法重生成，请重跑 create-asset"
        )

    # `create_static_asset` 按 `config.output_dir / <asset_id>` 定位资产目录，
    # 而 repair 的资产目录是用户在命令行给的。两者不一致时，重生成会落到**另一个
    # 目录**，这里却照样报"已重生成" —— 与其修错地方，不如直接说清楚。
    request = load_request(request_path)
    expected = ArtifactStore.for_asset(config.output_dir, request.asset_id).root
    if expected.resolve() != store.root.resolve():
        raise ProcessingError(
            f"{store.root} 不在当前配置的 output_dir 下（按配置应为 {expected}）。"
            "重生成会写到配置指向的目录，拒绝执行 —— 请用 --config 指定匹配的配置。"
        )

    _advance_job(store, step, JobEvent.REPAIR_REGENERATE_GRID)
    create_static_asset(request_path, config, regenerate=True)
    return RepairOutcome(step, True, f"已重生成静态原图：{step.target}")


def _advance_job(store: ArtifactStore, step: RepairStep, event: JobEvent) -> None:
    """把任务推过 ``validation_failed → repairing → …`` 并留痕。

    状态机在这里不是装饰：``repairing`` 的三条出边分别对应"本地可修"、
    "这次生成废了"、"身份基准废了"三种严重程度，走哪条边本身就是修复决策的记录。
    """
    table, job = _find_job(store, step.target)
    if table is None or job is None:
        return

    if job.status is not JobStatus.VALIDATION_FAILED:
        job.status = JobStatus.VALIDATION_FAILED
    job.fire(JobEvent.PLAN_REPAIR, detail=f"失败项：{', '.join(step.reasons)}")
    job.fire(event, detail=step.detail)
    store.save_job_table(table)


def _settle_reprocess(store: ArtifactStore, step: RepairStep, event: JobEvent, detail: str) -> None:
    """把本地重跑的结果落到任务状态上，**让任务回到可验证的状态**。

    ``run_process`` 是纯离线处理，从不碰任务表 —— 少了这一步，本地修复完的任务
    就停在 ``processing``，而 ``validate`` 只收 ``processed`` / ``validating`` /
    ``validated`` / ``validation_failed``（静态另收 ``exported``）。
    实测：修完再 validate 报"没有可验证任务，当前状态：approved, planned,
    processing" —— 而 repair 自己打印的下一步正是"修复后请重新 validate 确认"。
    整个 ``validation_failed → repairing → processing → processed → validating``
    的环就断在这里。

    重跑不出东西时走 ``PROCESSING_ERROR``（→ ``failed``）而不是原地不动：
    原图都找不到，这个任务离线补不回来，只能重生成 —— 状态得如实说出这件事。
    """
    table, job = _find_job(store, step.target)
    if table is None or job is None or not job.can(event):
        return
    job.fire(event, detail=detail)
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
