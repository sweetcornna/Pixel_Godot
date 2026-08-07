"""无动画静态资产流水线。"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from ..errors import PauseRequested, ProcessingError, ProviderError, RetryLimitExceededError
from ..models.job import Job, JobEvent, JobKind, JobStatus, JobTable
from ..models.manifest import AnchorInfo, StaticImageInfo
from ..models.request import STATIC_ASSET_TYPES, AssetRequest, load_request
from ..models.validation import ValidationReport
from ..planning.grid_layout import GridLayout
from ..planning.planner import plan_request
from ..processing.anchor import CENTER, place_on_canvas
from ..processing.background import resolve_key_color
from ..processing.pipeline import ProcessOptions, process_grid
from ..processing.spritesheet import save_png
from ..prompts import compile_static_prompt
from ..providers import ImageProvider, bypass_cache, get_provider
from ..storage.artifacts import ArtifactStore
from ..storage.hashes import hash_file
from .common import ensure_manifest, load_rgb, record_generation, require_source_slot
from .export import ExportSummary, run_export
from .validation import run_validation, static_validation_binding


@dataclass
class StaticAssetResult:
    asset_id: str
    source_path: Path
    image_path: Path
    requested_size: tuple[int, int]
    actual_size: tuple[int, int]
    key_color: str
    key_threshold: float
    grid_block_size: float | None
    palette: list[str]
    cached: bool
    request_id: str | None
    warnings: list[str]


@dataclass
class StaticAssetCompletion:
    validation: ValidationReport | None
    export: ExportSummary | None

    @property
    def passed(self) -> bool:
        return self.validation is None or self.validation.passed


def _load_table(
    store: ArtifactStore,
    request: AssetRequest,
    config: Config,
) -> JobTable:
    return plan_request(
        request,
        existing=store.load_job_table(),
        provider=config.provider,
        model=config.model,
    ).jobs


def _static_job(table: JobTable) -> Job:
    jobs = table.of_kind(JobKind.STATIC)
    if len(jobs) != 1:
        raise ProcessingError(
            f"{table.asset_id} 的任务表应有且仅有一个 static 任务，实际 {len(jobs)} 个"
        )
    return jobs[0]


def validate_and_export_static_asset(
    asset_dir: str | Path,
    *,
    targets: Sequence[str],
    stop_requested: threading.Event | None = None,
) -> StaticAssetCompletion:
    """验证并导出已处理的静态资产，供单资产与 pack 协调器共用。"""
    store = ArtifactStore(root=Path(asset_dir))
    table = store.load_job_table()
    if table is None:
        raise ProcessingError(f"{asset_dir} 下没有任务表 —— 先完成静态资产生成与处理")
    job = _static_job(table)

    validation: ValidationReport | None = None
    if job.status in (
        JobStatus.PROCESSED,
        JobStatus.VALIDATING,
        JobStatus.VALIDATION_FAILED,
    ):
        validation = run_validation(store.root)
        if not validation.passed:
            return StaticAssetCompletion(validation=validation, export=None)
    elif job.status in (JobStatus.VALIDATED, JobStatus.EXPORTED):
        current, _reason = static_validation_binding(store, job)
        if not current:
            validation = run_validation(store.root)
            if not validation.passed:
                return StaticAssetCompletion(validation=validation, export=None)
    else:
        raise ProcessingError(
            f"{job.id} 当前状态为 {job.status.value}，不能进入静态验证与导出"
        )

    if stop_requested is not None and stop_requested.is_set():
        raise PauseRequested(f"{job.id} 已验证并停在阶段边界")

    exported = run_export(store.root, targets=list(targets))
    return StaticAssetCompletion(validation=validation, export=exported)


def _mark_provider_failed(
    store: ArtifactStore,
    table: JobTable,
    job: Job,
    exc: ProviderError,
) -> None:
    event = (
        JobEvent.RETRY_LIMIT_EXCEEDED
        if isinstance(exc, RetryLimitExceededError)
        else JobEvent.PERMANENT_ERROR
    )
    if job.can(event):
        job.fire(event, detail=exc.message)
        store.save_job_table(table)


def _mark_processing_failed(
    store: ArtifactStore,
    table: JobTable,
    job: Job,
    exc: Exception,
) -> None:
    if job.can(JobEvent.PROCESSING_ERROR):
        job.fire(JobEvent.PROCESSING_ERROR, detail=str(exc))
        store.save_job_table(table)


def create_static_asset(
    request_path: str | Path,
    config: Config,
    *,
    provider: ImageProvider | None = None,
    regenerate: bool = False,
    stop_requested: threading.Event | None = None,
    allow_cached_resume: bool = False,
) -> StaticAssetResult:
    """生成并处理一个无动画静态资产，停在 ``processed`` 等待真实验证。"""
    request = load_request(request_path)
    if request.asset_type not in STATIC_ASSET_TYPES or request.animation_list():
        allowed = ", ".join(sorted(STATIC_ASSET_TYPES))
        raise ProcessingError(
            f"静态流水线只接受无 animations 的资产类型：{allowed}"
        )

    store = ArtifactStore.for_asset(config.output_dir, request.asset_id).ensure()
    store.save_request_copy(request_path)
    table = _load_table(store, request, config)
    job = _static_job(table)

    background = resolve_key_color(
        request.description,
        requested=request.background.color,
        fallbacks=request.background.fallback_colors,
        conflict_hint=request.background.conflict_hint,
        palette=request.style.palette_colors or (),
    )
    warnings: list[str] = []
    if background.downgraded:
        warnings.append(background.explain())

    active_provider = provider
    if active_provider is not None and (
        active_provider.name != config.provider or active_provider.model != config.model
    ):
        raise ProcessingError(
            "Provider 与有效配置不一致："
            f"{active_provider.name}/{active_provider.model} != "
            f"{config.provider}/{config.model}"
        )

    prompt = compile_static_prompt(request, key_color=background.color_used)
    source_path = store.source_path("static")
    result = None

    def should_stop() -> bool:
        return stop_requested is not None and stop_requested.is_set()

    if regenerate:
        require_source_slot(store, "static", regenerate=True)
        job.status = JobStatus.PLANNED
        job.attempts = 0
        job.error = None

    if job.status is JobStatus.PLANNED:
        active_provider = active_provider or get_provider(config)
        job.fire(JobEvent.START_EXECUTION)
        store.save_job_table(table)
        try:
            with bypass_cache(active_provider) if regenerate else nullcontext():
                result = active_provider.generate(prompt.text, size=prompt.size)
        except ProviderError as exc:
            _mark_provider_failed(store, table, job, exc)
            raise

        if result.provider != config.provider or result.model != config.model:
            raise ProcessingError(
                "生成结果与有效配置不一致："
                f"{result.provider}/{result.model} != {config.provider}/{config.model}"
            )
        store.write_source("static", result.image)
        record_generation(store, job, result, prompt_chars=len(prompt.text))
        job.fire(JobEvent.PROVIDER_SUCCESS)
        store.save_job_table(table)
        if should_stop():
            raise PauseRequested(f"{job.id} 已保存原图，停在 generated 阶段")
    elif job.status is JobStatus.GENERATING:
        if not source_path.exists():
            if not allow_cached_resume:
                raise ProcessingError(
                    f"{job.id} 停在 generating 且没有原图；上次调用结果未知，需恢复协调器对账"
                )
            active_provider = active_provider or get_provider(config)
            result = active_provider.generate(prompt.text, size=prompt.size)
            if not result.cached:
                raise ProcessingError(
                    f"{job.id} 的恢复调用未命中 cache；为避免不可判定重复计费，拒绝提交"
                )
            store.write_source("static", result.image)
            record_generation(store, job, result, prompt_chars=len(prompt.text))
        job.fire(JobEvent.PROVIDER_SUCCESS, detail="从已落盘原图或缓存恢复")
        store.save_job_table(table)
        if should_stop():
            raise PauseRequested(f"{job.id} 已从原图恢复到 generated 阶段")

    if job.status is JobStatus.PROCESSING:
        job.fire(JobEvent.RECOVER_GENERATED, detail="从不可变原图幂等重跑处理")
        store.save_job_table(table)
    if job.status is JobStatus.PROCESSED:
        manifest = ensure_manifest(
            store,
            request,
            background,
            provider_name=config.provider,
            model=config.model,
        )
        if manifest.static_image is None:
            raise ProcessingError(f"{job.id} 为 processed，但 Manifest 没有 static_image")
        image_path = store.root / manifest.static_image.image
        if not image_path.exists():
            raise ProcessingError(f"{job.id} 为 processed，但成品缺失：{image_path}")
        return StaticAssetResult(
            asset_id=request.asset_id,
            source_path=source_path,
            image_path=image_path,
            requested_size=manifest.static_image.requested_size,
            actual_size=manifest.static_image.actual_size,
            key_color=background.color_used,
            key_threshold=manifest.static_image.key_threshold,
            grid_block_size=manifest.static_image.grid_block_size,
            palette=manifest.palette.colors,
            cached=False,
            request_id=job.request_id,
            warnings=warnings,
        )
    if job.status is not JobStatus.GENERATED:
        raise ProcessingError(
            f"{job.id} 当前状态为 {job.status.value}，不能进入静态处理"
        )

    try:
        job.fire(JobEvent.START_PROCESSING)
        store.save_job_table(table)
        image = load_rgb(source_path)
        layout = GridLayout(
            frames=1,
            cols=1,
            rows=1,
            cell=(image.shape[1], image.shape[0]),
        )
        final_size = request.style.target_size
        # 这里曾经缩到 ``边长 - 2`` 再居中放回，等于恒留 1px 边距 ——
        # **那把主体最长边锁死在 (边长-2)/边长 以下**（24px 画布上 0.917）。
        # 参照是 Kenney Pixel Platformer 的 27 张独立精灵（CC0）：最长边中位 0.96、
        # 21/27 张直接顶到画布边缘。内缩让我们画不出那种铺满构图。
        #
        # 2026-08-07 拿真实产出做 A/B（6 张 gpt-image-2 原图，只改这一行离线重跑，
        # 生成层零差异）：
        #
        #     最长边占画布   内缩 0.833 – 0.917（上限顶死）→ 拆掉 0.917 – 1.000
        #     不透明占比     +0.05 ~ +0.10
        #     验证           两组各 6 张全过，无一项变红
        #
        # 不裁切的保证并不来自这个内缩，而来自等比缩放本身：
        # ``fit = min(画布宽/内容宽, 画布高/内容高)`` 保证内容完整落进画布，
        # 顶边只意味着"刚好铺满"，不意味着被切掉（见 validation/engine.py 里
        # content_bounds 那段）。所以拆掉它不放过任何裁切。
        processed = process_grid(
            image,
            layout,
            ProcessOptions(
                key_color=background.color_used,
                target_size=final_size,
                max_colors=request.style.max_colors,
                anchor=CENTER,
                crop_padding=1,
                palette=list(request.style.palette_colors)
                if request.style.palette_colors is not None
                else None,
            ),
        )
        final_frame = place_on_canvas(processed.frames[0], final_size, anchor=CENTER)
        image_path = save_png(final_frame, store.frames / "static.png")

        manifest = ensure_manifest(
            store,
            request,
            background,
            provider_name=config.provider,
            model=config.model,
        )
        if (
            manifest.provider.name != config.provider
            or manifest.provider.model != config.model
        ):
            raise ProcessingError(
                "既有 Manifest 与有效 Provider 不一致："
                f"{manifest.provider.name}/{manifest.provider.model} != "
                f"{config.provider}/{config.model}"
            )
        manifest.anchor = AnchorInfo(type="center", x=0.5, y=0.5)
        manifest.palette.colors = (
            list(request.style.palette_colors)
            if request.style.palette_colors is not None
            else processed.palette.colors
        )
        manifest.static_image = StaticImageInfo(
            source_image=str(source_path.relative_to(store.root)),
            image=str(image_path.relative_to(store.root)),
            requested_size=result.requested_size if result else prompt.size,
            actual_size=result.actual_size if result else (image.shape[1], image.shape[0]),
            key_threshold=processed.key_threshold,
            grid_block_size=(
                processed.grid_snap.block_size
                if processed.grid_snap is not None and processed.grid_snap.applied
                else None
            ),
            source_hash=hash_file(source_path),
            processed_hash=hash_file(image_path),
        )
        manifest.status = "processed"
        manifest.save(store.manifest_path)

        job.fire(JobEvent.PROCESSING_DONE)
        store.save_job_table(table)
        if should_stop():
            raise PauseRequested(f"{job.id} 已保存成品，停在 processed 阶段")
    except PauseRequested:
        raise
    except Exception as exc:
        _mark_processing_failed(store, table, job, exc)
        raise

    warnings.extend(processed.warnings)
    return StaticAssetResult(
        asset_id=request.asset_id,
        source_path=source_path,
        image_path=image_path,
        requested_size=result.requested_size if result else prompt.size,
        actual_size=result.actual_size if result else (image.shape[1], image.shape[0]),
        key_color=background.color_used,
        key_threshold=processed.key_threshold,
        grid_block_size=(
            processed.grid_snap.block_size
            if processed.grid_snap is not None and processed.grid_snap.applied
            else None
        ),
        palette=manifest.palette.colors,
        cached=result.cached if result else True,
        request_id=result.request_id if result else job.request_id,
        warnings=warnings,
    )
