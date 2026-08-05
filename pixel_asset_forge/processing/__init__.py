"""确定性图像处理层（PLAN §3 / Sprint 3）。

除背景冲突**预检**（纯文本/颜色推理，生成前就要用到）之外，
本包全部是纯像素运算：同一输入必得同一输出，golden image 测试可完全覆盖。
"""

from .anchor import (
    BOTTOM_CENTER,
    CENTER,
    Anchor,
    align_frames,
    anchor_drift,
    content_anchor,
    place_on_canvas,
)
from .background import BackgroundDecision, resolve_key_color
from .bounds import OverflowReport, detect_overflow
from .chroma_key import (
    KeyResult,
    apply_chroma_key,
    background_mask,
    color_distance,
    hex_to_rgb,
    otsu_threshold,
    zero_transparent_rgb,
)
from .component_split import (
    SplitMethod,
    SplitResult,
    group_components,
    split_frames,
)
from .crop import ContentBox, content_bounds, crop, crop_all, union_bounds
from .despill import despill, spill_ratio
from .frame_split import (
    assert_uniform_size,
    center_crop_to_grid,
    normalize_cell_sizes,
    split_grid,
)
from .palette import (
    PaletteResult,
    extract_palette,
    palette_overflow_ratio,
    quantize_frames,
)
from .pipeline import ProcessOptions, ProcessResult, process_grid, process_seed
from .pixel_cleanup import cleanup_frames, count_isolated, remove_isolated_pixels
from .resize import (
    block_median_resize,
    fit_within,
    introduces_new_colors,
    nearest_resize,
    resize_to_fit,
)
from .scale_profile import ScaleProfile, derive_profile, scale_for
from .spritesheet import (
    SheetLayout,
    compose_spritesheet,
    contact_sheet,
    save_frames,
    save_gif,
    save_png,
)

__all__ = [
    "BOTTOM_CENTER",
    "CENTER",
    "Anchor",
    "BackgroundDecision",
    "ContentBox",
    "KeyResult",
    "OverflowReport",
    "PaletteResult",
    "ProcessOptions",
    "ProcessResult",
    "ScaleProfile",
    "SheetLayout",
    "SplitMethod",
    "SplitResult",
    "align_frames",
    "anchor_drift",
    "apply_chroma_key",
    "assert_uniform_size",
    "background_mask",
    "block_median_resize",
    "center_crop_to_grid",
    "cleanup_frames",
    "color_distance",
    "compose_spritesheet",
    "contact_sheet",
    "content_anchor",
    "content_bounds",
    "count_isolated",
    "crop",
    "crop_all",
    "derive_profile",
    "despill",
    "detect_overflow",
    "extract_palette",
    "fit_within",
    "group_components",
    "hex_to_rgb",
    "introduces_new_colors",
    "nearest_resize",
    "normalize_cell_sizes",
    "otsu_threshold",
    "palette_overflow_ratio",
    "place_on_canvas",
    "process_grid",
    "process_seed",
    "quantize_frames",
    "remove_isolated_pixels",
    "resize_to_fit",
    "resolve_key_color",
    "save_frames",
    "save_gif",
    "save_png",
    "scale_for",
    "spill_ratio",
    "split_frames",
    "split_grid",
    "union_bounds",
    "zero_transparent_rgb",
]
