"""确定性处理链 —— 从原始网格图到成品帧（PLAN §3）。

```text
越界检测 → 固定网格切分 → 色键去背景 → Despill
→ 自动裁剪 → 统一缩放 → Bottom-center 对齐
→ 调色板量化 → 像素清理 → Spritesheet 重组
```

整条链是**纯像素运算，100% 确定性**：同一张输入图 + 同一组参数，
必然产出同一批字节。这是 ``pixel-asset process`` 能离线重跑的全部依据，
也是 golden image 测试能覆盖这一层的原因。

顺序上有两处不能调换：

- **despill 必须在量化之前**：溢色一旦被量化固化，就成了调色板里的一档。
- **裁剪必须整组共用一个框**：逐帧各裁各的等于给每帧施加不同平移，
  角色会在帧间跳动。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..constants import DEFAULT_TARGET_SIZE
from ..logging_utils import get_logger
from ..planning.grid_layout import GridLayout
from .anchor import BOTTOM_CENTER, Anchor, align_frames, anchor_drift
from .bounds import OverflowReport, detect_overflow
from .chroma_key import apply_chroma_key, hex_to_rgb, zero_transparent_rgb
from .component_split import SplitMethod, SplitResult, split_frames
from .crop import ContentBox, crop_all
from .despill import despill
from .frame_split import assert_uniform_size, normalize_cell_sizes
from .palette import PaletteResult, quantize_frames
from .pixel_cleanup import cleanup_frames
from .pixel_grid import (
    KEY_RESIDUE_WARN_RATIO,
    GridSnap,
    resolution_warning,
    snap_rgba_to_grid,
    strip_key_residue,
)
from .resize import nearest_resize
from .scale_profile import ScaleProfile, clamp_warning, scale_for, uneven_upscale

logger = get_logger("processing.pipeline")


@dataclass
class ProcessOptions:
    key_color: str = "#FF00FF"
    key_threshold: float | None = None
    """None 表示逐图自动求解。``process`` 重跑时必须传入 Manifest 里记录的值。"""

    target_size: tuple[int, int] = DEFAULT_TARGET_SIZE
    max_colors: int = 24
    anchor: Anchor = BOTTOM_CENTER
    crop_padding: int = 0
    dither: bool = False
    cleanup_isolated: bool = True

    split_method: SplitMethod = SplitMethod.AUTO
    """抽帧方式。默认按连通域定位 sprite，而不是按格线硬切（ADR-003 修订）。"""

    snap_pixel_grid: bool = True
    """把"块边缘发虚的像素画"吸附回模型自己的像素网格。

    **这是产出可用像素资产的前提。** 不做的话，最近邻下采样会采到块的软边缘，
    输出是一团读不出轮廓的泥（见 pixel_grid 模块）。
    """

    grid_block_size: float | None = None
    """显式块大小。``process`` 离线重跑必须传入 Manifest 记录的值才能复现。"""

    scale_profile: ScaleProfile | None = None
    """跨动作缩放基准。None 表示本动作就是参考动作，按等比填满画布。

    传入基准后，本动作按基准推算输出大小 —— 蹲伏、倒地这类**真实的**
    姿势尺寸差异才不会被归一化掉。
    """


@dataclass
class ProcessResult:
    frames: list[np.ndarray]
    overflow: OverflowReport
    key_threshold: float
    background_ratio: float
    palette: PaletteResult
    content_box: ContentBox
    source_size: tuple[int, int]
    cell_size: tuple[int, int]
    anchor_drift_px: float
    content_source_height: int = 0
    """缩放前的内容高度（源像素）。导出 scale profile 时要用。"""
    source_cell_height: int = 0
    """源图网格的单元格高度。scale profile 的分母，必须与 sprite 大小无关。"""
    grid_snap: GridSnap | None = None
    """像素网格吸附结果。``block_size`` 必须写进 Manifest 才能离线复现。"""
    output_content_height: int = 0
    split: SplitResult | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def frame_size(self) -> tuple[int, int]:
        height, width = self.frames[0].shape[:2]
        return (width, height)


def process_grid(
    image: np.ndarray,
    layout: GridLayout,
    options: ProcessOptions | None = None,
) -> ProcessResult:
    """把一张动作网格图处理成一组成品帧。"""
    opts = options or ProcessOptions()
    warnings: list[str] = []

    rgb = image[:, :, :3] if image.ndim == 3 else image
    source_size = (rgb.shape[1], rgb.shape[0])
    key = hex_to_rgb(opts.key_color)

    snap: GridSnap | None = None

    # 1. 键控 —— 先在整图上做，越界检测需要完整的前景掩膜
    keyed = apply_chroma_key(rgb, key, threshold=opts.key_threshold)
    if keyed.suspicious:
        warnings.append(
            f"键控后背景占比 {keyed.background_ratio:.1%} 异常，阈值可能不可信"
        )

    # 2. 吸附到模型自己的像素网格 —— **必须在键控之后**。
    #
    # gpt-image-2 画的是"块边缘发虚的像素画"（实测块约 8.5px、整图 6 万色）。
    # 不还原成块级像素而直接下采样，采样点落在软边缘上，输出是读不出轮廓的泥。
    #
    # 放在键控之后：跨角色边缘的块只统计不透明像素取色，键控色在结构上没有机会
    # 污染前景。反过来做会在角色身上留下刺眼的洋红斑点（真实发生过）。
    keyed_rgba = keyed.rgba
    if opts.snap_pixel_grid:
        snap = snap_rgba_to_grid(keyed_rgba, block_size=opts.grid_block_size)
        if snap.applied:
            keyed_rgba = snap.image
        else:
            warnings.append(f"像素网格{snap.summary()} —— 按原始分辨率处理")

    keyed_rgba, residue = strip_key_residue(keyed_rgba, key)
    # 只有比例高到真的可疑才告警。删掉的大头是被角色围住的背景区域
    # （两腿之间、弓的弯里），那是正常情形 —— 每次都报会训练用户忽略告警。
    if residue > KEY_RESIDUE_WARN_RATIO:
        warnings.append(
            f"前景里有 {residue:.1%} 的像素过近键控色，已删除 —— "
            "比例这么高通常意味着角色配色与键控色撞了，"
            "换个 background.color 或加 fallback_colors"
        )

    foreground = keyed_rgba[:, :, 3] > 0

    # 3. 格线越界 —— 仅作**参考信息**，不再是失败判据。
    #
    # 实测：一张 8 个姿势彼此完全分离、毫无损伤的产出，会因为整体相对假想格线
    # 偏移而被判出 3 个"跨格连通域"。跨不跨格线衡量的是"布局是否符合我的假设"，
    # 不是"sprite 有没有被切坏" —— 后者才是要拦的（ADR-003 修订）。
    overflow = detect_overflow(foreground, layout)

    # 4. 抽帧：按连通域定位 sprite，格线只在退化路径上用到
    split = split_frames(keyed_rgba, layout, method=opts.split_method)
    frames = normalize_cell_sizes(split.frames)
    cell_size = assert_uniform_size(frames)

    if split.method is not SplitMethod.COMPONENTS:
        warnings.append(
            f"连通域抽帧未成功，退回 {split.method.value}：姿势可能粘连成一片。"
            f"（格线检查：{overflow.summary()}）"
        )
    elif split.overlapping_pairs:
        warnings.append(
            f"{split.overlapping_pairs} 对 sprite 的包围盒互相重叠 —— "
            "姿势挤得很近，抽帧仍然正确但构图偏紧"
        )

    # 4. Despill —— 必须早于量化
    frames = [despill(f, key) for f in frames]

    # 5. 整组共用一个裁剪框
    frames, box = crop_all(frames, padding=opts.crop_padding)

    # 6. 等比缩放 + 锚点对齐
    #
    # 缩放系数对整组帧**统一**：逐帧各自 fit-to-canvas 会让每帧比例不同，
    # 播放起来角色一大一小地跳。跨动作则由 scale_profile 统一（见该模块）。
    content_h, content_w = frames[0].shape[:2]

    # 分母必须是**源图网格的单元格高度**，不是抽帧后的视口高度 ——
    # 视口本身就随 sprite 大小伸缩，用它做分母比值恒等于 1、不携带任何信息，
    # scale profile 就退化成"每个动作各自填满画布"，等于没做。
    #
    # 而且要用**吸附之后**的高度：content_h 量的是吸附后的逻辑像素，拿吸附前的
    # 全分辨率高度当分母，比值会整体差一个块大小（实测 0.187 而非 0.80）。
    # 块大小是逐图检测的，不保证各动作一致 —— 单位不统一的比值不能跨动作比较。
    source_cell_height = max(1, keyed_rgba.shape[0] // layout.rows)
    scale = scale_for(
        opts.scale_profile,
        content_size=(content_w, content_h),
        cell_height=source_cell_height,
        canvas=opts.target_size,
    )
    fit = min(opts.target_size[0] / content_w, opts.target_size[1] / content_h)
    wanted = fit
    if opts.scale_profile is not None:
        wanted = opts.scale_profile.target_height(
            subject_ratio=content_h / max(1, source_cell_height),
            canvas_height=opts.target_size[1],
        ) / max(1, content_h)

    scaled_size = (max(1, round(content_w * scale)), max(1, round(content_h * scale)))
    frames = [nearest_resize(f, scaled_size) for f in frames]

    clamp_note = clamp_warning("本动作", wanted, scale)
    if clamp_note:
        warnings.append(clamp_note)
    if uneven_upscale(scale):
        warnings.append(
            f"本动作被放大 {scale:.2f}× —— 非整数倍放大会让一部分块占 1 个输出像素、"
            f"一部分占 2 个，像素等宽被打回参差。模型原生高度 {content_h}px，"
            f"把 target_size 设成 {content_h} 或 {content_h * 2} 就能整数倍放大。"
            "（跨动作大小一致优先，所以这里不自动取整，只告警。）"
        )

    frames = align_frames(frames, opts.target_size, anchor=opts.anchor)

    # 7. 量化（整组共用调色板）
    palette = quantize_frames(frames, opts.max_colors, dither=opts.dither)
    frames = palette.frames

    # 8. 像素清理
    if opts.cleanup_isolated:
        frames = cleanup_frames(frames)

    # 9. 兜底：透明像素 RGB 必须为零（致命级验证项）
    frames = [zero_transparent_rgb(f) for f in frames]

    drift = anchor_drift(frames, anchor=opts.anchor)

    ys = np.nonzero(frames[0][:, :, 3])[0]
    output_height = int(ys.max() - ys.min() + 1) if ys.size else 0

    # 目标尺寸是否把模型产出丢掉太多 —— 这是"资产不可用"最常见的单一原因。
    native_note = resolution_warning(content_h, opts.target_size[1])
    if native_note:
        warnings.append(native_note)

    return ProcessResult(
        frames=frames,
        overflow=overflow,
        key_threshold=keyed.threshold,
        background_ratio=keyed.background_ratio,
        palette=palette,
        content_box=box,
        source_size=source_size,
        cell_size=cell_size,
        anchor_drift_px=drift,
        content_source_height=content_h,
        source_cell_height=source_cell_height,
        grid_snap=snap,
        output_content_height=output_height,
        split=split,
        warnings=warnings,
    )


def process_seed(
    image: np.ndarray,
    options: ProcessOptions | None = None,
) -> ProcessResult:
    """处理单幅种子图。等价于 1×1 网格。"""
    opts = options or ProcessOptions()
    single = GridLayout(frames=1, cols=1, rows=1, cell=(image.shape[1], image.shape[0]))
    return process_grid(image, single, opts)
