"""Validation Report（PLAN §9 / schemas/validation-report.schema.json）。

一条不可退让的规则：**验证失败时绝不把资产标记为成功。**
``passed`` 由 ``checks`` 推导，不接受外部直接赋值 —— 否则总有一天会有人手动设成 True。
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .. import PIPELINE_VERSION, REPORT_SCHEMA_VERSION
from ..constants import (
    ACTION_THRESHOLDS,
    DIRECTION_MULTIPLIER,
    LOCOMOTION_THRESHOLDS,
    PALETTE_OVERFLOW_MAX,
    THRESHOLDS_CALIBRATED,
    Direction,
)
from ..schema_registry import validate_against
from ..storage.atomic import atomic_write_json

CheckId = Literal[
    "artifact_exists",
    "artifact_hash",
    "frame_count",
    "frame_size",
    "blank_frame",
    "cell_overflow",
    "content_bounds",
    "palette_membership",
    "transparent_rgb_residue",
    "partial_alpha",
    "isolated_pixel",
    "key_color_residue",
    "frame_order_continuity",
    "beat_signature",
    "mirror_flip",
    "anchor_drift",
    "height_variation",
    "silhouette_variation",
    "palette_overflow",
    "duplicate_frame_exact",
    "duplicate_frame_approx",
    "static_animation",
    "tile_seam",
    "tile_border",
    "tile_adjacency",
    "tile_terrain",
    "map_adjacency",
]

ALL_CHECK_IDS: tuple[CheckId, ...] = (
    "artifact_exists",
    "artifact_hash",
    "frame_count",
    "frame_size",
    "blank_frame",
    "cell_overflow",
    "content_bounds",
    "palette_membership",
    "transparent_rgb_residue",
    "partial_alpha",
    "isolated_pixel",
    "key_color_residue",
    "frame_order_continuity",
    "beat_signature",
    "mirror_flip",
    "anchor_drift",
    "height_variation",
    "silhouette_variation",
    "palette_overflow",
    "duplicate_frame_exact",
    "duplicate_frame_approx",
    "static_animation",
    "tile_seam",
    "tile_border",
    "tile_adjacency",
    "tile_terrain",
    "map_adjacency",
)

SkipReason = Literal[
    "derived_animation",
    "action_exempt",
    "custom_action_unthresholded",
    "non_looping_animation",
    "not_applicable",
    "dependency_failed",
    "static_asset",
    "guaranteed_by_construction",
]
"""``guaranteed_by_construction`` 与 ``not_applicable`` 不是一回事，别合并。

- ``not_applicable`` —— 这个检查对该目标**没有意义**（静态图查不了循环闭合）。
- ``guaranteed_by_construction`` —— 检查有意义，但**流水线自己把结论定死了**，
  它红不起来，因此那句 PASS 不携带任何信息。用户该做的事不同：前者不必再管，
  后者要知道"这一项其实没人在查"，并去看真正有判别力的那个检查。
"""


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
    "artifact_exists": Severity.FATAL,
    "artifact_hash": Severity.FATAL,
    "frame_count": Severity.FATAL,
    "frame_size": Severity.FATAL,
    "blank_frame": Severity.FATAL,
    "cell_overflow": Severity.FATAL,
    "content_bounds": Severity.HIGH,
    "palette_membership": Severity.HIGH,
    "transparent_rgb_residue": Severity.FATAL,
    "partial_alpha": Severity.HIGH,
    "isolated_pixel": Severity.MEDIUM,
    "key_color_residue": Severity.HIGH,
    # 实测不可判定（见 validation/frame_order.py）。保留检查项是为了在报告里
    # 显式记录"这条防线是缺的"，而不是让它悄悄消失 —— 但绝不允许它阻断放行。
    "frame_order_continuity": Severity.LOW,
    # 帧序问题上唯一被实测支撑的自动判据（见 validation/beat_signature.py）。
    #
    # 严重度定 MEDIUM 而不是 HIGH：判据本身很准（对正确产出零误报，
    # 对乱序的放行率 7.0% 已经贴着 6.7% 的组合学下限），但**只在一个真实样本上
    # 验证过**。等阈值校准（任务 #37）拿到更多数据再谈要不要升级成阻断项。
    "beat_signature": Severity.MEDIUM,
    # 播放时极刺眼，静态看单帧却完全正常 —— 每帧都是合格的角色，只是朝向反了。
    #
    # **MEDIUM 而不是 HIGH。** 逐图核对过：确有真阳性（法杖/剑换侧），
    # 也确有误报（正常的受击后仰被报出来）。真阳性 -0.03~-0.10、
    # 误报 -0.020~-0.024，两组只隔 0.006，而且判据在低手性角色
    # （方正的石魔、浑圆的史莱姆）上根本不成立。
    #
    # 报出来让人去看 contact sheet，但不阻断 —— 一个会误报的阻断项，
    # 最终一定会被开发者关掉（PLAN §9.1）。
    "mirror_flip": Severity.MEDIUM,
    "anchor_drift": Severity.HIGH,
    "height_variation": Severity.MEDIUM,
    "silhouette_variation": Severity.MEDIUM,
    "palette_overflow": Severity.MEDIUM,
    "duplicate_frame_exact": Severity.MEDIUM,
    "static_animation": Severity.MEDIUM,
    "duplicate_frame_approx": Severity.LOW,
    # tile 拼不起来就等于整套不可用 —— 平铺后每隔一格一道缝或一条网格线，
    # 是满屏可见的缺陷，不是"看看再说"。两条都定 FATAL。
    "tile_seam": Severity.FATAL,
    "tile_border": Severity.FATAL,
    # 邻接表与像素对不上不代表 tile 本身坏了 —— 坏了的话 tile_seam 会先炸。
    # 它说的是"Manifest 在描述一件与产物不符的事"：多半是产出后有人动过 tile 图，
    # 或阈值换过一版而表没重算。ADR-001 是 manifest-first，让它阻断放行，
    # 但不必与"整套 tile 不可用"同级。
    "tile_adjacency": Severity.HIGH,
    # 声明的角落地形与像素实测不符，后续 terrain bits 就会把错误一路放大到地图。
    # tile 像素本身仍可用，坏的是 Manifest 对它的语义描述，故与邻接漂移同为 HIGH。
    "tile_terrain": Severity.HIGH,
    # 一张违反邻接表的地图，接缝会满屏可见 —— 和 tile 本身拼不起来一样糟。
    # 而且它是**确定性算法的硬性质**：真的判失败就说明求解器有 bug，不是品味问题。
    "map_adjacency": Severity.FATAL,
}

#: 本地即可修复的检查项 —— 不需要重新调用 API（PLAN §9.3）。
LOCALLY_REPAIRABLE: frozenset[CheckId] = frozenset(
    {
        "transparent_rgb_residue",
        "palette_membership",
        "palette_overflow",
        "partial_alpha",
        "isolated_pixel",
        "anchor_drift",
        "frame_size",
    }
)

#: 只能靠重生成解决的检查项 —— 构图已经错了，本地补不回被切掉的像素。
REQUIRES_REGENERATION: frozenset[CheckId] = frozenset(
    {
        "artifact_exists",
        "artifact_hash",
        "cell_overflow",
        "content_bounds",
        "frame_count",
        "frame_order_continuity",
        "beat_signature",
        "mirror_flip",
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


def thresholds_for(
    action: str,
    direction: Direction | None = None,
    locomotion: str = "biped",
) -> Thresholds:
    """查 per-action 阈值，并对 ``up`` 方向做 ×1.3 的轮廓类修正（PLAN §9.1）。

    锚点漂移是像素单位的绝对量，不参与方向修正 —— 背面再不稳定，脚也该踩在同一条线上。

    ``locomotion`` 有专用阈值时优先（弹跳式走路的形变量级与双足完全不同，
    见 ``LOCOMOTION_THRESHOLDS``）。
    """
    base = LOCOMOTION_THRESHOLDS.get(locomotion, {}).get(action) or ACTION_THRESHOLDS.get(
        action
    )
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
        return atomic_write_json(path, payload)
