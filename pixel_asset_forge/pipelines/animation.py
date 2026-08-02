"""AnimationGridPipeline —— 一次生成完整动作网格。

```text
seed.png → 创建空白键控画布 → seed 作为参考图 → 生成完整动作网格
        → 越界检测 → 按比例切帧 → 对齐 → 缩小 → 透明化 → 预览
```

**绝不逐帧生成。** 逐帧调用会造成服装、武器、比例和朝向漂移 ——
而身份漂移是本项目最难解决的问题。一次生成整网格，模型至少在同一次
推理里看着同一个参考图。

镜像方向不调用 API：由源方向翻转 derive，身份一致性是 100% 的（ADR-006）。
"""

from __future__ import annotations

import threading
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..config import Config
from ..constants import MIRROR_PAIR, Direction
from ..errors import (
    PauseRequested,
    ProcessingError,
    ProviderError,
    RetryLimitExceededError,
)
from ..logging_utils import get_logger
from ..models.job import Job, JobEvent, JobKind, JobStatus, JobTable, make_job_id
from ..models.manifest import (
    AssetManifest,
    DerivedAnimation,
    GeneratedAnimation,
    GridInfo,
    MirroringInfo,
)
from ..models.request import AssetRequest, load_request
from ..planning.grid_layout import layout_for_frames
from ..processing.background import resolve_key_color
from ..processing.chroma_key import zero_transparent_rgb
from ..processing.pipeline import ProcessOptions, process_grid
from ..processing.spritesheet import compose_spritesheet, save_frames, save_gif, save_png
from ..prompts import compile_animation_prompt
from ..providers import ImageProvider, ReferenceImage, bypass_cache, get_provider
from ..storage.artifacts import ArtifactStore
from .character import seed_is_approved
from .common import (
    anchor_sheet,
    ensure_manifest,
    load_job_table,
    load_rgb,
    profile_from_manifest,
    record_generation,
    require_source_slot,
    store_profile,
)

logger = get_logger("pipeline.animation")


@dataclass
class AnimationResult:
    asset_id: str
    key: str
    frames: int
    frame_size: tuple[int, int]
    requested_size: tuple[int, int]
    actual_size: tuple[int, int]
    key_threshold: float
    anchor_drift: float
    overflow_clean: bool
    overflow_summary: str
    palette: list[str]
    derived_from: str | None
    cached: bool
    request_id: str | None
    warnings: list[str]

    @property
    def calls_api(self) -> bool:
        return self.derived_from is None


def create_animation(
    asset_dir: str | Path,
    *,
    action: str,
    direction: Direction | None,
    config: Config,
    provider: ImageProvider | None = None,
    regenerate: bool = False,
    stop_requested: threading.Event | None = None,
) -> AnimationResult:
    """生成一个 ``(action, direction)`` 的动作网格。"""
    store = ArtifactStore(root=Path(asset_dir)).ensure()
    if not store.request_path.exists():
        raise ProcessingError(f"{asset_dir} 下没有 request.yaml —— 先跑 create-character")

    request = load_request(store.request_path)
    table = load_job_table(store, request)

    # -- 人工闸门 ---------------------------------------------------------
    if not seed_is_approved(store):
        raise ProcessingError(
            "canonical seed 尚未获批准。seed 是所有动画的身份基准，"
            "它不对则后续动画全部作废重来。请先查看 seed-pixel.png，"
            "确认后运行 `pixel-asset create-animation ... --approve-seed`。"
        )

    spec = next((a for a in request.animation_list() if a.name == action), None)
    if spec is None:
        available = ", ".join(a.name for a in request.animation_list()) or "（无）"
        raise ProcessingError(f"request 里没有动作 {action!r}。已声明的动作：{available}")

    job_id = make_job_id(request.asset_id, JobKind.ANIMATION, action, direction)
    if job_id not in table:
        raise ProcessingError(
            f"任务表里没有 {job_id} —— 该 (动作, 方向) 组合不在请求范围内"
        )
    job = table.get(job_id)
    key = job.key

    background = resolve_key_color(
        request.description,
        requested=request.background.color,
        fallbacks=request.background.fallback_colors,
        conflict_hint=request.background.conflict_hint,
        palette=request.style.palette_colors or (),
    )
    warnings: list[str] = []
    active_provider = provider
    if active_provider is not None and (
        active_provider.name != config.provider or active_provider.model != config.model
    ):
        raise ProcessingError(
            "Provider 与有效配置不一致："
            f"{active_provider.name}/{active_provider.model} != "
            f"{config.provider}/{config.model}"
        )
    manifest = ensure_manifest(
        store, request, background, provider_name=config.provider, model=config.model
    )

    if job.kind is JobKind.DERIVED:
        return _derive(store, manifest, table, job, request, warnings)

    # -- 生成 -------------------------------------------------------------
    layout = layout_for_frames(spec.frames)
    prompt = compile_animation_prompt(
        request,
        action=action,
        direction=direction,
        frames=spec.frames,
        layout=layout,
        key_color=background.color_used,
    )

    source_path = store.source_path(key)
    if regenerate:
        require_source_slot(store, key, regenerate=True)
        job.status = JobStatus.PLANNED
        job.attempts = 0
        job.error = None
        store.save_job_table(table)
    elif job.status not in (
        JobStatus.GENERATING,
        JobStatus.GENERATED,
        JobStatus.PROCESSING,
    ):
        require_source_slot(store, key, regenerate=False)
        if job.status is not JobStatus.PLANNED:
            job.status = JobStatus.PLANNED

    seed_bytes = store.source_path("seed").read_bytes()
    result = None
    if job.status is JobStatus.PLANNED:
        active_provider = active_provider or get_provider(config)
        job.fire(JobEvent.START_EXECUTION)
        store.save_job_table(table)
        try:
            # 重生成必须绕开缓存：命中会原样返回那张不合格的图，修复永远不可能成功。
            with bypass_cache(active_provider) if regenerate else nullcontext():
                # base image 是 anchor sheet 而不是空白画布：每格先摆好一个姿态正确、
                # 大小正确、脚线正确的角色，模型只需改姿势。纯文字压不住的镜像翻转、
                # 体型漂移、脚线漂移，靠这张图压得住（agent-sprite-forge 的手法）。
                result = active_provider.edit(
                    prompt.text,
                    base_image=anchor_sheet(
                        load_rgb(store.source_path("seed")),
                        prompt.size,
                        layout,
                        background.color_used,
                    ),
                    size=prompt.size,
                    references=[ReferenceImage("seed", seed_bytes)],
                )
        except ProviderError as exc:
            event = (
                JobEvent.RETRY_LIMIT_EXCEEDED
                if isinstance(exc, RetryLimitExceededError)
                else JobEvent.PERMANENT_ERROR
            )
            if job.can(event):
                job.fire(event, detail=exc.message)
                store.save_job_table(table)
            raise

        store.write_source(key, result.image)
        record_generation(store, job, result, prompt_chars=len(prompt.text))
        job.fire(JobEvent.PROVIDER_SUCCESS)
        store.save_job_table(table)
        if result.size_snapped:
            warnings.append(
                f"端点把请求的 {result.requested_size[0]}×{result.requested_size[1]} 改成了 "
                f"{result.actual_size[0]}×{result.actual_size[1]}；切帧按实际尺寸比例进行"
            )
    elif job.status is JobStatus.GENERATING:
        if not source_path.exists():
            raise ProcessingError(
                f"{job.id} 停在 generating 且没有原图；上次调用结果未知，需恢复协调器对账"
            )
        job.fire(JobEvent.PROVIDER_SUCCESS, detail="从已落盘原图恢复")
        store.save_job_table(table)
    elif job.status is JobStatus.PROCESSING:
        job.fire(JobEvent.RECOVER_GENERATED, detail="从不可变原图幂等重跑处理")
        store.save_job_table(table)

    if stop_requested is not None and stop_requested.is_set():
        raise PauseRequested(f"{job.id} 已保存原图，停在 generated 阶段")

    if job.status is not JobStatus.GENERATED:
        raise ProcessingError(
            f"{job.id} 当前状态为 {job.status.value}，不能进入动画处理"
        )

    # -- 处理链 -----------------------------------------------------------
    try:
        job.fire(JobEvent.START_PROCESSING)
        store.save_job_table(table)
        profile = profile_from_manifest(manifest)
        processed = process_grid(
            load_rgb(source_path),
            layout,
            ProcessOptions(
                key_color=background.color_used,
                target_size=request.style.target_size,
                max_colors=request.style.max_colors,
                scale_profile=profile,
                palette=(
                    list(request.style.palette_colors)
                    if request.style.palette_colors is not None
                    else None
                ),
            ),
        )
        warnings.extend(processed.warnings)

    # 基准取幅度最大的动作。增量生成看不到未来，只能边走边顶替。
    #
    # 顶替时要拿**不带基准**的那次处理结果去推导，不能拿上面这次。
    # 上面这次是在前一任基准下缩过的，用它推出来的 canvas_fraction 是循环产物 ——
    # 实测 hurt 顶替成参考后记下 0.427，于是全部动作都按"参考只占画布 43%"缩，
    # 整个资产小了一圈。
        reference_result = processed
        if profile is not None:
            reference_result = process_grid(
                load_rgb(source_path),
                layout,
                ProcessOptions(
                    key_color=background.color_used,
                    target_size=request.style.target_size,
                    max_colors=request.style.max_colors,
                    palette=(
                        list(request.style.palette_colors)
                        if request.style.palette_colors is not None
                        else None
                    ),
                ),
            )
        _profile, superseded = store_profile(manifest, key, reference_result)
        if profile is None:
            warnings.append(f"{key} 已确立跨动作缩放基准，后续动作将复用其比例")
        elif superseded:
            warnings.append(
                f"{key} 比原参考动作 {profile.reference} 幅度更大，已顶替为新基准 —— "
                f"此前生成的动作还是按旧基准出的图，跑一次 `pixel-asset process` "
                f"让全部动作按新基准重出（不花 API 调用）"
            )

        frame_paths = save_frames(processed.frames, store.frames_of(key), stem=key)
        sheet, _ = compose_spritesheet(processed.frames)
        sheet_path = save_png(sheet, store.sheets / f"{key}.png")
        _save_preview(processed.frames, store, key, spec.fps, spec.loop)
        job.fire(JobEvent.PROCESSING_DONE)

        manifest.animations[key] = GeneratedAnimation(
            fps=spec.fps,
            loop=spec.loop,
            grid=GridInfo(
                cols=layout.cols,
                rows=layout.rows,
                cell=layout.actual_cell(processed.source_size),
                requested_size=layout.size,
                actual_size=processed.source_size,
            ),
            source_image=str(source_path.relative_to(store.root)),
            key_threshold=processed.key_threshold,
            frames=[str(p.relative_to(store.root)) for p in frame_paths],
        )
        manifest.sheets[key] = str(sheet_path.relative_to(store.root))
        manifest.palette.colors = processed.palette.colors
        manifest.mirroring = MirroringInfo(
            enabled=request.mirroring_enabled,
            source_direction=request.mirroring.source_direction if request.mirroring else None,
            strict_lighting=request.style.strict_lighting,
        )
        manifest.status = "processed"
        manifest.save(store.manifest_path)
        store.save_job_table(table)
    except Exception as exc:
        if job.can(JobEvent.PROCESSING_ERROR):
            job.fire(JobEvent.PROCESSING_ERROR, detail=str(exc))
            store.save_job_table(table)
        raise

    if stop_requested is not None and stop_requested.is_set():
        raise PauseRequested(f"{job.id} 已保存动作成品，停在 processed 阶段")

    return AnimationResult(
        asset_id=request.asset_id,
        key=key,
        frames=len(processed.frames),
        frame_size=processed.frame_size,
        requested_size=result.requested_size if result else prompt.size,
        actual_size=result.actual_size if result else processed.source_size,
        key_threshold=processed.key_threshold,
        anchor_drift=processed.anchor_drift_px,
        overflow_clean=processed.overflow.clean,
        overflow_summary=processed.overflow.summary(),
        palette=processed.palette.colors,
        derived_from=None,
        cached=result.cached if result else False,
        request_id=result.request_id if result else job.request_id,
        warnings=warnings,
    )


def _derive(
    store: ArtifactStore,
    manifest: AssetManifest,
    table: JobTable,
    job: Job,
    request: AssetRequest,
    warnings: list[str],
) -> AnimationResult:
    """镜像派生：翻转源方向的成品帧。**不调用 API。**

    镜像方向的身份一致性是 100% 的 —— 服装、武器、比例、配色与源方向
    完全相同，不存在任何漂移。代价是左上角光源被翻到右上角，
    这在 32×32 下几乎不可察觉（ADR-006）。
    """
    source_key = job.derived_from
    if not source_key:  # pragma: no cover - planner 一定会填
        raise ProcessingError(f"{job.id} 是派生任务却没有 derived_from")

    source_dir = store.frames_of(source_key)
    source_frames = sorted(source_dir.glob("*.png"))
    if not source_frames:
        raise ProcessingError(
            f"源方向 {source_key} 还没有成品帧 —— 先生成它，再 derive {job.key}"
        )

    from PIL import Image

    flipped = []
    for path in source_frames:
        arr = np.array(Image.open(path).convert("RGBA"))
        flipped.append(zero_transparent_rgb(np.ascontiguousarray(arr[:, ::-1])))

    job.fire(JobEvent.DERIVE_READY)
    job.fire(JobEvent.START_PROCESSING)

    save_frames(flipped, store.frames_of(job.key), stem=job.key)
    sheet, _ = compose_spritesheet(flipped)
    sheet_path = save_png(sheet, store.sheets / f"{job.key}.png")

    source_entry = manifest.animations.get(source_key)
    fps = getattr(source_entry, "fps", 10)
    loop = getattr(source_entry, "loop", True)
    _save_preview(flipped, store, job.key, fps, loop)
    job.fire(JobEvent.PROCESSING_DONE)

    manifest.animations[job.key] = DerivedAnimation(
        derived_from=source_key, transform="flip_horizontal"
    )
    manifest.sheets[job.key] = str(sheet_path.relative_to(store.root))
    manifest.save(store.manifest_path)
    store.save_job_table(table)

    height, width = flipped[0].shape[:2]
    return AnimationResult(
        asset_id=request.asset_id,
        key=job.key,
        frames=len(flipped),
        frame_size=(width, height),
        requested_size=(0, 0),
        actual_size=(0, 0),
        key_threshold=0.0,
        anchor_drift=0.0,
        overflow_clean=True,
        overflow_summary="镜像派生，不适用",
        palette=list(manifest.palette.colors),
        derived_from=source_key,
        cached=False,
        request_id=None,
        warnings=[*warnings, f"{job.key} 由 {source_key} 翻转派生，未调用 API"],
    )


def _save_preview(
    frames: list[np.ndarray], store: ArtifactStore, key: str, fps: int, loop: bool
) -> None:
    try:
        save_gif(frames, store.previews / f"{key}.gif", fps=fps, loop=loop)
    except Exception as exc:
        logger.warning("生成 %s 预览 GIF 失败：%s", key, exc)


def next_pending(store: ArtifactStore) -> list[str]:
    """列出依赖已满足、可以立即执行的动画任务。"""
    table = store.load_job_table()
    if table is None:
        return []
    return [job.id for job in table.ready_jobs() if job.kind is not JobKind.SEED]


def mirror_target_of(request: AssetRequest, direction: Direction) -> Direction | None:
    if not request.mirroring_enabled:
        return None
    return MIRROR_PAIR.get(direction)
