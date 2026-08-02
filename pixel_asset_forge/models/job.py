"""Job 与状态机（PLAN §5.3）。

每个 ``(asset, action, direction)`` 三元组是一个独立任务，这样才能只重跑失败的部分。

状态机以**数据**形式表达（``_TRANSITIONS``），而不是一堆 if/else：
非法转移必须报错而不是静默容忍 —— 一个允许任意跳转的状态机等于没有状态机。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..constants import Direction
from ..errors import StateTransitionError


class JobStatus(StrEnum):
    PLANNED = "planned"
    GENERATING = "generating"
    GENERATED = "generated"
    PROCESSING = "processing"
    PROCESSED = "processed"
    VALIDATING = "validating"
    VALIDATED = "validated"
    VALIDATION_FAILED = "validation_failed"
    REPAIRING = "repairing"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    EXPORTED = "exported"
    FAILED = "failed"


class JobEvent(StrEnum):
    START_EXECUTION = "start_execution"
    CACHE_HIT = "cache_hit"
    DERIVE_READY = "derive_ready"
    PROVIDER_SUCCESS = "provider_success"
    RECOVER_GENERATED = "recover_generated"
    TRANSIENT_ERROR = "transient_error"
    PERMANENT_ERROR = "permanent_error"
    RETRY_LIMIT_EXCEEDED = "retry_limit_exceeded"
    START_PROCESSING = "start_processing"
    PROCESSING_DONE = "processing_done"
    PROCESSING_ERROR = "processing_error"
    START_VALIDATION = "start_validation"
    VALIDATION_PASSED = "validation_passed"
    VALIDATION_FAILED = "validation_failed"
    PLAN_REPAIR = "plan_repair"
    REPAIR_LIMIT_EXCEEDED = "repair_limit_exceeded"
    REPAIR_LOCAL = "repair_local"
    REPAIR_REGENERATE_GRID = "repair_regenerate_grid"
    REPAIR_REGENERATE_SEED = "repair_regenerate_seed"
    REQUIRE_APPROVAL = "require_approval"
    APPROVE = "approve"
    REJECT = "reject"
    EXPORT = "export"


class JobKind(StrEnum):
    STATIC = "static"
    """单张静态物件图。直接调用 API，不伪装成 seed 或 animation。"""

    SEED = "seed"
    """canonical seed。所有动画的身份基准，也是唯一的人工闸门。"""

    ANIMATION = "animation"
    """由 API 生成的动作网格。"""

    DERIVED = "derived"
    """由其他方向翻转 derive，不调用 API（ADR-006）。"""


TERMINAL_STATES: frozenset[JobStatus] = frozenset(
    {JobStatus.EXPORTED, JobStatus.APPROVED, JobStatus.FAILED}
)

#: 处于这些状态时任务需要调用 API 才能推进。
API_BOUND_STATES: frozenset[JobStatus] = frozenset({JobStatus.GENERATING})


class _Transition(BaseModel):
    model_config = ConfigDict(frozen=True)

    target: JobStatus
    effect: str
    calls_api: bool = False
    cascades: bool = False
    """是否级联作废全部下游任务（只有重生成种子图会这样）。"""


# PLAN §5.3 状态转移表。表外的一切转移都是非法的。
_TRANSITIONS: dict[tuple[JobStatus, JobEvent], _Transition] = {
    (JobStatus.PLANNED, JobEvent.START_EXECUTION): _Transition(
        target=JobStatus.GENERATING, effect="写 job 记录", calls_api=True
    ),
    (JobStatus.PLANNED, JobEvent.CACHE_HIT): _Transition(
        target=JobStatus.GENERATED, effect="复用既有 artifact，跳过 API 调用"
    ),
    # PLAN §5.3 之外的一条补充边：derived 任务从来不需要生成，
    # 它的"原图"就是源方向的产物。语义与缓存命中一致 —— 无 API 调用即得 artifact。
    (JobStatus.PLANNED, JobEvent.DERIVE_READY): _Transition(
        target=JobStatus.GENERATED, effect="源方向已就绪，直接翻转 derive"
    ),
    (JobStatus.GENERATING, JobEvent.PROVIDER_SUCCESS): _Transition(
        target=JobStatus.GENERATED, effect="落盘原图 + request id + prompt hash"
    ),
    (JobStatus.PROCESSING, JobEvent.RECOVER_GENERATED): _Transition(
        target=JobStatus.GENERATED, effect="处理阶段中断，从不可变原图恢复"
    ),
    (JobStatus.GENERATING, JobEvent.TRANSIENT_ERROR): _Transition(
        target=JobStatus.GENERATING, effect="指数退避重试，attempts +1", calls_api=True
    ),
    (JobStatus.GENERATING, JobEvent.PERMANENT_ERROR): _Transition(
        target=JobStatus.FAILED, effect="写可操作错误信息"
    ),
    (JobStatus.GENERATING, JobEvent.RETRY_LIMIT_EXCEEDED): _Transition(
        target=JobStatus.FAILED, effect="退避重试超限"
    ),
    (JobStatus.GENERATED, JobEvent.START_PROCESSING): _Transition(
        target=JobStatus.PROCESSING, effect="进入确定性处理链"
    ),
    (JobStatus.PROCESSING, JobEvent.PROCESSING_DONE): _Transition(
        target=JobStatus.PROCESSED, effect="写 frames/"
    ),
    (JobStatus.PROCESSING, JobEvent.PROCESSING_ERROR): _Transition(
        target=JobStatus.FAILED, effect="处理异常"
    ),
    (JobStatus.PROCESSED, JobEvent.START_VALIDATION): _Transition(
        target=JobStatus.VALIDATING, effect="进入验证引擎"
    ),
    (JobStatus.VALIDATED, JobEvent.START_VALIDATION): _Transition(
        target=JobStatus.VALIDATING, effect="重新验证既有成品"
    ),
    (JobStatus.EXPORTED, JobEvent.START_VALIDATION): _Transition(
        target=JobStatus.VALIDATING, effect="导出后产物发生变化，重新验证既有成品"
    ),
    (JobStatus.VALIDATION_FAILED, JobEvent.START_VALIDATION): _Transition(
        target=JobStatus.VALIDATING, effect="修正后重新验证"
    ),
    (JobStatus.VALIDATING, JobEvent.VALIDATION_PASSED): _Transition(
        target=JobStatus.VALIDATED, effect="写 validation-report.json"
    ),
    (JobStatus.VALIDATING, JobEvent.VALIDATION_FAILED): _Transition(
        target=JobStatus.VALIDATION_FAILED, effect="写 validation-report.json"
    ),
    (JobStatus.VALIDATION_FAILED, JobEvent.PLAN_REPAIR): _Transition(
        target=JobStatus.REPAIRING, effect="写 repair-plan.json"
    ),
    (JobStatus.VALIDATION_FAILED, JobEvent.REPAIR_LIMIT_EXCEEDED): _Transition(
        target=JobStatus.FAILED, effect="超过 max_repair_rounds"
    ),
    # 三条重入边 —— 修复机制的核心，分别对应"本地可修"/"这次生成废了"/"身份基准废了"。
    (JobStatus.REPAIRING, JobEvent.REPAIR_LOCAL): _Transition(
        target=JobStatus.PROCESSING, effect="本地重新处理，不调用 API"
    ),
    (JobStatus.REPAIRING, JobEvent.REPAIR_REGENERATE_GRID): _Transition(
        target=JobStatus.GENERATING, effect="重生成动作网格", calls_api=True
    ),
    (JobStatus.REPAIRING, JobEvent.REPAIR_REGENERATE_SEED): _Transition(
        target=JobStatus.PLANNED, effect="重生成种子图，级联作废全部下游任务", cascades=True
    ),
    (JobStatus.VALIDATED, JobEvent.REQUIRE_APPROVAL): _Transition(
        target=JobStatus.AWAITING_APPROVAL, effect="输出 contact sheet 等待人工确认"
    ),
    (JobStatus.AWAITING_APPROVAL, JobEvent.APPROVE): _Transition(
        target=JobStatus.APPROVED, effect="解锁下游动画任务"
    ),
    (JobStatus.AWAITING_APPROVAL, JobEvent.REJECT): _Transition(
        target=JobStatus.PLANNED, effect="重新生成种子图", cascades=True
    ),
    (JobStatus.VALIDATED, JobEvent.EXPORT): _Transition(
        target=JobStatus.EXPORTED, effect="写 exports/"
    ),
}


def allowed_events(status: JobStatus) -> tuple[JobEvent, ...]:
    return tuple(event for (state, event) in _TRANSITIONS if state == status)


class JobRecord(BaseModel):
    """一次状态转移的日志条目。所有修复操作必须留痕（Sprint 5 退出门槛）。"""

    model_config = ConfigDict(frozen=True)

    event: JobEvent
    from_status: JobStatus
    to_status: JobStatus
    effect: str
    detail: str | None = None


class Job(BaseModel):
    """单个可独立重跑的任务。"""

    model_config = ConfigDict(validate_assignment=True)

    id: str
    asset_id: str
    kind: JobKind
    status: JobStatus = JobStatus.PLANNED

    action: str | None = None
    direction: Direction | None = None

    frames: int | None = None
    fps: int | None = None
    loop: bool = True

    grid: tuple[int, int] | None = None
    """(cols, rows)。derived 与 seed 任务为 None。"""

    physical_size: tuple[int, int] | None = None
    """提交给 API 的物理尺寸。"""

    derived_from: str | None = None
    transform: Literal["flip_horizontal", "flip_vertical"] | None = None

    depends_on: tuple[str, ...] = ()

    attempts: int = 0
    repair_rounds: int = 0

    input_fingerprint: str | None = None
    """规范化输入 + pipeline/provider/model 的指纹，用于阻止同 ID 静默复用不同输入。"""

    prompt_hash: str | None = None
    request_id: str | None = None
    validated_processed_hash: str | None = Field(
        default=None, pattern=r"^[0-9A-Fa-f]{64}$"
    )
    """静态任务最近一次通过验证时绑定的 ``processed_hash``。"""
    error: str | None = None

    history: list[JobRecord] = Field(default_factory=list)

    # -- 派生属性 ---------------------------------------------------------

    @property
    def key(self) -> str:
        """动画键，形如 ``walk_down``；seed 任务返回 ``seed``。"""
        if self.kind is JobKind.SEED:
            return "seed"
        if self.kind is JobKind.STATIC:
            return "static"
        parts = [p for p in (self.action, self.direction) if p]
        return "_".join(parts)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    @property
    def calls_api(self) -> bool:
        """本任务在正常路径上是否需要 API 调用。derived 任务永远不需要。"""
        return self.kind in (JobKind.STATIC, JobKind.SEED, JobKind.ANIMATION)

    # -- 状态机 -----------------------------------------------------------

    def can(self, event: JobEvent) -> bool:
        return (self.status, event) in _TRANSITIONS

    def fire(self, event: JobEvent, *, detail: str | None = None) -> _Transition:
        """执行一次状态转移。非法转移抛 :class:`StateTransitionError`。"""
        transition = _TRANSITIONS.get((self.status, event))
        if transition is None:
            raise StateTransitionError(self.id, self.status.value, event.value)

        previous = self.status
        self.status = transition.target

        if event is JobEvent.TRANSIENT_ERROR:
            self.attempts += 1
        elif event is JobEvent.START_EXECUTION:
            self.attempts = max(self.attempts, 1)
        elif event is JobEvent.PLAN_REPAIR:
            self.repair_rounds += 1
        elif event in (JobEvent.REPAIR_REGENERATE_GRID, JobEvent.REPAIR_REGENERATE_SEED):
            # 重生成算一次全新的尝试，退避计数重置。
            self.attempts = 0

        if detail and transition.target is JobStatus.FAILED:
            self.error = detail

        self.history.append(
            JobRecord(
                event=event,
                from_status=previous,
                to_status=transition.target,
                effect=transition.effect,
                detail=detail,
            )
        )
        return transition

    def reset_to_planned(self, *, reason: str) -> None:
        """被上游级联作废。区别于 ``fire``：这是被动作废，不是自身事件。"""
        previous = self.status
        self.status = JobStatus.PLANNED
        self.attempts = 0
        self.error = None
        self.history.append(
            JobRecord(
                event=JobEvent.REPAIR_REGENERATE_SEED,
                from_status=previous,
                to_status=JobStatus.PLANNED,
                effect="被上游级联作废",
                detail=reason,
            )
        )


def make_job_id(
    asset_id: str,
    kind: JobKind,
    action: str | None = None,
    direction: str | None = None,
) -> str:
    """确定性 Job ID。

    同一请求重复执行必须产生同一批 ID —— 这是"不创建重复任务"（幂等）的实现方式。
    因此 ID 只能由 ``(asset_id, kind, action, direction)`` 决定，不含时间戳、不含随机数。
    """
    if kind is JobKind.SEED:
        return f"{asset_id}:seed"
    if kind is JobKind.STATIC:
        return f"{asset_id}:static"
    parts = [asset_id, action or "unknown"]
    if direction:
        parts.append(direction)
    return ":".join(parts)


class JobTable(BaseModel):
    """一个资产的全部任务 + 依赖关系。"""

    model_config = ConfigDict(validate_assignment=True)

    asset_id: str
    jobs: dict[str, Job] = Field(default_factory=dict)

    def __iter__(self) -> Iterator[Job]:  # type: ignore[override]
        return iter(self.jobs.values())

    def __len__(self) -> int:
        return len(self.jobs)

    def __contains__(self, job_id: object) -> bool:
        return job_id in self.jobs

    def add(self, job: Job) -> Job:
        """加入任务；同 ID 的不同输入不得被幂等逻辑静默吞掉。"""
        existing = self.jobs.get(job.id)
        if existing is not None:
            if (
                existing.input_fingerprint is not None
                and job.input_fingerprint is not None
                and existing.input_fingerprint != job.input_fingerprint
            ):
                raise ValueError(
                    f"任务 {job.id} 的 input_fingerprint 冲突："
                    f"既有 {existing.input_fingerprint}，新规划 {job.input_fingerprint}"
                )
            return existing
        self.jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(f"未知任务：{job_id}")
        return job

    def of_kind(self, kind: JobKind) -> tuple[Job, ...]:
        return tuple(j for j in self.jobs.values() if j.kind is kind)

    @property
    def seed_job(self) -> Job | None:
        jobs = self.of_kind(JobKind.SEED)
        return jobs[0] if jobs else None

    def dependents_of(self, job_id: str) -> tuple[Job, ...]:
        """直接依赖 ``job_id`` 的任务。"""
        return tuple(j for j in self.jobs.values() if job_id in j.depends_on)

    def transitive_dependents_of(self, job_id: str) -> tuple[Job, ...]:
        """全部下游任务（传递闭包）。重生成种子图时用它做级联作废。"""
        seen: set[str] = set()
        frontier = [job_id]
        while frontier:
            current = frontier.pop()
            for dep in self.dependents_of(current):
                if dep.id not in seen:
                    seen.add(dep.id)
                    frontier.append(dep.id)
        return tuple(self.jobs[i] for i in sorted(seen))

    def cascade_invalidate(self, job_id: str, *, reason: str) -> tuple[Job, ...]:
        """把 ``job_id`` 的全部下游任务打回 planned。"""
        affected = self.transitive_dependents_of(job_id)
        for job in affected:
            job.reset_to_planned(reason=reason)
        return affected

    def ready_jobs(self) -> tuple[Job, ...]:
        """依赖已满足、可以立即执行的任务。

        依赖的"满足"定义为上游进入了终态里的成功态（``approved`` / ``exported``）
        或 ``validated``。种子图未获批准时，全部动画任务都不 ready ——
        这就是人工闸门在调度层的落地。
        """
        done = {JobStatus.APPROVED, JobStatus.VALIDATED, JobStatus.EXPORTED}
        out = []
        for job in self.jobs.values():
            if job.is_terminal or job.status is not JobStatus.PLANNED:
                continue
            if all(self.jobs[d].status in done for d in job.depends_on if d in self.jobs):
                out.append(job)
        return tuple(out)

    def topological_order(self) -> tuple[Job, ...]:
        """按依赖拓扑排序。同层内按 ID 排序以保证输出稳定。"""
        remaining = dict(self.jobs)
        ordered: list[Job] = []
        emitted: set[str] = set()
        while remaining:
            layer = [
                job
                for job in remaining.values()
                if all(d in emitted or d not in self.jobs for d in job.depends_on)
            ]
            if not layer:
                raise ValueError(f"任务依赖存在环：{sorted(remaining)}")
            for job in sorted(layer, key=lambda j: j.id):
                ordered.append(job)
                emitted.add(job.id)
                del remaining[job.id]
        return tuple(ordered)

    def estimated_api_calls(self) -> int:
        """预计 API 调用次数。derived 任务与已完成任务不计。"""
        return sum(
            1
            for job in self.jobs.values()
            if job.calls_api and job.status in (JobStatus.PLANNED, JobStatus.GENERATING)
        )

    def signature(self) -> str:
        """任务集合的稳定指纹。用于判断"这次 plan 和上次是不是同一批任务"。"""
        payload = "|".join(
            f"{j.id}#{j.kind}#{j.frames}#{j.derived_from or ''}"
            + (f"#{j.input_fingerprint}" if j.input_fingerprint else "")
            for j in sorted(self.jobs.values(), key=lambda j: j.id)
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def summary(self) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        for job in self.jobs.values():
            by_status[job.status.value] = by_status.get(job.status.value, 0) + 1
        return {
            "asset_id": self.asset_id,
            "total_jobs": len(self.jobs),
            "estimated_api_calls": self.estimated_api_calls(),
            "derived_jobs": len(self.of_kind(JobKind.DERIVED)),
            "by_status": by_status,
            "signature": self.signature(),
        }


def merge_tables(base: JobTable, incoming: Iterable[Job]) -> JobTable:
    """把新规划的任务并入既有任务表，已存在的 ID 保留原状态。

    这是断点续跑与幂等的实现点：重新 ``plan`` 不会把已完成的任务打回 planned。
    """
    for job in incoming:
        base.add(job)
    return base
