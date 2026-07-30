"""引擎导出器。

所有导出文件必须能仅凭 Manifest + ``frames/`` 重建（ADR-001）。
"""

from __future__ import annotations

from ..errors import ExportError
from .base import AnimationView, Exporter, ExportResult, animation_views
from .generic_json import GenericJsonExporter
from .godot import GodotExporter

EXPORTERS: dict[str, type[Exporter]] = {
    GenericJsonExporter.target: GenericJsonExporter,
    GodotExporter.target: GodotExporter,
}


def get_exporter(target: str) -> Exporter:
    cls = EXPORTERS.get(target)
    if cls is None:
        raise ExportError(
            f"未知导出目标：{target}。可选：{', '.join(sorted(EXPORTERS))}。"
            "（phaser / tiled 排期在 Sprint 7 与 Sprint 8）"
        )
    return cls()


__all__ = [
    "EXPORTERS",
    "AnimationView",
    "ExportResult",
    "Exporter",
    "GenericJsonExporter",
    "GodotExporter",
    "animation_views",
    "get_exporter",
]
