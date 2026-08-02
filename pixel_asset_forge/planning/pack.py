"""Pack 规划汇总 —— 每个展开资产仍保留独立的任务表。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..models.job import JobKind, JobStatus, JobTable
from ..models.pack import StaticAssetPack, input_fingerprint
from .planner import PlanResult, plan_request


@dataclass(frozen=True, slots=True)
class PackPlanResult:
    pack: StaticAssetPack
    assets: tuple[PlanResult, ...]

    @property
    def estimated_api_calls(self) -> int:
        return sum(result.estimated_api_calls for result in self.assets)

    @property
    def estimated_seed_api_calls(self) -> int:
        return sum(
            1
            for result in self.assets
            for job in result.jobs
            if job.kind is JobKind.SEED
            and job.status in (JobStatus.PLANNED, JobStatus.GENERATING)
        )

    @property
    def estimated_animation_api_calls(self) -> int:
        return sum(
            1
            for result in self.assets
            for job in result.jobs
            if job.kind is JobKind.ANIMATION
            and job.status in (JobStatus.PLANNED, JobStatus.GENERATING)
        )

    @property
    def total_jobs(self) -> int:
        return sum(len(result.jobs) for result in self.assets)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack_id": self.pack.pack_id,
            "pack_type": self.pack.pack_type,
            "estimated_api_calls": self.estimated_api_calls,
            "estimated_seed_api_calls": self.estimated_seed_api_calls,
            "estimated_animation_api_calls": self.estimated_animation_api_calls,
            "total_jobs": self.total_jobs,
            "assets": [result.to_dict() for result in self.assets],
        }


def plan_pack(
    pack: StaticAssetPack,
    *,
    existing: Mapping[str, JobTable] | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> PackPlanResult:
    """逐资产规划；调用方可按 ``asset_id`` 提供既有任务表。"""
    existing_by_asset = existing or {}
    results_list: list[PlanResult] = []
    for request in pack.expand_requests():
        result = plan_request(
            request,
            existing=existing_by_asset.get(request.asset_id),
            provider=provider,
            model=model,
        )
        seed = result.jobs.seed_job
        if seed is not None:
            fingerprint = input_fingerprint(
                request,
                provider or "<default-provider>",
                model or "<default-model>",
            )
            if (
                seed.input_fingerprint is not None
                and seed.input_fingerprint != fingerprint
            ):
                raise ValueError(
                    f"任务 {seed.id} 的 input_fingerprint 冲突："
                    f"既有 {seed.input_fingerprint}，新规划 {fingerprint}"
                )
            seed.input_fingerprint = fingerprint
        results_list.append(result)
    results = tuple(results_list)
    return PackPlanResult(pack=pack, assets=results)
