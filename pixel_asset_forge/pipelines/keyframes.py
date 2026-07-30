"""导入一段关键帧（PLAN §8 Sprint 6.8.2）。不调用 API。

与 `import_seed` 的单张导入不同，这里进来的是**同一个动作的若干张图**。
多出来的三件事，每一件不做都会在播放时看出来：

- **共用一套调色板。** 各帧各自量化，同一块布料在相邻帧里会落到不同的色号上，
  播放时整个角色闪色。调色板必须一次性从全部帧上求解。
- **共用一个裁剪框与锚点。** 逐帧各裁各的等于给每帧施加不同平移，角色会跳动。
- **顺序由文件名定，不由目录顺序定。** ``glob`` 的返回顺序不保证，
  而帧序错乱**无法自动检测**（见 `validation/frame_order.py`）。

导入后这段关键帧就是补间的输入（Sprint 6.8.3）。
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from ..config import Config
from ..errors import ProcessingError
from ..logging_utils import get_logger
from ..models.manifest import AssetManifest, GeneratedAnimation
from ..models.request import load_request
from ..planning.framerate import FrameBudget, plan_inbetweens
from ..processing.anchor import BOTTOM_CENTER, align_frames, anchor_drift
from ..processing.background import resolve_key_color
from ..processing.chroma_key import apply_chroma_key, hex_to_rgb, zero_transparent_rgb
from ..processing.crop import crop_all
from ..processing.palette import quantize_frames, snap_to_palette
from ..processing.pixel_cleanup import cleanup_frames
from ..processing.resize import nearest_resize
from ..processing.spritesheet import compose_spritesheet, save_frames, save_gif, save_png
from ..storage.artifacts import ArtifactStore
from .common import ensure_manifest

logger = get_logger("pipeline.keyframes")

#: 关键帧图片的扩展名。大小写都收。
KEYFRAME_SUFFIXES = (".png", ".webp", ".gif", ".bmp")


@dataclass
class KeyframeImport:
    asset_id: str
    key: str
    keyframe_paths: list[Path]
    frame_paths: list[Path]
    canvas: tuple[int, int]
    palette: list[str]
    anchor_drift_px: float
    budget: FrameBudget | None
    warnings: list[str]


def collect_keyframes(source: str | Path) -> list[Path]:
    """按**文件名排序**收集关键帧。目录或显式列表都收。

    排序用的是文件名而不是 ``glob`` 的返回顺序 —— 后者不保证，
    而帧序错乱是无法自动检测的静默失败。
    """
    root = Path(source)
    if root.is_dir():
        found = sorted(
            path
            for path in root.iterdir()
            if path.is_file() and path.suffix.lower() in KEYFRAME_SUFFIXES
        )
    else:
        found = [root]

    if not found:
        allowed = " / ".join(KEYFRAME_SUFFIXES)
        raise ProcessingError(f"{root} 下没有关键帧图片（支持 {allowed}）")
    return found


def _load_flat(path: Path, key_color: str) -> tuple[np.ndarray, bool]:
    """读入一帧并把透明背景合成到键控色上。返回 ``(RGB, 原本是否带 alpha)``。"""
    image = Image.open(path)
    had_alpha = image.mode in ("RGBA", "LA", "P") and (
        image.mode != "P" or "transparency" in image.info
    )
    rgba = image.convert("RGBA")
    if had_alpha:
        flat = Image.new("RGBA", rgba.size, (*hex_to_rgb(key_color), 255))
        flat.alpha_composite(rgba)
        rgba = flat
    return np.array(rgba.convert("RGB")), had_alpha


def import_keyframes(
    request_path: str | Path,
    source: str | Path,
    config: Config,
    *,
    action: str,
    direction: str | None = "down",
    source_fps: int | None = None,
    target_fps: int | None = None,
    target_frames: int | None = None,
    loop: bool | None = None,
) -> KeyframeImport:
    """把一段关键帧导入成一个动作。不调用 API。

    **动作不必事先在 request 里声明。** request 里 ``animations[*].frames``
    的枚举（4/6/8/9/12）说的是"生成网格的帧数档位"，而导入的关键帧不走网格 ——
    数量由磁盘上有几个文件决定。声明了就沿用其中的 fps 与 loop，没声明就用参数。

    给了 ``target_fps`` 或 ``target_frames`` 时顺带算出补间预算并写进 Manifest，
    但**不生成**任何中间帧 —— 那是 Sprint 6.8.3 的事。
    """
    request = load_request(request_path)
    store = ArtifactStore.for_asset(config.output_dir, request.asset_id).ensure()
    store.save_request_copy(request_path)

    key = f"{action}_{direction}" if direction else action
    paths = collect_keyframes(source)
    if len(paths) < 2:
        raise ProcessingError(
            f"{source} 只有 {len(paths)} 张图。一段关键帧至少要两张 —— "
            "单张请用 import-seed 导入为 canonical seed。"
        )

    background = resolve_key_color(
        request.description,
        requested=request.background.color,
        fallbacks=request.background.fallback_colors,
        conflict_hint=request.background.conflict_hint,
    )
    warnings: list[str] = []
    if background.downgraded:
        warnings.append(background.explain())

    # -- 1. 逐帧读入并去背景 ------------------------------------------------
    #
    # flat 与 keyed 都要留着：flat 是"合成到键控色之后的原始输入"，
    # 要原样存进 source/；keyed 是抠完背景的中间结果，只在本次处理里用。
    flat: list[np.ndarray] = []
    keyed: list[np.ndarray] = []
    alpha_seen = False
    for path in paths:
        rgb, had_alpha = _load_flat(path, background.color_used)
        alpha_seen = alpha_seen or had_alpha
        flat.append(rgb)
        keyed.append(apply_chroma_key(rgb, hex_to_rgb(background.color_used)).rgba)
    if alpha_seen:
        warnings.append(
            f"关键帧带透明通道，已合成到键控色 {background.color_used} 上 —— "
            "处理链的前提是纯色背景"
        )

    sizes = {frame.shape[:2] for frame in keyed}
    if len(sizes) > 1:
        raise ProcessingError(
            f"{key} 的关键帧尺寸不一致：{sorted(sizes)}。"
            "补间要求所有关键帧同尺寸，否则统一裁剪框无从谈起。"
        )

    # -- 2. 整组共用一个裁剪框 + 统一缩放 + 锚点对齐 ------------------------
    #
    # 这三步的顺序与 process_grid 一致。共用裁剪框是关键：逐帧各裁各的
    # 等于给每帧施加不同平移，播放时角色会跳。
    frames, _box = crop_all(keyed)
    canvas = request.style.target_size
    content_h, content_w = frames[0].shape[:2]
    scale = min(canvas[0] / content_w, canvas[1] / content_h)
    scaled = (max(1, round(content_w * scale)), max(1, round(content_h * scale)))
    frames = [nearest_resize(frame, scaled) for frame in frames]
    frames = align_frames(frames, canvas, anchor=BOTTOM_CENTER)

    # -- 3. 共用一套调色板 --------------------------------------------------
    #
    # 各帧各自量化，同一块布料在相邻帧里会落到不同色号上，播放时整个角色闪色。
    # 量化只用来**求**调色板；真正的像素映射交给 snap_to_palette。
    #
    # 关键是 snap 的对象必须是**未量化的** frames，不是 palette.frames ——
    # 后者已经过 Pillow 中位切分的内部映射，再 snap 一次是空操作。
    #
    # 为什么非要统一到最近色：补间拿到的只是一份既定调色板，没有原始像素分布
    # 可供中位切分参考，它只能用最近色。两边映射不同，补完之后关键帧会与导入
    # 产物差几十到几百个像素（实测 61 / 61 / 303）—— 而"关键帧原样保留"
    # 是这个功能的第一条硬约束。
    palette = quantize_frames(frames, request.style.max_colors)
    frames, _drift = snap_to_palette(frames, palette.colors)
    frames = cleanup_frames(frames)
    frames = [zero_transparent_rgb(frame) for frame in frames]
    drift = anchor_drift(frames, anchor=BOTTOM_CENTER)

    # -- 4. 落盘 ------------------------------------------------------------
    #
    # 存进 source/ 的必须是**合成到键控色之后的原始输入**，不是抠完背景的 RGBA。
    # source/ 在本项目里的含义处处都是"永不覆盖的原始输入"，存派生物会让下游
    # 按"原图是纯色背景"的假设去读它 —— 补间就踩过：RGBA 转 RGB 把透明变成黑，
    # 键控找到 0% 背景，整幅图都算前景，裁剪框形同虚设。
    for index, (path, rgb) in enumerate(zip(paths, flat, strict=True)):
        buffer = io.BytesIO()
        Image.fromarray(rgb).save(buffer, format="PNG")
        store.write_source(f"{key}-key{index:02d}", buffer.getvalue())
        logger.info("关键帧 %d ← %s", index, path.name)

    frame_paths = save_frames(frames, store.frames_of(key), stem=key)
    sheet, _ = compose_spritesheet(frames)
    sheet_path = save_png(sheet, store.sheets / f"{key}.png")

    spec = next((a for a in request.animation_list() if a.name == action), None)
    fps = source_fps or (spec.fps if spec else 6)
    is_loop = loop if loop is not None else (spec.loop if spec else True)

    budget: FrameBudget | None = None
    if target_fps is not None or target_frames is not None:
        budget = plan_inbetweens(
            len(frames),
            source_fps=fps,
            target_fps=target_fps or fps,
            loop=is_loop,
            target_frames=target_frames,
        )
        warnings.append(f"补间预算：{budget.describe()}（尚未生成，见 6.8.3）")

    try:
        save_gif(frames, store.previews / f"{key}.gif", fps=fps, loop=is_loop)
    except Exception as exc:  # pragma: no cover - 预览失败不该阻断导入
        logger.warning("生成 %s 预览 GIF 失败：%s", key, exc)

    manifest = ensure_manifest(
        store, request, background, provider_name=config.provider, model=config.model
    )
    # fps 记的是**磁盘上这几张帧**的帧率，不是补间后的目标帧率。
    # Manifest 描述事实：现在盘上就是 3 张 @3fps，写成 9 会让后续的
    # interpolate 以为已经补过了（"已经是 3 帧，不需要补间"），当场卡住。
    manifest.animations[key] = GeneratedAnimation(
        fps=fps,
        loop=is_loop,
        source_image=None,
        frames=[str(p.relative_to(store.root)) for p in frame_paths],
        keyframe_count=len(frames),
        keyframe_fps=fps,
    )
    manifest.sheets[key] = str(sheet_path.relative_to(store.root))
    manifest.palette.colors = palette.colors
    manifest.status = "processed"
    manifest.save(store.manifest_path)

    return KeyframeImport(
        asset_id=request.asset_id,
        key=key,
        keyframe_paths=paths,
        frame_paths=frame_paths,
        canvas=canvas,
        palette=palette.colors,
        anchor_drift_px=drift,
        budget=budget,
        warnings=warnings,
    )


def load_manifest_palette(store: ArtifactStore) -> list[str]:
    """取资产已确立的调色板。补间生成的中间帧必须锁死到它。"""
    if not store.manifest_path.exists():
        raise ProcessingError(f"{store.root} 下没有 Manifest —— 先导入关键帧")
    return list(AssetManifest.load(store.manifest_path).palette.colors)
