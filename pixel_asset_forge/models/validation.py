"""Validation Report（PLAN §9 / schemas/validation-report.schema.json）。

一条不可退让的规则：**验证失败时绝不把资产标记为成功。**
``passed`` 由 ``checks`` 推导，不接受外部直接赋值 —— 否则总有一天会有人手动设成 True。
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .. import PIPELINE_VERSION, REPORT_SCHEMA_VERSION
from ..constants import (
    ACTION_THRESHOLDS,
    DIRECTION_MULTIPLIER,
    PALETTE_OVERFLOW_MAX,
    THRESHOLDS_CALIBRATED,
    Direction,
)
from ..schema_registry import validate_against

CheckId = Literal[
    "frame_count",
    "frame_size",
    "blank_frame",
    "cell_overflow",
    "transparent_rgb_residue",
    "frame_order_continuity",
    "anchor_drift",
    "height_variation",
    "silhouette_variation",
    "palette_overflow",
    "duplicate_frame_exact",
    "duplicate_frame_approx",
    "static_animation",
]

SkipReason = Literal[
    "derived_animation",
    "action_exempt",
    "custom_action_unthresholded",
    "non_looping_animation",
    "not_applicable",
    "dependency_failed",
]


class Severity(StrEnum):
    FATAL = "fatal"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class CheckResult(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


#: 各检查项的固有严重度（PLAN §9.2）。严重度是检查项的属性，不由调用方随意指定。
CHECK_SEVERITY: dict[CheckId, Severity] = {
    "frame_count": Severity.FATAL,
    "frame_size": Severity.FATAL,
    "blank_frame": Severity.FATAL,
    "cell_overflow": Severity.FATAL,
    "transparent_rgb_residue": Severity.FATAL,
    # 实测不可判定（见 validation/frame_order.py）。保留检查项是为了在报告里
    # 显式记录"这条防线是缺的"，而不是让它悄悄消失 —— 但绝不允许它阻断放行。
    "frame_order_continuity": Severity.LOW,
    "anchor_drift": Severity.HIGH,
    "height_variation": Severity.MEDIUM,
    "silhouette_variation": Severity.MEDIUM,
    "palette_overflow": Severity.MEDIUM,
    "duplicate_frame_exact": Severity.MEDIUM,
    "static_animation": Severity.MEDIUM,
    "duplicate_frame_approx": Severity.LOW,
}

#: 本地即可修复的检查项 —— 不需要重新调用 API（PLAN §9.3）。
LOCALLY_REPAIRABLE: frozenset[CheckId] = frozenset(
    {
        "transparent_rgb_residue",
        "palette_overflow",
        "anchor_drift",
        "frame_size",
    }
)

#: 只能靠重生成解决的检查项 —— 构图已经错了，本地补不回被切掉的像素。
REQUIRES_REGENERATION: frozenset[CheckId] = frozenset(
    {
        "cell_overflow",
        "frame_count",
        "frame_order_continuity",
        "blank_frame",
        "static_animation",
    }
)

BLOCKING_SEVERITIES: frozenset[Severity] = frozenset({Severity.FATAL, Severity.HIGH})


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: CheckId
    target: str
    action: str | None = None
    direction: Direction | None = None
    severity: Severity
    result: CheckResult
    measured: float | bool | None = None
    threshold: float | bool | None = None
    message: str | None = Field(default=None, max_length=500)
    skip_reason: SkipReason | None = None

    @classmethod
    def make(
        cls,
        check_id: CheckId,
        target: str,
        result: CheckResult,
        **kwargs: Any,
    ) -> Check:
        """按 :data:`CHECK_SEVERITY` 自动填严重度。"""
        return cls(id=check_id, target=target, result=result,
                   severity=CHECK_SEVERITY[check_id], **kwargs)

    @property
    def blocking(self) -> bool:
        return self.result is CheckResult.FAIL and self.severity in BLOCKING_SEVERITIES


Thresholds = dict[str, float | int | None]


def thresholds_for(action: str, direction: Direction | None = None) -> Thresholds:
    """查 per-action 阈值，并对 ``up`` 方向做 ×1.3 的轮廓类修正（PLAN §9.1）。

    锚点漂移是像素单位的绝对量，不参与方向修正 —— 背面再不稳定，脚也该踩在同一条线上。
    """
    base = ACTION_THRESHOLDS.get(action)
    if base is None:
        # 自定义动作没有 per-action 阈值 —— 我们不知道一个 dodge_roll 该有
        # 多大的高度变化，猜一个数只会产出一堆无意义的红叉或绿勾。
        # 几何检查跳过，但**必须让用户看见**（skip_reason 分开记，见调用方）。
        return {
            "height_variation_max": None,
            "silhouette_variation_max": None,
            "anchor_drift_max_px": None,
            "palette_overflow_max": PALETTE_OVERFLOW_MAX,
        }

    multiplier = DIRECTION_MULTIPLIER.get(direction, 1.0) if direction else 1.0
    out = base.as_dict()
    for field in ("height_variation_max", "silhouette_variation_max"):
        value = out[field]
        if value is not None:
            out[field] = round(float(value) * multiplier, 4)
    out["palette_overflow_max"] = PALETTE_OVERFLOW_MAX
    return out


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = REPORT_SCHEMA_VERSION
    asset_id: str
    pipeline_version: str = PIPELINE_VERSION
    thresholds_calibrated: bool = THRESHOLDS_CALIBRATED
    checks: list[Check] = Field(default_factory=list)
    thresholds_used: dict[str, dict[str, Any]] = Field(default_factory=dict)
    artifacts: dict[str, Any] = Field(default_factory=dict)

    # -- 结论 -------------------------------------------------------------

    @property
    def passed(self) -> bool:
        """任一 fatal / high 级别失败即为 False。"""
        return not any(c.blocking for c in self.checks)

    @property
    def blocking_checks(self) -> list[Check]:
        return [c for c in self.checks if c.blocking]

    def summary(self) -> dict[str, int]:
        return {
            "total": len(self.checks),
            "passed": sum(1 for c in self.checks if c.result is CheckResult.PASS),
            "failed": sum(1 for c in self.checks if c.result is CheckResult.FAIL),
            "warnings": sum(1 for c in self.checks if c.result is CheckResult.WARN),
            "skipped": sum(1 for c in self.checks if c.result is CheckResult.SKIP),
        }

    def repair_hint(self) -> dict[str, list[str]]:
        """把失败项分成"本地可修"与"必须重生成"两堆（PLAN §9.3）。

        先看本地能不能修 —— 能离线解决的就不要花钱重生成。
        """
        local: list[str] = []
        regenerate: list[str] = []
        for check in self.checks:
            if check.result is not CheckResult.FAIL:
                continue
            if check.id in LOCALLY_REPAIRABLE:
                local.append(check.target)
            elif check.id in REQUIRES_REGENERATION:
                regenerate.append(check.target)
        return {
            "local": sorted(set(local)),
            "regenerate": sorted(set(regenerate)),
        }

    # -- 序列化 -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude_none=True)
        data["passed"] = self.passed
        data["summary"] = self.summary()
        if not data.get("artifacts"):
            data.pop("artifacts", None)
        if not data.get("thresholds_used"):
            data.pop("thresholds_used", None)
        return data

    def save(self, path: str | Path) -> Path:
        payload = self.to_dict()
        validate_against("validation-report", payload, what=f"{self.asset_id} 的验证报告")
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return p
