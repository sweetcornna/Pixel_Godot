"""Pack 规划汇总 —— 每个展开资产仍保留独立的任务表。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..models.job import JobTable
from ..models.pack import PotionPack
from .planner import PlanResult, plan_request


@dataclass(frozen=True, slots=True)
class PackPlanResult:
    pack: PotionPack
    assets: tuple[PlanResult, ...]

    @property
    def estimated_api_calls(self) -> int:
        return sum(result.estimated_api_calls for result in self.assets)

    @property
    def total_jobs(self) -> int:
        return sum(len(result.jobs) for result in self.assets)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack_id": self.pack.pack_id,
            "pack_type": self.pack.pack_type,
            "estimated_api_calls": self.estimated_api_calls,
            "total_jobs": self.total_jobs,
            "assets": [result.to_dict() for result in self.assets],
        }


def plan_pack(
    pack: PotionPack,
    *,
    existing: Mapping[str, JobTable] | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> PackPlanResult:
    """逐资产规划；调用方可按 ``asset_id`` 提供既有任务表。"""
    existing_by_asset = existing or {}
    results = tuple(
        plan_request(
            request,
            existing=existing_by_asset.get(request.asset_id),
            provider=provider,
            model=model,
        )
        for request in pack.expand_requests()
    )
    return PackPlanResult(pack=pack, assets=results)
