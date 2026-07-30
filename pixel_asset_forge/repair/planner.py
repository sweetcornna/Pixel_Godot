"""Repair Planner —— 把验证失败项翻译成修复动作（PLAN §9.3）。

两条原则，顺序不能反：

1. **优先选不调 API 的本地修复。** 能离线解决的就离线解决 ——
   重生成既慢又花钱，而且因为生成层不可复现，重生成出来的是**另一张图**，
   连"这次改动有没有效"都判断不了。
2. **只重生成最小失败单元。** 一个动作坏了就重生成那一个动作，
   不要把整个资产推倒重来。只有身份基准（seed）坏了才级联作废下游。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..models.validation import (
    LOCALLY_REPAIRABLE,
    REQUIRES_REGENERATION,
    CheckResult,
    Severity,
    ValidationReport,
)


class RepairAction(StrEnum):
    REPROCESS = "reprocess"
    """重跑本地确定性处理链。**不调用 API。**"""

    REGENERATE_GRID = "regenerate_grid"
    """重生成当前动作网格。调用 API。"""

    REGENERATE_SEED = "regenerate_seed"
    """重生成 canonical seed，级联作废全部下游。调用 API。"""

    MANUAL = "manual"
    """无法自动修复，需人工介入。"""


#: 各动作是否需要 API 调用。这张表是"能离线就离线"这条规则的落地点。
CALLS_API: dict[RepairAction, bool] = {
    RepairAction.REPROCESS: False,
    RepairAction.REGENERATE_GRID: True,
    RepairAction.REGENERATE_SEED: True,
    RepairAction.MANUAL: False,
}


@dataclass(frozen=True, slots=True)
class RepairStep:
    target: str
    action: RepairAction
    reasons: tuple[str, ...]
    severity: Severity
    detail: str

    @property
    def calls_api(self) -> bool:
        return CALLS_API[self.action]

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "action": self.action.value,
            "calls_api": self.calls_api,
            "reasons": list(self.reasons),
            "severity": self.severity.value,
            "detail": self.detail,
        }


@dataclass
class RepairPlan:
    asset_id: str
    steps: list[RepairStep] = field(default_factory=list)
    rounds_used: int = 0
    max_rounds: int = 2
    exhausted: bool = False

    @property
    def api_calls(self) -> int:
        return sum(1 for s in self.steps if s.calls_api)

    @property
    def empty(self) -> bool:
        return not self.steps

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "rounds_used": self.rounds_used,
            "max_rounds": self.max_rounds,
            "exhausted": self.exhausted,
            "estimated_api_calls": self.api_calls,
            "steps": [s.to_dict() for s in self.steps],
        }


def plan_repairs(
    report: ValidationReport,
    *,
    rounds_used: int = 0,
    max_rounds: int = 2,
) -> RepairPlan:
    """按验证报告生成修复计划。

    同一个 target 同时有本地问题与重生成问题时，只保留重生成 ——
    重生成会重跑整条处理链，本地修复是它的子集，再单列一步是白做。
    """
    plan = RepairPlan(asset_id=report.asset_id, rounds_used=rounds_used, max_rounds=max_rounds)

    if rounds_used >= max_rounds:
        plan.exhausted = True
        return plan

    local: dict[str, list[tuple[str, Severity, str]]] = {}
    regenerate: dict[str, list[tuple[str, Severity, str]]] = {}

    for check in report.checks:
        if check.result is not CheckResult.FAIL:
            continue
        record = (check.id, check.severity, check.message or "")
        if check.id in LOCALLY_REPAIRABLE:
            local.setdefault(check.target, []).append(record)
        elif check.id in REQUIRES_REGENERATION:
            regenerate.setdefault(check.target, []).append(record)
        elif check.blocking:
            # 阻断放行却没有对应动作 —— 必须让人看见。
            plan.steps.append(
                RepairStep(
                    target=check.target,
                    action=RepairAction.MANUAL,
                    reasons=(check.id,),
                    severity=check.severity,
                    detail=check.message or "没有对应的自动修复动作",
                )
            )
        # 其余（medium / low 且无对应动作）不进修复计划。
        #
        # 它们多半是几何类变化量超了 per-action 阈值 —— 而阈值**尚未用真实数据
        # 校准**（PLAN §9.1）。对着未校准的阈值提修复建议就是对着噪声行动，
        # 只会让修复计划失去信噪比。它们仍然出现在验证报告里供人判断。

    # 本地修复排在前面：它不花钱，先跑完再决定要不要花钱重生成。
    for target in sorted(local):
        if target in regenerate:
            continue  # 重生成会覆盖本地修复
        records = local[target]
        plan.steps.append(
            RepairStep(
                target=target,
                action=RepairAction.REPROCESS,
                reasons=tuple(r[0] for r in records),
                severity=max((r[1] for r in records), key=_severity_rank),
                detail="离线重跑处理链即可，不需要重新生成",
            )
        )

    for target in sorted(regenerate):
        records = regenerate[target]
        severity = max((r[1] for r in records), key=_severity_rank)
        reasons = tuple(r[0] for r in records)

        if _regeneration_is_futile(records):
            plan.steps.append(
                RepairStep(
                    target=target,
                    action=RepairAction.MANUAL,
                    reasons=reasons,
                    severity=severity,
                    detail=FUTILE_REGENERATION_DETAIL,
                )
            )
            continue

        plan.steps.append(
            RepairStep(
                target=target,
                action=RepairAction.REGENERATE_GRID,
                reasons=reasons,
                severity=severity,
                detail="构图本身错了，本地补不回被切掉的像素 —— 必须重生成整个动作网格",
            )
        )

    return plan


#: 越界报告里出现这个片段，说明根因是端点改了长短边比。
_ASPECT_MARKER = "越界高风险"

FUTILE_REGENERATION_DETAIL = (
    "越界的根因是端点把长短边比改掉了，格子被压扁 —— **重生成不会解决它**。"
    "实测同一个 prompt 连续三次都返回同样的错误比例，三次都越界。"
    "请改用别的帧数档位（换一个网格形状），或改用该端点会如实返回的尺寸比例。"
)


def _regeneration_is_futile(records: list[tuple[str, Severity, str]]) -> bool:
    """判断"重生成"这一步是不是注定白跑。

    只针对一种已实测的情形：越界是由**长短边比被端点改掉**引起的。
    这种情况下模型每次都按方格构图、每次都拿到压扁的格子，重摇多少次都一样，
    只会把 ``max_repair_rounds`` 烧光然后判 failed。

    与其推荐一个注定失败的动作，不如直说"换个网格形状"。
    """
    return any(
        check_id == "cell_overflow" and _ASPECT_MARKER in message
        for check_id, _severity, message in records
    )


_SEVERITY_ORDER = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.FATAL: 3,
}


def _severity_rank(severity: Severity) -> int:
    return _SEVERITY_ORDER[severity]
