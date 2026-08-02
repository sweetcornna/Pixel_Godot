"""CharacterSeedPipeline —— 产出 canonical seed。

```text
角色描述 → Prompt Compiler → 生成 → 背景冲突预检 → 去背景
        → 自动裁剪 → 统一构图 → 调色板量化 → seed.png
```

产出之后任务停在 ``awaiting_approval``：**seed 是所有动画的身份基准，
它不对则后续生成的全部动画都要作废重来。** 这是整条流水线里唯一的人工闸门，
不要自作主张跳过。
"""

from __future__ import annotations

import io
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from ..errors import PauseRequested, ProviderError, RetryLimitExceededError
from ..logging_utils import get_logger
from ..models.job import JobEvent, JobKind, JobStatus
from ..models.manifest import AssetManifest
from ..models.request import load_request
from ..planning.grid_layout import GridLayout
from ..processing.background import resolve_key_color
from ..processing.chroma_key import hex_to_rgb
from ..processing.pipeline import ProcessOptions, process_grid
from ..processing.spritesheet import save_png
from ..prompts import compile_seed_prompt
from ..providers import ImageProvider, bypass_cache, get_provider
from ..storage.artifacts import ArtifactStore
from .common import (
    ensure_manifest,
    load_job_table,
    load_rgb,
    record_generation,
    require_source_slot,
)

logger = get_logger("pipeline.character")


@dataclass
class SeedResult:
    asset_id: str
    seed_path: Path
    pixel_path: Path
    requested_size: tuple[int, int]
    actual_size: tuple[int, int]
    key_color: str
    key_threshold: float
    background_ratio: float
    palette: list[str]
    cached: bool
    request_id: str | None
    warnings: list[str]

    @property
    def size_snapped(self) -> bool:
        return self.requested_size != self.actual_size


def create_character(
    request_path: str | Path,
    config: Config,
    *,
    provider: ImageProvider | None = None,
    regenerate: bool = False,
    stop_requested: threading.Event | None = None,
) -> SeedResult:
    """生成 canonical seed 并停在人工闸门前。"""
    request = load_request(request_path)
    store = ArtifactStore.for_asset(config.output_dir, request.asset_id).ensure()
    store.save_request_copy(request_path)

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

    table = load_job_table(store, request)
    job = table.seed_job
    if job is None:  # pragma: no cover - planner 一定会建 seed 任务
        raise RuntimeError(f"{request.asset_id} 的任务表里没有 seed 任务")

    prompt = compile_seed_prompt(request, key_color=background.color_used)
    active_provider = provider
    if active_provider is not None and (
        active_provider.name != config.provider or active_provider.model != config.model
    ):
        from ..errors import ProcessingError

        raise ProcessingError(
            "Provider 与有效配置不一致："
            f"{active_provider.name}/{active_provider.model} != "
            f"{config.provider}/{config.model}"
        )

    if not regenerate and job.status in (
        JobStatus.AWAITING_APPROVAL,
        JobStatus.APPROVED,
    ):
        require_source_slot(store, "seed", regenerate=False)

    if regenerate:
        require_source_slot(store, "seed", regenerate=True)
        job.status = JobStatus.PLANNED
        table.cascade_invalidate(job.id, reason="seed 重新生成")
        job.attempts = 0
        job.error = None
        store.save_job_table(table)

    result = None
    source_path = store.source_path("seed")
    if job.status is JobStatus.PLANNED:
        require_source_slot(store, "seed", regenerate=False)
        active_provider = active_provider or get_provider(config)
        job.fire(JobEvent.START_EXECUTION)
        store.save_job_table(table)
        try:
            # 重生成必须绕开缓存，理由同 animation.py。
            with bypass_cache(active_provider) if regenerate else nullcontext():
                result = active_provider.generate(prompt.text, size=prompt.size)
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

        store.write_source("seed", result.image)
        record_generation(store, job, result, prompt_chars=len(prompt.text))
        job.fire(JobEvent.PROVIDER_SUCCESS)
        store.save_job_table(table)
        if result.size_snapped:
            warnings.append(
                f"端点把请求的 {result.requested_size[0]}×{result.requested_size[1]} 改成了 "
                f"{result.actual_size[0]}×{result.actual_size[1]}（已知行为，见 Sprint 0 A-1）"
            )
    elif job.status is JobStatus.GENERATING:
        if not source_path.exists():
            from ..errors import ProcessingError

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

    if job.status not in (
        JobStatus.GENERATED,
        JobStatus.PROCESSED,
        JobStatus.VALIDATING,
        JobStatus.VALIDATED,
    ):
        from ..errors import ProcessingError

        raise ProcessingError(
            f"{job.id} 当前状态为 {job.status.value}，不能进入 seed 处理与审批"
        )

    # -- 本地处理链 --------------------------------------------------------
    try:
        started_processing = job.status is JobStatus.GENERATED
        if started_processing:
            job.fire(JobEvent.START_PROCESSING)
            store.save_job_table(table)
        image = load_rgb(source_path)
        layout = GridLayout(frames=1, cols=1, rows=1, cell=(image.shape[1], image.shape[0]))
        processed = process_grid(
            image,
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
        warnings.extend(processed.warnings)

        pixel_path = save_png(processed.frames[0], store.root / "seed-pixel.png")
        if started_processing:
            job.fire(JobEvent.PROCESSING_DONE)
            store.save_job_table(table)
    except Exception as exc:
        if job.can(JobEvent.PROCESSING_ERROR):
            job.fire(JobEvent.PROCESSING_ERROR, detail=str(exc))
            store.save_job_table(table)
        raise

    if stop_requested is not None and stop_requested.is_set():
        raise PauseRequested(f"{job.id} 已保存 seed 成品，停在 processed 阶段")

    # Sprint 5 的验证引擎还没有；这里只做处理层自己能保证的检查。
    if job.status is JobStatus.PROCESSED:
        job.fire(JobEvent.START_VALIDATION)
        store.save_job_table(table)
    if job.status is JobStatus.VALIDATING:
        job.fire(JobEvent.VALIDATION_PASSED)
        store.save_job_table(table)
    if job.status is JobStatus.VALIDATED:
        job.fire(JobEvent.REQUIRE_APPROVAL)
        store.save_job_table(table)

    active_provider = active_provider or get_provider(config)
    manifest = ensure_manifest(
        store,
        request,
        background,
        provider_name=active_provider.name,
        model=active_provider.model,
    )
    # **seed 不确立跨动作缩放基准。**
    #
    # 直觉上 canonical seed 该当参考，但它的构图约定与动作网格不同：
    # seed 是整幅 1×1 画布、prompt 要求四周留 10% 边距，动作格子则紧凑得多。
    # 实测 seed 的 subject_ratio 是 0.57 而 walk 是 0.80 —— 两者不可比，
    # 拿 seed 当参考会把所有动作都推到画布外再被钳回来，等于没做。
    # 基准由动作之间自己确立，见 pipelines/process.py。
    manifest.background.key_threshold = processed.key_threshold
    manifest.palette.colors = processed.palette.colors
    manifest.status = "awaiting_approval"
    manifest.save(store.manifest_path)

    return SeedResult(
        asset_id=request.asset_id,
        seed_path=store.source_path("seed"),
        pixel_path=pixel_path,
        requested_size=result.requested_size if result else prompt.size,
        actual_size=result.actual_size if result else (image.shape[1], image.shape[0]),
        key_color=background.color_used,
        key_threshold=processed.key_threshold,
        background_ratio=processed.background_ratio,
        palette=processed.palette.colors,
        cached=result.cached if result else False,
        request_id=result.request_id if result else job.request_id,
        warnings=warnings,
    )


def approve_seed(asset_dir: str | Path) -> tuple[str, int]:
    """记录人工批准，解锁下游动画任务。

    返回 ``(asset_id, 解锁的任务数)``。

    批准是一个**显式的、留痕的动作** —— 状态转移写进 job history，
    因为"seed 是谁看过、什么时候放行的"是出问题时第一个要查的东西。
    """
    from ..errors import ProcessingError

    store = ArtifactStore(root=Path(asset_dir))
    table = store.load_job_table()
    if table is None:
        raise ProcessingError(f"{asset_dir} 下没有任务表 —— 先跑 create-character")

    job = table.seed_job
    if job is None:
        raise ProcessingError(f"{asset_dir} 的任务表里没有 seed 任务")

    if job.status is JobStatus.APPROVED:
        return (table.asset_id, len(table.ready_jobs()))

    if job.status is not JobStatus.AWAITING_APPROVAL:
        raise ProcessingError(
            f"seed 当前状态是 {job.status.value}，不在等待批准。"
            f"先跑 create-character 产出 seed。"
        )

    job.fire(JobEvent.APPROVE, detail="人工闸门放行")
    store.save_job_table(table)

    if store.manifest_path.exists():
        manifest = AssetManifest.load(store.manifest_path)
        manifest.status = "approved"
        manifest.save(store.manifest_path)

    unlocked = [j for j in table.ready_jobs() if j.kind is not JobKind.SEED]
    return (table.asset_id, len(unlocked))


def seed_is_approved(store: ArtifactStore) -> bool:
    table = store.load_job_table()
    if table is None:
        return False
    job = table.seed_job
    return job is not None and job.status is JobStatus.APPROVED


@dataclass
class ImportResult:
    asset_id: str
    seed_path: Path
    pixel_path: Path
    source_size: tuple[int, int]
    had_alpha: bool
    key_color: str
    palette: list[str]
    warnings: list[str]


def import_seed(
    request_path: str | Path,
    image_path: str | Path,
    config: Config,
    *,
    replace: bool = False,
) -> ImportResult:
    """把**用户自己的**像素资产导入成 canonical seed。不调用 API。

    存在的理由：用户已经有角色了，只是缺动画。让他们先花一次生成去换一张
    "风格接近但不是同一个角色"的 seed，既费钱又把身份基准换掉了。

    两处与生成路径不同，都不能省：

    - **透明背景要合成到键控色上。** 整条处理链的前提是"背景是一片纯键控色"，
      而用户导出的素材通常带 alpha。不合成的话去背景那一步无事可做，
      后面的连通域抽帧、despill 全部落空。
    - **不写生成日志。** 没有 prompt、没有 request_id，硬凑一条只会让
      溯源记录里出现查无此物的调用。
    """
    from PIL import Image

    request = load_request(request_path)
    store = ArtifactStore.for_asset(config.output_dir, request.asset_id).ensure()
    store.save_request_copy(request_path)

    background = resolve_key_color(
        request.description,
        requested=request.background.color,
        fallbacks=request.background.fallback_colors,
        conflict_hint=request.background.conflict_hint,
    )
    warnings: list[str] = []
    if background.downgraded:
        warnings.append(background.explain())

    source = Image.open(image_path)
    had_alpha = source.mode in ("RGBA", "LA") or "transparency" in source.info
    rgba = source.convert("RGBA")
    if had_alpha:
        flat = Image.new("RGBA", rgba.size, (*hex_to_rgb(background.color_used), 255))
        flat.alpha_composite(rgba)
        rgba = flat
        warnings.append(
            f"素材带透明通道，已合成到键控色 {background.color_used} 上 —— "
            "处理链的前提是纯色背景"
        )

    buffer = io.BytesIO()
    rgba.convert("RGB").save(buffer, format="PNG")
    require_source_slot(store, "seed", regenerate=replace)
    store.write_source("seed", buffer.getvalue())

    table = load_job_table(store, request)
    job = table.seed_job
    if job is None:  # pragma: no cover - planner 一定会建 seed 任务
        raise RuntimeError(f"{request.asset_id} 的任务表里没有 seed 任务")

    image = load_rgb(store.source_path("seed"))
    layout = GridLayout(frames=1, cols=1, rows=1, cell=(image.shape[1], image.shape[0]))
    processed = process_grid(
        image,
        layout,
        ProcessOptions(
            key_color=background.color_used,
            target_size=request.style.target_size,
            max_colors=request.style.max_colors,
        ),
    )
    warnings.extend(processed.warnings)
    pixel_path = save_png(processed.frames[0], store.root / "seed-pixel.png")

    if job.status is JobStatus.PLANNED:
        job.fire(JobEvent.START_EXECUTION)
        job.fire(JobEvent.PROVIDER_SUCCESS, detail=f"导入自 {Path(image_path).name}")
        job.fire(JobEvent.START_PROCESSING)
        job.fire(JobEvent.PROCESSING_DONE)
        job.fire(JobEvent.START_VALIDATION)
        job.fire(JobEvent.VALIDATION_PASSED)
        job.fire(JobEvent.REQUIRE_APPROVAL)

    provider = get_provider(config)
    manifest = ensure_manifest(
        store, request, background, provider_name=provider.name, model=provider.model
    )
    manifest.background.key_threshold = processed.key_threshold
    manifest.palette.colors = processed.palette.colors
    manifest.status = "awaiting_approval"
    manifest.save(store.manifest_path)
    store.save_job_table(table)

    return ImportResult(
        asset_id=request.asset_id,
        seed_path=store.source_path("seed"),
        pixel_path=pixel_path,
        source_size=source.size,
        had_alpha=had_alpha,
        key_color=background.color_used,
        palette=processed.palette.colors,
        warnings=warnings,
    )
