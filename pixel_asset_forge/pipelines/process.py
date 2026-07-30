"""``pixel-asset process`` —— 离线重跑确定性处理链。

**不调用 API。** 输入是 ``source/`` 下永不覆盖的原始生成图，
输出是 ``frames/`` / ``sheets/`` / ``previews/`` 与更新后的 Manifest。

这条命令存在的意义就是 SKILL.md 那条规则："能离线解决的就离线解决"。
处理逻辑或阈值的问题在这里重跑，不要重新生成 —— 重生成既慢又花钱，
而且因为生成层不可复现，重生成出来的还是另一张图。
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from ..constants import ACTION_DEFAULTS
from ..errors import ProcessingError
from ..logging_utils import get_logger
from ..models.manifest import AssetManifest, GeneratedAnimation, GridInfo
from ..models.request import AssetRequest, load_request
from ..planning.grid_layout import GridLayout, layout_for_frames
from ..processing.anchor import BOTTOM_CENTER
from ..processing.pipeline import ProcessOptions, ProcessResult, process_grid
from ..processing.scale_profile import ScaleProfile, derive_profile
from ..processing.spritesheet import compose_spritesheet, save_frames, save_gif, save_png
from ..storage.artifacts import ArtifactStore

logger = get_logger("pipeline.process")

_SOURCE_RE = re.compile(r"^(?P<key>.+?)-original(?:\.r\d+)?\.png$")


def _source_key(path: Path) -> str | None:
    """``walk-down-original.png`` → ``walk_down``。归档副本（``.r1``）跳过。"""
    match = _SOURCE_RE.match(path.name)
    if not match or ".r" in path.name:
        return None
    return match.group("key").replace("-", "_")


def _layout_for(
    key: str,
    request: AssetRequest | None,
    size: tuple[int, int],
    manifest: AssetManifest | None = None,
) -> GridLayout:
    """确定这张原图该按什么网格切。

    **Manifest 里记着的行列数优先于任何重新推导。** 布局的默认值会随版本变
    （单行条带就是一次这样的变更），而 source/ 下那张图是当时按当时的布局画的 ——
    照新默认值去切老图，每一帧都会静默错位。

    没有记录时才按帧数推：优先信 request 里声明的，再退回动作默认帧数；
    seed 是单幅立绘，1×1。
    """
    if key == "seed":
        return GridLayout(frames=1, cols=1, rows=1, cell=size)

    recorded = manifest.animations.get(key) if manifest is not None else None
    if isinstance(recorded, GeneratedAnimation) and recorded.grid is not None:
        grid = recorded.grid
        return GridLayout(
            frames=len(recorded.frames) or grid.cols * grid.rows,
            cols=grid.cols,
            rows=grid.rows,
            cell=(grid.cell[0], grid.cell[1]),
        )

    action = key.split("_", 1)[0]
    frames: int | None = None

    if request is not None:
        for spec in request.animation_list():
            if spec.name == action:
                frames = spec.frames
                break

    if frames is None:
        default = ACTION_DEFAULTS.get(action)
        if default is None:
            raise ProcessingError(
                f"无法确定 {key} 的帧数：request 里没有 {action} 动作，也没有内置默认值。"
            )
        frames = default.frames

    return layout_for_frames(frames)


def _base_options(request: AssetRequest | None, manifest: AssetManifest | None) -> ProcessOptions:
    """组装与具体图像无关的处理参数（键控色、目标尺寸、色数）。"""
    options = ProcessOptions(anchor=BOTTOM_CENTER)

    if request is not None:
        options.key_color = request.background.color
        options.target_size = request.style.target_size
        options.max_colors = request.style.max_colors

    if manifest is not None:
        options.key_color = manifest.background.color_used
        options.target_size = (manifest.canvas.width, manifest.canvas.height)
        options.max_colors = manifest.palette.max_colors

    return options


def _threshold_for(key: str, manifest: AssetManifest | None) -> float | None:
    """取这张原图**自己**的既有阈值；没有就返回 None（自动求解）。

    必须逐图取。共用一个资产级阈值会让 ``process`` 不幂等：
    首次运行每张图各自求解，写回只留下一个；再次运行时所有图被强制用那一个，
    除第一张外产出全变 —— "重跑一遍就一致"的假设当场失效（ADR-004）。
    """
    if manifest is None:
        return None
    if key == "seed":
        return manifest.background.key_threshold
    entry = manifest.animations.get(key)
    return getattr(entry, "key_threshold", None)


def _measure_reference(
    sources: list[Path],
    request: AssetRequest | None,
    base: ProcessOptions,
    manifest: AssetManifest | None,
    *,
    only: str | None,
) -> ScaleProfile | None:
    """量测各动作在自己格子里占多满，取最大者作为跨动作缩放基准。

    只看动作，不看 seed —— seed 的构图约定与动作网格不可比。
    """
    best: tuple[float, str, ProcessResult] | None = None

    for path in sources:
        key = _source_key(path)
        if key is None or key == "seed" or (only and key != only):
            continue

        image = np.array(Image.open(path).convert("RGB"))
        layout = _layout_for(key, request, (image.shape[1], image.shape[0]), manifest)
        result = process_grid(
            image,
            layout,
            replace(base, key_threshold=_threshold_for(key, manifest), scale_profile=None),
        )
        ratio = result.content_source_height / max(1, result.source_cell_height)
        if best is None or ratio > best[0]:
            best = (ratio, key, result)

    if best is None:
        return None

    _ratio, key, result = best
    logger.info("跨动作缩放基准取自 %s（subject_ratio 最大）", key)
    return derive_profile(
        key,
        content_height=result.content_source_height,
        cell_height=result.source_cell_height,
        canvas_height=base.target_size[1],
        output_height=result.output_content_height,
    )


def run_process(asset_dir: str | Path, *, only: str | None = None) -> list[dict[str, Any]]:
    """重跑一个资产的全部（或指定）动作。返回每个动作的处理摘要。"""
    root = Path(asset_dir)
    if not root.exists():
        raise ProcessingError(f"资产目录不存在：{root}")

    store = ArtifactStore(root=root)
    if not store.source.exists():
        raise ProcessingError(f"{root} 下没有 source/ 目录 —— 还没有生成过任何原图")

    request = load_request(store.request_path) if store.request_path.exists() else None
    manifest = AssetManifest.load(store.manifest_path) if store.manifest_path.exists() else None
    base = _base_options(request, manifest)

    summaries: list[dict[str, Any]] = []
    animations: dict[str, Any] = dict(manifest.animations) if manifest else {}
    sheets: dict[str, str] = dict(manifest.sheets) if manifest else {}
    palette_colors: list[str] = []
    seed_threshold: float | None = manifest.background.key_threshold if manifest else None

    sources = sorted(store.source.glob("*-original.png"))

    # 基准要取**所有动作里 subject_ratio 最大的那个**。
    #
    # 取第一个（字母序 → idle）会让幅度最小的姿势当参考，其余动作全被推到画布外
    # 再钳回来，相对大小信息全丢；取 seed 也不行 —— seed 是 1×1 整幅画布、
    # 四周留 10% 边距，与动作格子的构图约定不可比（实测 0.57 vs 0.80）。
    #
    # 代价是要多跑一趟无缩放的量测。process 是离线命令，这个代价可以接受。
    #
    # **不复用 Manifest 里存着的基准。** 增量生成时基准是边走边顶替的，
    # 后来的参考动作在写入时已经被前一任基准缩过一道，它记下的 canvas_fraction
    # 因此是循环推导的产物 —— 实测 hurt 顶替成参考后记下 0.427，
    # 于是全部动作都按"参考只占画布 43%"去缩，整个资产小了一圈。
    # process 是唯一看得见全部动作的地方，正该由它一次算准。
    profile: ScaleProfile | None = None
    if only is not None and manifest is not None and manifest.scale_profile is not None:
        # 只重跑单个动作时看不到别的动作，只能沿用既有基准，否则这一个动作
        # 会按自己的比例重新定标，与其余动作对不上。
        profile = ScaleProfile(
            reference=manifest.scale_profile.reference,
            subject_ratio=manifest.scale_profile.subject_ratio,
            canvas_fraction=manifest.scale_profile.canvas_fraction,
        )
    else:
        profile = _measure_reference(sources, request, base, manifest, only=only)

    for path in sources:
        key = _source_key(path)
        if key is None or (only and key != only):
            continue

        image = np.array(Image.open(path).convert("RGB"))
        layout = _layout_for(key, request, (image.shape[1], image.shape[0]), manifest)
        options = replace(
            base,
            key_threshold=_threshold_for(key, manifest),
            scale_profile=profile,
        )
        result = process_grid(image, layout, options)

        # seed 只产出一张标准图，不进 frames/ 的动画序列
        if key == "seed":
            save_png(result.frames[0], store.root / "seed-pixel.png")
            seed_threshold = result.key_threshold
        else:
            frame_paths = save_frames(result.frames, store.frames_of(key), stem=key)
            sheet, _ = compose_spritesheet(result.frames)
            sheet_path = save_png(sheet, store.sheets / f"{key}.png")
            sheets[key] = str(sheet_path.relative_to(store.root))

            action = key.split("_", 1)[0]
            defaults = ACTION_DEFAULTS.get(action)
            fps = defaults.fps if defaults else 10
            loop = defaults.loop if defaults else True
            if request is not None:
                for spec in request.animation_list():
                    if spec.name == action:
                        fps, loop = spec.fps, spec.loop
                        break

            try:
                save_gif(result.frames, store.previews / f"{key}.gif", fps=fps, loop=loop)
            except Exception as exc:
                logger.warning("生成 %s 预览 GIF 失败：%s", key, exc)

            animations[key] = GeneratedAnimation(
                fps=fps,
                loop=loop,
                grid=GridInfo(
                    cols=layout.cols,
                    rows=layout.rows,
                    cell=layout.actual_cell(result.source_size),
                    requested_size=layout.size,
                    actual_size=result.source_size,
                ),
                source_image=str(path.relative_to(store.root)),
                key_threshold=result.key_threshold,
                frames=[str(p.relative_to(store.root)) for p in frame_paths],
            )

        palette_colors = result.palette.colors
        summaries.append(
            {
                "key": key,
                "frames": len(result.frames),
                "frame_size": f"{result.frame_size[0]}×{result.frame_size[1]}",
                "threshold": result.key_threshold,
                "background_ratio": result.background_ratio,
                "colors": result.palette.color_count,
                "quantization_error": result.palette.quantization_error_ratio,
                "anchor_drift": result.anchor_drift_px,
                "split_method": result.split.method.value if result.split else "slots",
                "fragments": result.split.fragments_attached if result.split else 0,
                "sprites_found": len(result.frames),
                "source_size": result.source_size,
                "warnings": list(result.warnings),
            }
        )

    if summaries:
        _update_manifest(store, manifest, request, base, animations, sheets,
                         palette_colors, seed_threshold, profile)
    return summaries


def _update_manifest(
    store: ArtifactStore,
    manifest: AssetManifest | None,
    request: AssetRequest | None,
    options: ProcessOptions,
    animations: dict[str, Any],
    sheets: dict[str, str],
    palette_colors: list[str],
    threshold: float | None,
    profile: ScaleProfile | None = None,
) -> None:
    """把这次处理的结果写回 Manifest。

    各动作的阈值已经写在自己的 ``animations[*].key_threshold`` 里；
    这里的 ``threshold`` 只是种子图的。
    """
    from ..models.manifest import (
        BackgroundInfo,
        CanvasInfo,
        PaletteInfo,
        ProviderInfo,
        ScaleProfileInfo,
    )

    if manifest is None:
        if request is None:
            logger.warning("既没有 manifest 也没有 request，跳过 manifest 写回")
            return
        manifest = AssetManifest(
            asset_id=request.asset_id,
            asset_type=request.asset_type,
            provider=ProviderInfo(name="mock", model="unknown"),
            canvas=CanvasInfo(width=options.target_size[0], height=options.target_size[1]),
            background=BackgroundInfo(
                mode=request.background.mode,
                color_requested=request.background.color,
                color_used=options.key_color,
                fallback_stage="tolerant_key",
                key_threshold=threshold,
            ),
            palette=PaletteInfo(max_colors=options.max_colors, colors=palette_colors),
            scale_profile=(
                ScaleProfileInfo(
                    reference=profile.reference,
                    subject_ratio=profile.subject_ratio,
                    canvas_fraction=profile.canvas_fraction,
                )
                if profile is not None
                else None
            ),
            status="processed",
        )
    else:
        if threshold is not None:
            manifest.background.key_threshold = threshold
        manifest.palette.colors = palette_colors
        manifest.status = "processed"
        if profile is not None:
            manifest.scale_profile = ScaleProfileInfo(
                reference=profile.reference,
                subject_ratio=profile.subject_ratio,
                canvas_fraction=profile.canvas_fraction,
            )

    manifest.animations = animations
    manifest.sheets = sheets
    manifest.save(store.manifest_path)
