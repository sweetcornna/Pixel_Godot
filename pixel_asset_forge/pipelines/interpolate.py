"""生成式补间（PLAN §8 Sprint 6.8.3）。调用 API。

给两张关键帧，让模型画出它们之间的 M 帧。

**算法插值不适用。** 光流、morph 这类做法对像素画会产出调色板外的新颜色
和糊掉的边缘 —— 那正是这个项目花了大力气消除的东西。所以中间帧是**生成**的，
但生成完之后立刻交给确定性链收拾干净。

两条硬约束，少一条这个功能就没有意义：

- **关键帧原样保留。** 用户给的帧是基准，不能被"顺手优化"。补完之后
  它们必须与导入时逐字节一致。
- **调色板锁死到关键帧的调色板。** 中间帧重新量化会解出自己的一套色号，
  同一块布料在关键帧与中间帧之间跳色 —— 播放时整个角色闪。

每个间隔**一次调用画完**。逐帧调用会让身份漂移，这是全项目最贵的教训。
"""

from __future__ import annotations

import io
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from ..config import Config
from ..constants import split_animation_key
from ..errors import ProcessingError
from ..logging_utils import get_logger
from ..models.manifest import AssetManifest, GeneratedAnimation
from ..models.request import load_request
from ..planning.framerate import FrameBudget, frame_order, plan_inbetweens
from ..planning.grid_layout import GridLayout, layout_for_frames
from ..processing.anchor import BOTTOM_CENTER, align_frames, anchor_drift
from ..processing.background import resolve_key_color
from ..processing.chroma_key import apply_chroma_key, hex_to_rgb
from ..processing.component_split import split_frames
from ..processing.crop import crop_all
from ..processing.palette import snap_to_palette
from ..processing.pixel_cleanup import cleanup_frames
from ..processing.resize import nearest_resize
from ..processing.spritesheet import compose_spritesheet, save_frames, save_gif, save_png
from ..prompts.inbetween import compile_inbetween_prompt
from ..providers import ReferenceImage, bypass_cache, get_provider
from ..storage.artifacts import ArtifactStore

logger = get_logger("pipeline.interpolate")


@dataclass
class InterpolateResult:
    asset_id: str
    key: str
    budget: FrameBudget
    frame_paths: list[Path]
    order: list[str]
    palette: list[str]
    max_palette_drift: float
    anchor_drift_px: float
    api_calls: int
    warnings: list[str]


def _to_png(rgba: np.ndarray, key_rgb: tuple[int, int, int]) -> bytes:
    """把一帧 RGBA 压回"键控色背景的 RGB PNG" —— 与 source/ 里的原图同一种形态。

    补间的参考图必须和生成路径喂给模型的东西长得一样，否则模型看到的是
    带 alpha 的图，画出来的背景也未必是纯键控色。
    """
    flat = Image.new("RGBA", (rgba.shape[1], rgba.shape[0]), (*key_rgb, 255))
    flat.alpha_composite(Image.fromarray(rgba))
    buffer = io.BytesIO()
    flat.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def _keyframes_from_grid(
    store: ArtifactStore,
    key: str,
    entry: GeneratedAnimation,
    key_rgb: tuple[int, int, int],
) -> list[bytes]:
    """把**生成出来的**动作网格拆成逐帧关键帧。

    生成的动作天然就是关键帧序列 —— 只是帧数少。实测 attack / hurt 只有 4 帧，
    相邻帧的轮廓变化中位 35~41%，是 walk（12%）的三倍、idle（4.6%）的八倍。
    每一帧都像重画的，播起来就是"鬼畜"。补间正是治这个的。

    从**源网格**而不是 frames/ 里取：源网格是全分辨率（一格三四百像素），
    成品帧只有 96px。参考图分辨率越高，模型越能抓住细节。
    """
    if entry.source_image is None or entry.grid is None:
        raise ProcessingError(
            f"{key} 没有记录源网格，拆不出关键帧 —— "
            f"它可能是镜像派生的动作，那种动作请补它的源方向。"
        )
    source = store.root / entry.source_image
    if not source.exists():
        raise ProcessingError(f"{key} 的源网格不在了：{source}")

    grid = entry.grid
    layout = GridLayout(
        frames=len(entry.frames) or grid.cols * grid.rows,
        cols=grid.cols,
        rows=grid.rows,
        cell=(grid.cell[0], grid.cell[1]),
    )
    raw = np.array(Image.open(source).convert("RGB"))
    keyed = apply_chroma_key(raw, key_rgb).rgba
    split = split_frames(keyed, layout)
    return [_to_png(frame, key_rgb) for frame in split.frames]


def _keyframes(
    store: ArtifactStore,
    key: str,
    entry: GeneratedAnimation,
    key_rgb: tuple[int, int, int],
) -> list[bytes]:
    """取这个动作的关键帧，两种来源都收。

    - **导入的**（``import --as keyframes``）—— 用户给的原图，原样读
    - **生成的** —— 从源网格拆，帧数少的动作正是最需要补间的
    """
    stem = key.replace("_", "-")
    imported = sorted(store.source.glob(f"{stem}-key*-original.png"))
    if len(imported) >= 2:
        return [path.read_bytes() for path in imported]

    frames = _keyframes_from_grid(store, key, entry, key_rgb)
    if len(frames) < 2:
        raise ProcessingError(
            f"{key} 只拆出 {len(frames)} 帧，补间至少要两帧"
        )
    return frames


def _tile(image: Image.Image, size: tuple[int, int], cols: int, rows: int) -> bytes:
    """把一张关键帧平铺进每个格子，作为 ``edit`` 的底图。

    同 anchor sheet 的道理：每格先摆好一个姿态、大小、位置都正确的角色，
    模型要做的从"照描述画 M 个角色"变成"把这个角色往目标姿势推 M 步"。
    """
    width, height = size
    cell_w, cell_h = width // cols, height // rows
    fitted = image.resize((cell_w, cell_h), Image.Resampling.NEAREST)

    canvas = Image.new("RGB", (width, height))
    for index in range(cols * rows):
        col, row = index % cols, index // cols
        canvas.paste(fitted, (col * cell_w, row * cell_h))

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue()


def run_interpolate(
    asset_dir: str | Path,
    *,
    key: str,
    config: Config,
    target_fps: int | None = None,
    target_frames: int | None = None,
    regenerate: bool = False,
) -> InterpolateResult:
    """把一段已导入的关键帧补到目标帧数。"""
    store = ArtifactStore(root=Path(asset_dir))
    if not store.manifest_path.exists():
        raise ProcessingError(f"{asset_dir} 下没有 Manifest —— 先导入关键帧")

    manifest = AssetManifest.load(store.manifest_path)
    entry = manifest.animations.get(key)
    if not isinstance(entry, GeneratedAnimation):
        raise ProcessingError(f"{key} 不在 Manifest 里，或不是生成型动作")

    request = load_request(store.request_path)
    background = resolve_key_color(
        request.description,
        requested=request.background.color,
        fallbacks=request.background.fallback_colors,
        conflict_hint=request.background.conflict_hint,
    )
    key_rgb = hex_to_rgb(background.color_used)
    canvas = (manifest.canvas.width, manifest.canvas.height)

    sources = _keyframes(store, key, entry, key_rgb)
    # 用**关键帧自己的**帧率，不是 entry.fps —— 后者补完一次就被改成目标帧率了，
    # 再拿它当源帧率会算出"已经够了，不需要补间"。
    keyframe_fps = entry.keyframe_fps or entry.fps
    budget = plan_inbetweens(
        len(sources),
        source_fps=keyframe_fps,
        target_fps=target_fps or keyframe_fps,
        loop=entry.loop,
        target_frames=target_frames,
    )
    if budget.generated_frames == 0:
        raise ProcessingError(
            f"{key} 已经是 {budget.target_frames} 帧，不需要补间。"
            "要补更多帧就调高 --target-fps 或 --target-frames。"
        )

    # 调色板是关键帧定的，中间帧只能服从（模块 docstring 第二条硬约束）。
    palette = list(manifest.palette.colors)
    if not palette:
        raise ProcessingError(
            f"{key} 的 Manifest 里没有调色板 —— 补间必须锁死到关键帧的调色板"
        )

    provider = get_provider(config)
    warnings: list[str] = []
    action, direction = split_animation_key(key)

    # 补一次之后再补到别的帧率，是**合法的重生成**：间隔数与每隔的帧数都变了，
    # 上一轮的网格没有复用价值。但原图永不覆盖，所以先归档。
    # 不归档的话第二次 interpolate 会撞上 "已存在且内容不同" 直接失败。
    for previous in sorted(store.source.glob(f"{key.replace('_', '-')}-gap*-original.png")):
        archived = store.archive_source(previous.stem.replace("-original", ""))
        if archived is not None:
            logger.info("归档上一轮的间隔网格 %s", archived.name)

    # 每个间隔一次调用，把该间隔的全部中间帧一次画完。
    generated: dict[int, list[np.ndarray]] = {}
    for gap, count in enumerate(budget.inbetweens):
        if count == 0:
            continue
        start = Image.open(io.BytesIO(sources[gap])).convert("RGB")
        end_bytes = sources[(gap + 1) % len(sources)]

        layout = layout_for_frames(count)
        prompt = compile_inbetween_prompt(
            request,
            action=action,
            direction=direction,
            frames=count,
            layout=layout,
            key_color=background.color_used,
        )

        with bypass_cache(provider) if regenerate else nullcontext():
            result = provider.edit(
                prompt.text,
                base_image=_tile(start, prompt.size, layout.cols, layout.rows),
                size=prompt.size,
                references=[
                    ReferenceImage("start", sources[gap]),
                    ReferenceImage("end", end_bytes),
                ],
            )
        logger.info("间隔 %d：补 %d 帧", gap, count)

        # 生成的网格原图要存进 source/ —— 与其它生成路径一视同仁：
        # 没有它就既不能离线重跑，也没法在产出可疑时回头看模型到底画了什么。
        store.write_source(f"{key}-gap{gap:02d}", result.image)

        raw = np.array(Image.open(io.BytesIO(result.image)).convert("RGB"))
        keyed_result = apply_chroma_key(raw, key_rgb)
        if keyed_result.suspicious:
            warnings.append(
                f"间隔 {gap} 的产出键控后背景占比 {keyed_result.background_ratio:.1%} 异常 —— "
                "模型可能没把背景画成纯键控色，中间帧会连着背景一起被切出来"
            )
        keyed = keyed_result.rgba
        split = split_frames(keyed, layout)
        generated[gap] = list(split.frames[:count])
        if len(generated[gap]) < count:
            warnings.append(
                f"间隔 {gap} 只抽出 {len(generated[gap])} 帧（要 {count}）—— "
                "姿势可能粘连成一片，跑 `validate` 看 contact sheet"
            )

    # -- 关键帧与中间帧合并 --------------------------------------------------
    #
    # **两者不在同一个坐标系里。** 中间帧已被 split_frames 裁到各自的包围盒，
    # 关键帧还是整幅原图；而且两者来自不同分辨率的源（关键帧是用户的 256px 素材，
    # 中间帧来自 1881px 的生成网格），绝对像素高度根本不可比。
    # 直接丢进同一个 crop_all 求共用框，求出来的框没有意义 —— 实测中间帧全部
    # 撑满 128×128，关键帧才 80×100。
    #
    # 正确做法分两步：关键帧之间用共用框保住它们的相对大小；中间帧则按**所在间隔
    # 两端关键帧的输出高度线性插值**定尺寸。动画师就是这么补的，而且它是确定性的。
    keyframes = [
        apply_chroma_key(
            np.array(Image.open(io.BytesIO(data)).convert("RGB")), key_rgb
        ).rgba
        for data in sources
    ]

    key_cropped, _box = crop_all(keyframes)
    content_h, content_w = key_cropped[0].shape[:2]
    scale = min(canvas[0] / content_w, canvas[1] / content_h)
    scaled = (max(1, round(content_w * scale)), max(1, round(content_h * scale)))
    key_scaled = [nearest_resize(f, scaled) for f in key_cropped]

    def _content_height(frame: np.ndarray) -> int:
        rows = np.nonzero(frame[:, :, 3])[0]
        return int(rows.max() - rows.min() + 1) if rows.size else 1

    key_heights = [_content_height(f) for f in key_scaled]

    ordered: list[np.ndarray] = []
    for index, frame in enumerate(key_scaled):
        ordered.append(frame)

        gap_frames = generated.get(index, [])
        if not gap_frames:
            continue
        start_h = key_heights[index]
        end_h = key_heights[(index + 1) % len(key_heights)]
        total = len(gap_frames)
        for step, raw_frame in enumerate(gap_frames):
            weight = (step + 1) / (total + 1)
            wanted = start_h + (end_h - start_h) * weight
            own_h, own_w = raw_frame.shape[:2]
            own_content = _content_height(raw_frame)
            factor = wanted / max(1, own_content)
            size = (max(1, round(own_w * factor)), max(1, round(own_h * factor)))
            ordered.append(nearest_resize(raw_frame, size))

    frames = align_frames(ordered, canvas, anchor=BOTTOM_CENTER)

    frames, drift_max = snap_to_palette(frames, palette)
    frames = cleanup_frames(frames)
    if drift_max > 60:
        warnings.append(
            f"中间帧最远偏离调色板 {drift_max:.0f}（RGB 距离）—— "
            "模型用了关键帧里没有的颜色，已被拉回最近色。颜色明显不对就重新生成。"
        )

    frame_paths = save_frames(frames, store.frames_of(key), stem=key)
    sheet, _ = compose_spritesheet(frames)
    sheet_path = save_png(sheet, store.sheets / f"{key}.png")
    try:
        save_gif(frames, store.previews / f"{key}.gif",
                 fps=budget.target_fps, loop=entry.loop)
    except Exception as exc:  # pragma: no cover - 预览失败不该阻断补间
        logger.warning("生成 %s 预览 GIF 失败：%s", key, exc)

    entry.fps = budget.target_fps
    entry.keyframe_count = len(sources)
    entry.keyframe_fps = keyframe_fps
    entry.frames = [str(p.relative_to(store.root)) for p in frame_paths]
    manifest.sheets[key] = str(sheet_path.relative_to(store.root))
    manifest.save(store.manifest_path)

    return InterpolateResult(
        asset_id=manifest.asset_id,
        key=key,
        budget=budget,
        frame_paths=frame_paths,
        order=frame_order(budget),
        palette=palette,
        max_palette_drift=drift_max,
        anchor_drift_px=anchor_drift(frames, anchor=BOTTOM_CENTER),
        api_calls=budget.api_calls,
        warnings=warnings,
    )
