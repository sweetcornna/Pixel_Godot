"""Pydantic 数据模型。三份 JSON Schema 的运行时对应物。"""

from .job import Job, JobEvent, JobKind, JobStatus, JobTable
from .manifest import (
    AnimationEntry,
    AssetManifest,
    BackgroundInfo,
    DerivedAnimation,
    GeneratedAnimation,
    GridInfo,
    PaletteInfo,
)
from .request import (
    AnimationSpec,
    AssetRequest,
    BackgroundSpec,
    ExportSpec,
    MirroringSpec,
    StyleSpec,
    load_request,
    parse_request,
)
from .validation import Check, CheckId, CheckResult, Severity, ValidationReport

__all__ = [
    "AnimationEntry",
    "AnimationSpec",
    "AssetManifest",
    "AssetRequest",
    "BackgroundInfo",
    "BackgroundSpec",
    "Check",
    "CheckId",
    "CheckResult",
    "DerivedAnimation",
    "ExportSpec",
    "GeneratedAnimation",
    "GridInfo",
    "Job",
    "JobEvent",
    "JobKind",
    "JobStatus",
    "JobTable",
    "MirroringSpec",
    "PaletteInfo",
    "Severity",
    "StyleSpec",
    "ValidationReport",
    "load_request",
    "parse_request",
]
