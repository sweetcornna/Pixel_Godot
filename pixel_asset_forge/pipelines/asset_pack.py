"""静态资产 pack 固定 worker 协调器。"""

from __future__ import annotations

import asyncio
import signal
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .. import PACK_SCHEMA_VERSION, PIPELINE_VERSION
from ..config import Config
from ..errors import PauseRequested, PlanError, ProviderError, redact
from ..logging_utils import get_logger
from ..models.job import JobKind, JobStatus
from ..models.pack import StaticAssetPack, input_fingerprint, load_pack
from ..models.request import load_request
from ..processing.background import resolve_key_color
from ..prompts import compile_static_prompt
from ..providers import ImageProvider, Throttle, get_provider
from ..storage.artifacts import ArtifactStore
from ..storage.atomic import atomic_write_json, atomic_write_text
from ..storage.cache import GenerationCache
from .static_asset import (
    StaticAssetResult,
    create_static_asset,
    validate_and_export_static_asset,
)
from .validation import static_validation_binding

logger = get_logger("pipeline.asset_pack")

PackOutcome = Literal[
    "exported",
    "validation_failed",
    "provider_failed",
    "processing_failed",
    "paused",
    "skipped",
    "outcome_missing",
]


class AssetOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    job_id: str
    input_fingerprint: str
    provider: str
    model: str
    outcome: PackOutcome
    job_status: str
    stage: str
    cached: bool = False
    resumed: bool = False
    skipped: bool = False
    outcome_unknown: bool = False
    error_code: str | None = None
    error: str | None = None
    request_id: str | None = None
    artifact_root: str


class PackSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = PACK_SCHEMA_VERSION
    pipeline_version: str = PIPELINE_VERSION
    pack_id: str
    pack_type: str
    provider: str
    model: str
    worker_count: int
    interrupted: bool = False
    interrupted_by: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    assets: list[AssetOutcome] = Field(default_factory=list)

    def refresh_counts(self) -> None:
        counts = {name: 0 for name in (
            "total", "exported", "validation_failed", "provider_failed",
            "processing_failed", "paused", "skipped", "cached", "resumed",
            "outcome_unknown", "outcome_missing",
        )}
        counts["total"] = len(self.assets)
        for asset in self.assets:
            counts[asset.outcome] += 1
            counts["cached"] += int(asset.cached)
            counts["resumed"] += int(asset.resumed)
            counts["outcome_unknown"] += int(asset.outcome_unknown)
        self.counts = counts


@dataclass
class PackRunControl:
    stop: threading.Event = field(default_factory=threading.Event)
    signal_number: int | None = None

    def request_stop(self, signum: int | None = None) -> None:
        if self.stop.is_set():
            return
        self.signal_number = signum
        self.stop.set()


@dataclass(frozen=True)
class _WorkItem:
    request_path: Path
    asset_id: str
    fingerprint: str
    resumed: bool


def _pack_dir(config: Config, pack_id: str) -> Path:
    return config.output_dir / "_packs" / pack_id


def _summary_path(config: Config, pack_id: str) -> Path:
    return _pack_dir(config, pack_id) / "pack-summary.json"


def _write_expanded_request(request: object, path: Path) -> Path:
    payload = request.model_dump(mode="json", exclude_none=True)  # type: ignore[attr-defined]
    return atomic_write_text(path, yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))


def _job_state(
    config: Config, asset_id: str
) -> tuple[JobStatus | None, bool, str | None]:
    store = ArtifactStore.for_asset(config.output_dir, asset_id)
    table = store.load_job_table()
    if table is None:
        return (None, False, None)
    jobs = table.of_kind(JobKind.STATIC)
    if not jobs:
        return (None, False, None)
    return (
        jobs[0].status,
        jobs[0].status is not JobStatus.PLANNED,
        jobs[0].input_fingerprint,
    )


def _require_saved_plans(
    pack: StaticAssetPack, config: Config, pack_path: str | Path
) -> None:
    """拒绝执行没有经过离线规划或规划指纹已经过期的资产。"""
    missing: list[str] = []
    mismatched: list[str] = []
    for request in pack.expand_requests():
        table = ArtifactStore.for_asset(config.output_dir, request.asset_id).load_job_table()
        jobs = table.of_kind(JobKind.STATIC) if table is not None else ()
        if not jobs:
            missing.append(request.asset_id)
            continue
        expected = input_fingerprint(request, config.provider, config.model)
        if jobs[0].input_fingerprint != expected:
            mismatched.append(request.asset_id)

    if not missing and not mismatched:
        return

    reasons: list[str] = []
    if missing:
        reasons.append(f"缺少已保存任务表：{'、'.join(missing)}")
    if mismatched:
        reasons.append(f"规划指纹与当前请求不一致：{'、'.join(mismatched)}")
    command = f"pixel-asset plan {Path(pack_path)} --save"
    raise PlanError(
        "批量执行前必须先完成离线规划；"
        f"{'；'.join(reasons)}。请先运行 `{command}`，确认计划后再重试。"
    )


def _reset_failed_static_job(config: Config, asset_id: str) -> bool:
    """把显式请求重试的失败任务恢复到最近一个可确认检查点。"""
    store = ArtifactStore.for_asset(config.output_dir, asset_id)
    table = store.load_job_table()
    if table is None:
        return False
    changed = False
    for job in table.of_kind(JobKind.STATIC):
        if job.status is not JobStatus.FAILED:
            continue
        has_source = store.source_path("static").exists()
        job.status = JobStatus.GENERATED if has_source else JobStatus.PLANNED
        job.attempts = 0
        job.repair_rounds = 0
        job.error = None
        if not has_source:
            job.prompt_hash = None
            job.request_id = None
        changed = True
    if changed:
        store.save_job_table(table)
    return changed


def _persist_summary(
    pack: StaticAssetPack,
    config: Config,
    outcomes: dict[str, AssetOutcome],
    *,
    control: PackRunControl,
) -> PackSummary:
    assets = []
    for request in pack.expand_requests():
        outcome = outcomes.get(request.asset_id)
        if outcome is None:
            outcome = AssetOutcome(
                asset_id=request.asset_id,
                job_id=f"{request.asset_id}:static",
                input_fingerprint=input_fingerprint(
                    request, config.provider, config.model
                ),
                provider=config.provider,
                model=config.model,
                outcome="outcome_missing",
                job_status="unknown",
                stage="outcome_missing",
                error_code="outcome_missing",
                error="资产未产生执行结果；汇总使用显式占位条目",
                artifact_root=str(config.asset_dir(request.asset_id)),
            )
        assets.append(outcome)

    summary = PackSummary(
        pack_id=pack.pack_id,
        pack_type=pack.pack_type,
        provider=config.provider,
        model=config.model,
        worker_count=config.max_concurrency,
        interrupted=control.stop.is_set(),
        interrupted_by=(
            signal.Signals(control.signal_number).name
            if control.signal_number is not None
            else None
        ),
        assets=assets,
    )
    summary.refresh_counts()
    atomic_write_json(_summary_path(config, pack.pack_id), summary.model_dump(mode="json"))
    return summary


def _has_cached_generation(
    item: _WorkItem,
    config: Config,
    provider: ImageProvider,
    cache: GenerationCache,
) -> bool:
    request = load_request(item.request_path)
    background = resolve_key_color(
        request.description,
        requested=request.background.color,
        fallbacks=request.background.fallback_colors,
        conflict_hint=request.background.conflict_hint,
        palette=request.style.palette_colors or (),
    )
    prompt = compile_static_prompt(request, key_color=background.color_used)
    key = provider.generate_key(prompt.text, prompt.size, config.model)
    entry = cache.get(key)
    return bool(
        entry is not None
        and entry.meta.get("provider") == config.provider
        and entry.meta.get("model") == config.model
    )


def _error_outcome(
    item: _WorkItem,
    config: Config,
    exc: Exception,
    *,
    provider_failed: bool,
) -> AssetOutcome:
    store = ArtifactStore.for_asset(config.output_dir, item.asset_id)
    table = store.load_job_table()
    status = "unknown"
    if table is not None:
        jobs = [job for job in table if job.id == f"{item.asset_id}:static"]
        if jobs:
            status = jobs[0].status.value
    return AssetOutcome(
        asset_id=item.asset_id,
        job_id=f"{item.asset_id}:static",
        input_fingerprint=item.fingerprint,
        provider=config.provider,
        model=config.model,
        outcome="provider_failed" if provider_failed else "processing_failed",
        job_status=status,
        stage=status,
        resumed=item.resumed,
        error_code=getattr(exc, "code", "unexpected_processing_error"),
        error=redact(str(exc)),
        request_id=getattr(exc, "request_id", None),
        artifact_root=str(store.root),
    )


def _run_one(
    item: _WorkItem,
    config: Config,
    provider: ImageProvider,
    cache: GenerationCache,
    control: PackRunControl,
    targets: list[str],
) -> AssetOutcome:
    store = ArtifactStore.for_asset(config.output_dir, item.asset_id)
    status, resumed, persisted_fingerprint = _job_state(config, item.asset_id)
    resumed = resumed or item.resumed

    if (
        persisted_fingerprint is not None
        and persisted_fingerprint != item.fingerprint
    ):
        return AssetOutcome(
            asset_id=item.asset_id,
            job_id=f"{item.asset_id}:static",
            input_fingerprint=item.fingerprint,
            provider=config.provider,
            model=config.model,
            outcome="processing_failed",
            job_status=status.value if status else "unknown",
            stage="planning",
            resumed=resumed,
            error_code="input_fingerprint_conflict",
            error=(
                "既有资产的输入指纹与本次 pack/provider/model 不一致；"
                "不会静默复用或覆盖原图"
            ),
            artifact_root=str(store.root),
        )

    if status is JobStatus.EXPORTED:
        table = store.load_job_table()
        jobs = table.of_kind(JobKind.STATIC) if table is not None else ()
        current = bool(jobs) and static_validation_binding(store, jobs[0])[0]
        if current:
            return AssetOutcome(
                asset_id=item.asset_id,
                job_id=f"{item.asset_id}:static",
                input_fingerprint=item.fingerprint,
                provider=config.provider,
                model=config.model,
                outcome="skipped",
                job_status="exported",
                stage="exported",
                resumed=True,
                skipped=True,
                artifact_root=str(store.root),
            )
    if status is JobStatus.VALIDATION_FAILED:
        return AssetOutcome(
            asset_id=item.asset_id,
            job_id=f"{item.asset_id}:static",
            input_fingerprint=item.fingerprint,
            provider=config.provider,
            model=config.model,
            outcome="validation_failed",
            job_status="validation_failed",
            stage="validation_failed",
            resumed=True,
            artifact_root=str(store.root),
        )
    if status is JobStatus.FAILED:
        return AssetOutcome(
            asset_id=item.asset_id,
            job_id=f"{item.asset_id}:static",
            input_fingerprint=item.fingerprint,
            provider=config.provider,
            model=config.model,
            outcome="processing_failed",
            job_status="failed",
            stage="failed",
            resumed=True,
            error_code="persisted_failure",
            error="任务已处于 failed；本次未自动重试",
            artifact_root=str(store.root),
        )
    cached_resume = False
    if status is JobStatus.GENERATING and not store.source_path("static").exists():
        cached_resume = _has_cached_generation(item, config, provider, cache)
        if not cached_resume:
            return AssetOutcome(
                asset_id=item.asset_id,
                job_id=f"{item.asset_id}:static",
                input_fingerprint=item.fingerprint,
                provider=config.provider,
                model=config.model,
                outcome="paused",
                job_status="generating",
                stage="generating",
                resumed=True,
                outcome_unknown=True,
                error_code="outcome_unknown",
                error="上次生成可能已计费，但 source/cache 未形成可确认检查点；未自动重试",
                artifact_root=str(store.root),
            )

    try:
        runnable = (
            None,
            JobStatus.PLANNED,
            JobStatus.GENERATING,
            JobStatus.GENERATED,
            JobStatus.PROCESSING,
        )
        if status in runnable:
            result: StaticAssetResult = create_static_asset(
                item.request_path,
                config,
                provider=provider,
                stop_requested=control.stop,
                allow_cached_resume=cached_resume,
            )
            cached = result.cached
            request_id = result.request_id
        else:
            cached = False
            request_id = None

        if control.stop.is_set():
            raise PauseRequested(f"{item.asset_id} 已停在阶段边界")

        completion = validate_and_export_static_asset(
            store.root,
            targets=targets,
            stop_requested=control.stop,
        )
        if not completion.passed:
            revalidated = status in (JobStatus.VALIDATED, JobStatus.EXPORTED)
            return AssetOutcome(
                asset_id=item.asset_id,
                job_id=f"{item.asset_id}:static",
                input_fingerprint=item.fingerprint,
                provider=config.provider,
                model=config.model,
                outcome="validation_failed",
                job_status="validation_failed",
                stage="validation_failed",
                cached=cached,
                resumed=resumed,
                error_code=(
                    "artifact_revalidation_failed" if revalidated else None
                ),
                error=(
                    "既有 validated/exported 成品的验证哈希绑定失效；重新验证未通过"
                    if revalidated
                    else None
                ),
                request_id=request_id,
                artifact_root=str(store.root),
            )
        return AssetOutcome(
            asset_id=item.asset_id,
            job_id=f"{item.asset_id}:static",
            input_fingerprint=item.fingerprint,
            provider=config.provider,
            model=config.model,
            outcome="exported",
            job_status="exported",
            stage=(
                "revalidated_exported"
                if status in (JobStatus.VALIDATED, JobStatus.EXPORTED)
                and completion.validation is not None
                else "exported"
            ),
            cached=cached,
            resumed=resumed,
            request_id=request_id,
            artifact_root=str(store.root),
        )
    except PauseRequested as exc:
        status, _, _ = _job_state(config, item.asset_id)
        return AssetOutcome(
            asset_id=item.asset_id,
            job_id=f"{item.asset_id}:static",
            input_fingerprint=item.fingerprint,
            provider=config.provider,
            model=config.model,
            outcome="paused",
            job_status=status.value if status else "planned",
            stage=status.value if status else "planned",
            resumed=resumed,
            error_code=exc.code,
            error=exc.message,
            artifact_root=str(store.root),
        )
    except ProviderError as exc:
        return _error_outcome(item, config, exc, provider_failed=True)
    except Exception as exc:
        return _error_outcome(item, config, exc, provider_failed=False)


async def run_asset_pack(
    pack_path: str | Path,
    config: Config,
    *,
    control: PackRunControl | None = None,
    retry_failed: bool = False,
) -> PackSummary:
    """并发执行一个静态资产 pack；每个 worker 拥有独立 client。"""
    pack = load_pack(pack_path)
    _require_saved_plans(pack, config, pack_path)
    run_control = control or PackRunControl()
    pack_dir = _pack_dir(config, pack.pack_id)
    pack_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(pack_dir / "request.yaml", Path(pack_path).read_text(encoding="utf-8"))

    shared_throttle = Throttle.from_rpm(config.max_concurrency, config.requests_per_minute)
    shared_cache = GenerationCache(config.cache_dir, enabled=config.cache_enabled)
    queue: asyncio.Queue[_WorkItem | None] = asyncio.Queue()
    outcomes: dict[str, AssetOutcome] = {}
    outcome_lock = asyncio.Lock()

    for request in pack.expand_requests():
        request_path = pack_dir / "requests" / f"{request.asset_id}.yaml"
        _write_expanded_request(request, request_path)
        reset_failed = retry_failed and _reset_failed_static_job(
            config, request.asset_id
        )
        _status, resumed, _persisted_fingerprint = _job_state(
            config, request.asset_id
        )
        await queue.put(
            _WorkItem(
                request_path=request_path,
                asset_id=request.asset_id,
                fingerprint=input_fingerprint(request, config.provider, config.model),
                resumed=resumed or reset_failed,
            )
        )

    worker_count = config.max_concurrency
    providers = [
        get_provider(config, cache=shared_cache, throttle=shared_throttle)
        for _ in range(worker_count)
    ]
    for _ in range(worker_count):
        await queue.put(None)

    async def worker(provider: ImageProvider) -> None:
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                if run_control.stop.is_set():
                    outcome = AssetOutcome(
                        asset_id=item.asset_id,
                        job_id=f"{item.asset_id}:static",
                        input_fingerprint=item.fingerprint,
                        provider=config.provider,
                        model=config.model,
                        outcome="paused",
                        job_status="planned",
                        stage="planned",
                        resumed=item.resumed,
                        error_code="pause_requested",
                        error="批次已暂停，资产尚未派发",
                        artifact_root=str(config.asset_dir(item.asset_id)),
                    )
                else:
                    outcome = await asyncio.to_thread(
                        _run_one,
                        item,
                        config,
                        provider,
                        shared_cache,
                        run_control,
                        list(pack.shared.export.targets),
                    )
                async with outcome_lock:
                    outcomes[item.asset_id] = outcome
                    try:
                        _persist_summary(pack, config, outcomes, control=run_control)
                    except Exception as exc:
                        logger.error(
                            "增量 pack-summary 写入失败，worker 将继续运行：%s",
                            redact(str(exc)),
                        )
            finally:
                queue.task_done()

    tasks = [asyncio.create_task(worker(provider)) for provider in providers]
    await queue.join()
    await asyncio.gather(*tasks)
    return _persist_summary(pack, config, outcomes, control=run_control)
