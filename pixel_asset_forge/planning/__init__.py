"""规划层：请求 → 任务 DAG。"""

from .framerate import FrameBudget, frame_order, plan_inbetweens
from .grid_layout import (
    GridLayout,
    SizeViolation,
    aspect_mismatch,
    check_size,
    grid_for_frames,
    layout_for_frames,
    layout_matching_cell,
    seed_layout,
    strip_for_frames,
    supported_batch_sizes,
)
from .pack import PackPlanResult, plan_pack
from .planner import PlanResult, plan_request

__all__ = [
    "FrameBudget",
    "GridLayout",
    "PackPlanResult",
    "PlanResult",
    "SizeViolation",
    "aspect_mismatch",
    "check_size",
    "frame_order",
    "grid_for_frames",
    "layout_for_frames",
    "layout_matching_cell",
    "plan_inbetweens",
    "plan_pack",
    "plan_request",
    "seed_layout",
    "strip_for_frames",
    "supported_batch_sizes",
]
