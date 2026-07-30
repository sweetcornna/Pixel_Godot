"""验证引擎（PLAN §9）。

**验证失败时绝不把资产标记为成功。** ``ValidationReport.passed`` 由 checks 推导。
"""

from .engine import validate_animation, validate_asset, validate_derived
from .frame_order import FrameOrderStats, measure_frame_order
from .metrics import (
    AnchorMeasurement,
    anchor_measurement,
    exact_duplicates,
    height_variation,
    is_blank,
    silhouette_variation,
    transparent_rgb_residue,
)

__all__ = [
    "AnchorMeasurement",
    "FrameOrderStats",
    "anchor_measurement",
    "exact_duplicates",
    "height_variation",
    "is_blank",
    "measure_frame_order",
    "silhouette_variation",
    "transparent_rgb_residue",
    "validate_animation",
    "validate_asset",
    "validate_derived",
]
