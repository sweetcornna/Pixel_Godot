"""tileset 流水线：逐块生成 → 整套一起处理 → 停在 processed 等验证。

与静态资产链的分工在 PLAN §8.1 写死了：那条链的去背景 / 包围盒 / 锚点 / 缩放基准
对 tile 逐条不适用，所以这里只做两件事 —— 把每块 tile 的原图取回来，然后**整套
一起**过处理链（共享调色板必须整批一次量化，不能逐块处理完再合）。

"整套一起"也是这条链和静态链最大的结构差异：静态链一个资产一个 job，处理紧跟
生成；这里要等**全部** tile 的原图齐了才谈得上处理。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..config import Config
from ..errors import PauseRequested, ProcessingError, ProviderError
from ..logging_utils import get_logger
from ..models.job import Job, JobEvent, JobKind, JobStatus, JobTable
from ..models.manifest import (
    AssetManifest,
    BackgroundInfo,
    CanvasInfo,
    PaletteInfo,
    ProviderInfo,
    TileEntry,
    TilesetInfo,
)
from ..models.request import AssetRequest, load_request
from ..processing.spritesheet import save_png
from ..processing.tile import process_tiles
from ..prompts import compile_tile_prompt
from ..providers import ImageProvider, get_provider
from ..storage import ArtifactStore
from ..storage.hashes import hash_file
from .common import load_rgb, record_generation, require_source_slot

logger = get_logger("pipeline.tileset")


@dataclass(frozen=True)
class TilesetPipelineResult:
    asset_id: str
    tile_ids: tuple[str, ...]
    tile_size: tuple[int, int]
    palette: list[str]
    generated: int
    """本次真正调用了 API 的 tile 数。续跑时会小于总数。"""


def _tile_jobs(table: JobTable) -> list[Job]:
    jobs = [job for job in table if job.kind is JobKind.TILE]
    if not jobs:
        raise ProcessingError("任务表里没有 tile 任务 —— 先跑 plan")
    # 按 tile_id 排序：共享调色板由整批一次量化得出，顺序不定就不幂等了。
    return sorted(jobs, key=lambda job: job.action or "")


def _generate_one(
    job: Job,
    request: AssetRequest,
    store: ArtifactStore,
    table: JobTable,
    config: Config,
    provider: ImageProvider,
    *,
    regenerate: bool,
) -> bool:
    """取回一块 tile 的原图。返回是否真的调用了 API。"""
    tile_id = job.action or ""
    spec = next(tile for tile in request.tile_list if tile.tile_id == tile_id)
    prompt = compile_tile_prompt(request, spec)

    if regenerate:
        require_source_slot(store, tile_id, regenerate=True)
        job.status = JobStatus.PLANNED
        job.attempts = 0
        job.error = None

    if job.status is not JobStatus.PLANNED:
        if store.source_path(tile_id).exists():
            return False
        raise ProcessingError(
            f"{job.id} 不在 planned 却没有原图；上次调用结果未知，需人工对账"
        )

    job.fire(JobEvent.START_EXECUTION)
    store.save_job_table(table)
    try:
        result = provider.generate(prompt.text, size=prompt.size)
    except ProviderError as exc:
        job.status = JobStatus.FAILED
        job.error = exc.message
        store.save_job_table(table)
        raise

    if result.provider != config.provider or result.model != config.model:
        raise ProcessingError(
            "生成结果与有效配置不一致："
            f"{result.provider}/{result.model} != {config.provider}/{config.model}"
        )
    store.write_source(tile_id, result.image)
    record_generation(store, job, result, prompt_chars=len(prompt.text))
    job.fire(JobEvent.PROVIDER_SUCCESS)
    store.save_job_table(table)
    return not result.cached


def create_tileset(
    request_path: str | Path,
    config: Config,
    *,
    provider: ImageProvider | None = None,
    regenerate: bool = False,
    stop_requested: threading.Event | None = None,
) -> TilesetPipelineResult:
    """生成并处理一整套 tile，停在 ``processed`` 等待验证。"""
    request = load_request(request_path)
    if request.tileset is None:
        raise ProcessingError(
            f"{request.asset_id} 不是 tileset 请求；单张静态资产请用 create-asset"
        )

    store = ArtifactStore.for_asset(config.output_dir, request.asset_id).ensure()
    store.save_request_copy(request_path)

    from ..planning.planner import plan_request

    table = plan_request(
        request,
        existing=store.load_job_table(),
        provider=config.provider,
        model=config.model,
    ).jobs
    jobs = _tile_jobs(table)

    active_provider = provider
    if active_provider is not None and (
        active_provider.name != config.provider or active_provider.model != config.model
    ):
        raise ProcessingError(
            "Provider 与有效配置不一致："
            f"{active_provider.name}/{active_provider.model} != "
            f"{config.provider}/{config.model}"
        )

    generated = 0
    for job in jobs:
        if stop_requested is not None and stop_requested.is_set():
            raise PauseRequested(f"{request.asset_id} 停在 {job.id} 之前")
        active_provider = active_provider or get_provider(config)
        if _generate_one(
            job, request, store, table, config, active_provider, regenerate=regenerate
        ):
            generated += 1

    # -- 整套一起处理 -----------------------------------------------------
    sources: dict[str, np.ndarray] = {}
    for job in jobs:
        tile_id = job.action or ""
        path = store.source_path(tile_id)
        if not path.exists():
            raise ProcessingError(f"{tile_id} 缺少原图：{path}")
        sources[tile_id] = load_rgb(path)
        if job.status is JobStatus.GENERATED:
            job.fire(JobEvent.START_PROCESSING)

    processed = process_tiles(
        sources,
        tile_size=request.tileset.tile_size,
        max_colors=request.style.max_colors,
    )

    tiles_dir = store.frames_of("tiles")
    entries: dict[str, TileEntry] = {}
    for tile_id, image in processed.tiles.items():
        out = save_png(image, tiles_dir / f"{tile_id}.png")
        source_path = store.source_path(tile_id)
        entries[tile_id] = TileEntry(
            source_image=str(source_path.relative_to(store.root)),
            image=str(out.relative_to(store.root)),
            source_hash=hash_file(source_path),
            processed_hash=hash_file(out),
        )

    width, height = processed.tile_size
    manifest = AssetManifest(
        asset_id=request.asset_id,
        asset_type=request.asset_type,
        provider=ProviderInfo(name=config.provider, model=config.model),  # type: ignore[arg-type]
        canvas=CanvasInfo(width=width, height=height),
        # tile 满幅不透明，去背景那一步从未执行 —— 如实记 opaque，不填占位色。
        background=BackgroundInfo(mode="opaque"),
        palette=PaletteInfo(
            max_colors=request.style.max_colors, colors=processed.palette.colors
        ),
        tileset=TilesetInfo(tile_size=(width, height), tiles=entries),
        status="processed",
    )
    manifest.save(store.manifest_path)

    for job in jobs:
        if job.status is JobStatus.PROCESSING:
            job.fire(JobEvent.PROCESSING_DONE)
    store.save_job_table(table)

    logger.info(
        "tileset %s 处理完成：%d 块 tile，共享 %d 色",
        request.asset_id,
        len(entries),
        len(processed.palette.colors),
    )
    return TilesetPipelineResult(
        asset_id=request.asset_id,
        tile_ids=tuple(sorted(entries)),
        tile_size=(width, height),
        palette=processed.palette.colors,
        generated=generated,
    )
