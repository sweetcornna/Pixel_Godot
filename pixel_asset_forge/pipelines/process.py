"""``pixel-asset process`` —— 离线重跑确定性处理链。

**不调用 API。** 输入是 ``source/`` 下永不覆盖的原始生成图，
输出是 ``frames/`` / ``sheets/`` / ``previews/`` 与更新后的 Manifest。

这条命令存在的意义就是 SKILL.md 那条规则："能离线解决的就离线解决"。
处理逻辑或阈值的问题在这里重跑，不要重新生成 —— 重生成既慢又花钱，
而且因为生成层不可复现，重生成出来的还是另一张图。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from ..constants import ACTION_DEFAULTS, ACTION_SIZE_BAND, split_animation_key
from ..errors import ProcessingError
from ..logging_utils import get_logger
from ..models.manifest import AssetManifest, GeneratedAnimation, GridInfo
from ..models.request import AssetRequest, load_request
from ..planning.grid_layout import GridLayout, layout_for_frames
from ..processing.anchor import BOTTOM_CENTER, align_frames
from ..processing.palette import quantize_frames
from ..processing.pipeline import ProcessOptions, ProcessResult, process_grid
from ..processing.resize import nearest_resize
from ..processing.scale_profile import ScaleProfile, derive_profile
from ..processing.spritesheet import compose_spritesheet, save_frames, save_gif, save_png
from ..storage.artifacts import ArtifactStore

logger = get_logger("pipeline.process")

_SOURCE_RE = re.compile(r"^(?P<key>.+?)-original(?:\.r\d+)?\.png$")

#: 补间的中间产物：``walk-down-key00-original.png`` / ``walk-down-gap00-original.png``。
_INTERMEDIATE_RE = re.compile(r"-(?:key|gap)\d+-original")


def _source_key(path: Path) -> str | None:
    """``walk-down-original.png`` → ``walk_down``。

    跳过两类文件：

    - **归档副本**（``.r1``）—— 重生成时留下的旧图
    - **补间的中间产物**（``-key00-`` / ``-gap00-``）—— 它们是补间的输入与
      中间结果，不是独立的动作。不跳过的话 ``process`` 会把
      ``hurt-down-gap00-original.png`` 当成动作 ``hurt_down_gap00``，
      查不到帧数直接报错 —— **任何补过间的资产都跑不了 process**。
    """
    if ".r" in path.name or _INTERMEDIATE_RE.search(path.name):
        return None
    match = _SOURCE_RE.match(path.name)
    if not match:
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

    action, _direction = split_animation_key(key)
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


@dataclass
class Survey:
    """量测趟的产出：跨动作缩放基准 + 跨动作共用调色板 + 站立基准高度。"""

    profile: ScaleProfile | None
    palette: list[str]
    standing_height: float
    """站立类动作输出高度的中位数。各动作的尺寸区间以它为基准。"""


def _survey(
    sources: list[Path],
    request: AssetRequest | None,
    base: ProcessOptions,
    manifest: AssetManifest | None,
    *,
    only: str | None,
) -> Survey:
    """先不缩放地跑一趟，量出三件**只有看得见全部动作才能定**的东西。

    1. **跨动作缩放基准** —— 取 subject_ratio 最大的动作。只看动作不看 seed，
       seed 的构图约定与动作网格不可比。
    2. **跨动作共用调色板** —— 各动作各自量化的话，同一个角色在不同动作里
       会换色。实测 6 个角色，跨动作调色板重合度 **0%**。
    3. **站立基准高度** —— 站立类动作输出高度的中位数，用来判断某个动作
       是不是被模型画得离谱地大或小。
    """
    best: tuple[float, str, ProcessResult] | None = None
    swatches: list[np.ndarray] = []
    standing: list[int] = []

    for path in sources:
        key = _source_key(path)
        if key is None or key == "seed" or (only and key != only):
            continue
        # 量测这一趟同样绕不开补过间的动作 —— 它的帧不来自单张网格
        if _is_interpolated(key, manifest):
            continue

        image = np.array(Image.open(path).convert("RGB"))
        layout = _layout_for(key, request, (image.shape[1], image.shape[0]), manifest)
        result = process_grid(
            image,
            layout,
            replace(base, key_threshold=_threshold_for(key, manifest),
                    scale_profile=None, palette=None),
        )
        ratio = result.content_source_height / max(1, result.source_cell_height)
        if best is None or ratio > best[0]:
            best = (ratio, key, result)

        swatches.extend(result.frames)
        action, _direction = split_animation_key(key)
        if ACTION_SIZE_BAND.get(action) is not None:
            standing.append(result.output_content_height)

    if best is None:
        return Survey(None, [], 0.0)

    # 一次性从全部动作的帧上解出调色板 —— 这才是"共用"的字面意思。
    shared = quantize_frames(swatches, base.max_colors).colors if swatches else []

    _ratio, key, result = best
    logger.info("跨动作缩放基准取自 %s（subject_ratio 最大）", key)
    return Survey(
        profile=derive_profile(
            key,
            content_height=result.content_source_height,
            cell_height=result.source_cell_height,
            canvas_height=base.target_size[1],
            output_height=result.output_content_height,
        ),
        palette=shared,
        standing_height=float(np.median(standing)) if standing else 0.0,
    )


def _is_interpolated(key: str, manifest: AssetManifest | None) -> bool:
    """这个动作补过间吗？

    补间之后帧数多于关键帧数，而且**帧不再来自单张网格** —— 一部分是原网格
    抽的，一部分来自各间隔的网格。``process`` 的整条逻辑建立在"一个动作
    对应一张源网格"之上，对它无能为力：照原网格的行列去切，第 5 帧就会
    撞上"帧下标超出网格容量"。

    所以跳过。要重出补过间的动作，跑 ``interpolate`` 而不是 ``process``。
    """
    if manifest is None:
        return False
    entry = manifest.animations.get(key)
    if not isinstance(entry, GeneratedAnimation) or entry.keyframe_count is None:
        return False
    return len(entry.frames) > entry.keyframe_count


def _clamp_to_size_band(
    result: ProcessResult,
    key: str,
    standing: float,
    base: ProcessOptions,
    summaries: list[dict[str, Any]],
) -> ProcessResult:
    """把输出内容高度钳进该动作的可信区间。区间为 None 的动作原样返回。"""
    action, _direction = split_animation_key(key)
    band = ACTION_SIZE_BAND.get(action)
    if band is None or result.output_content_height <= 0:
        return result

    low, high = standing * band[0], standing * band[1]
    current = float(result.output_content_height)
    wanted = min(max(current, low), high)
    if abs(wanted - current) < 1.0:
        return result

    factor = wanted / current
    canvas = base.target_size
    frames = []
    for frame in result.frames:
        height, width = frame.shape[:2]
        size = (max(1, round(width * factor)), max(1, round(height * factor)))
        frames.append(nearest_resize(frame, size))
    frames = align_frames(frames, canvas, anchor=base.anchor)

    result.warnings.append(
        f"输出高度 {current:.0f}px 超出 {action} 相对站立基准 {standing:.0f}px 的"
        f"可信区间 {band[0]:.0%}~{band[1]:.0%}，已按 {factor:.2f}× 钳回 {wanted:.0f}px —— "
        "模型把这个动作画得偏大或偏小，不是真实的姿势差异"
    )
    return replace(result, frames=frames, output_content_height=round(wanted))


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
    survey: Survey | None = None
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
        survey = _survey(sources, request, base, manifest, only=only)
        profile = survey.profile

    for path in sources:
        key = _source_key(path)
        if key is None or (only and key != only):
            continue
        if _is_interpolated(key, manifest):
            logger.info("%s 已补过间，跳过 —— 它的帧不是从单张网格来的", key)
            continue

        image = np.array(Image.open(path).convert("RGB"))
        layout = _layout_for(key, request, (image.shape[1], image.shape[0]), manifest)
        options = replace(
            base,
            key_threshold=_threshold_for(key, manifest),
            scale_profile=profile,
            # 跨动作共用调色板 —— 不共用的话同一个角色在不同动作里会换色
            # （实测跨动作重合度 0%）。只重跑单个动作时拿不到全局调色板，
            # 沿用 Manifest 里记着的那套。
            palette=(survey.palette if survey else (manifest.palette.colors if manifest else None))
                    or None,
        )
        result = process_grid(image, layout, options)

        # 站立类动作的尺寸钳到可信区间内。
        #
        # 跨动作缩放基准的前提是"尺寸差异是真实的姿势差异"，于是把模型的随机
        # 漂移也原样保住了 —— 实测史莱姆待机 70px、走路 45px，走路不会让角色
        # 矮三成。真实的姿势差异是有界的，超出的部分判为漂移。
        if key != "seed" and survey is not None and survey.standing_height > 0:
            result = _clamp_to_size_band(result, key, survey.standing_height, base, summaries)

        # seed 只产出一张标准图，不进 frames/ 的动画序列
        if key == "seed":
            save_png(result.frames[0], store.root / "seed-pixel.png")
            seed_threshold = result.key_threshold
        else:
            frame_paths = save_frames(result.frames, store.frames_of(key), stem=key)
            sheet, _ = compose_spritesheet(result.frames)
            sheet_path = save_png(sheet, store.sheets / f"{key}.png")
            sheets[key] = str(sheet_path.relative_to(store.root))

            action, _dir = split_animation_key(key)
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
