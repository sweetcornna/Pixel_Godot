"""Planner —— 把 Asset Request 编译为任务 DAG。

三条设计约束：

- **纯函数。** 同一请求必然产出同一批任务 ID，不含时间戳、不含随机数。
  这是"重复执行不创建重复任务"（Sprint 1 退出门槛）的实现方式。
- **不调用 API、不碰磁盘。** ``plan`` 命令必须能在用户没有 Key 的情况下跑。
- **把要花钱的地方摆到台面上。** 输出必须包含预计 API 调用次数 ——
  用户在按下执行键之前有权知道这一趟要生成什么、要花多少次调用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..constants import (
    ACTION_DEFAULTS,
    GENERATION_ORDER,
    MIRROR_PAIR,
    Direction,
)
from ..models.job import Job, JobKind, JobStatus, JobTable, make_job_id
from ..models.pack import input_fingerprint
from ..models.request import STATIC_ASSET_TYPES, AnimationSpec, AssetRequest
from ..processing.background import BackgroundDecision, resolve_key_color
from .grid_layout import GridLayout, grid_for_frames, seed_layout

# 会破坏左右对称的物件。误判镜像的代价特别高：角色被镜像成左撇子，
# **而且这个错误能通过全部自动验证项** —— 几何检查对左右手完全不敏感。
# plan 阶段的这条告警是唯一的自动拦截机会。
_ASYMMETRY_KEYWORDS = (
    "sword", "shield", "axe", "bow", "staff", "wand", "spear", "lance",
    "hammer", "dagger", "quiver", "eyepatch", "satchel", "holster",
    "剑", "盾", "斧", "弓", "法杖", "矛", "锤", "匕首", "箭袋", "眼罩",
)

_NEGATIONS = ("no ", "without ", "not ", "无", "没有", "不带")


def _mentions_asymmetry(text: str) -> tuple[str, ...]:
    """找出描述里暗示非对称的词，跳过被否定的（"no shield" 不算命中）。"""
    lowered = text.lower()
    hits: list[str] = []
    for keyword in _ASYMMETRY_KEYWORDS:
        for match in re.finditer(re.escape(keyword.lower()), lowered):
            prefix = lowered[max(0, match.start() - 12) : match.start()]
            if any(neg in prefix for neg in _NEGATIONS):
                continue
            hits.append(keyword)
            break
    return tuple(hits)


@dataclass(frozen=True, slots=True)
class PlannedAnimation:
    """一个动作在 plan 输出里的呈现形态。"""

    action: str
    directions: tuple[Direction | None, ...]
    frames: int
    fps: int
    loop: bool
    layout: GridLayout
    generated: tuple[str, ...]
    derived: tuple[tuple[str, str], ...]
    """``(目标方向, 源方向)`` 对。"""


@dataclass
class PlanResult:
    request: AssetRequest
    jobs: JobTable
    background: BackgroundDecision
    animations: tuple[PlannedAnimation, ...] = ()
    warnings: list[str] = field(default_factory=list)

    @property
    def estimated_api_calls(self) -> int:
        return self.jobs.estimated_api_calls()

    def to_dict(self) -> dict[str, Any]:
        """``plan --json`` 的输出结构。"""
        return {
            "asset_id": self.request.asset_id,
            "asset_type": self.request.asset_type,
            "signature": self.jobs.signature(),
            "estimated_api_calls": self.estimated_api_calls,
            "total_jobs": len(self.jobs),
            "background": {
                "mode": self.request.background.mode,
                "color_requested": self.background.color_requested,
                "color_used": self.background.color_used,
                "fallback_stage": self.background.fallback_stage,
                "downgraded": self.background.downgraded,
            },
            "mirroring": {
                "enabled": self.request.mirroring_enabled,
                "source_direction": self.request.mirror_source,
                "strict_lighting": self.request.style.strict_lighting,
            },
            "animations": [
                {
                    "action": a.action,
                    "frames": a.frames,
                    "fps": a.fps,
                    "loop": a.loop,
                    "grid": {"cols": a.layout.cols, "rows": a.layout.rows},
                    "physical_size": list(a.layout.size),
                    "generated": list(a.generated),
                    "derived": [{"direction": d, "from": s} for d, s in a.derived],
                }
                for a in self.animations
            ],
            "jobs": [
                {
                    "id": job.id,
                    "kind": job.kind.value,
                    "status": job.status.value,
                    "key": job.key,
                    "calls_api": job.calls_api,
                    "depends_on": list(job.depends_on),
                    "derived_from": job.derived_from,
                    "physical_size": list(job.physical_size) if job.physical_size else None,
                }
                for job in self.jobs.topological_order()
            ],
            "warnings": list(self.warnings),
        }


def _direction_sort_key(direction: Direction | None) -> int:
    """按优先试错顺序排：down → left → up → right（PLAN §8）。

    ``up``（背面）身份一致性最难，应尽早暴露而不是留到最后。
    """
    if direction is None:
        return -1
    return GENERATION_ORDER.index(direction)


def _plan_animation(
    request: AssetRequest,
    spec: AnimationSpec,
    seed_id: str,
    table: JobTable,
    warnings: list[str],
) -> PlannedAnimation:
    layout = grid_for_frames(spec.frames)
    directions = spec.resolved_directions(request.asset_type)

    ordered: tuple[Direction | None, ...]
    ordered = tuple(sorted(directions, key=_direction_sort_key)) if directions else (None,)

    mirror_source: Direction | None = None
    mirror_target: Direction | None = None
    if request.mirroring_enabled and directions:
        source = request.mirror_source
        target = MIRROR_PAIR[source]
        # 只有源方向也在请求里时才可能 derive —— 没有源就没得翻。
        if source in directions and target in directions:
            mirror_source, mirror_target = source, target
        elif target in directions and source not in directions:
            warnings.append(
                f"{spec.name}：已启用镜像但只请求了 {target} 方向，"
                f"没有 {source} 可作为镜像源，因此 {target} 仍独立生成。"
            )

    generated: list[str] = []
    derived: list[tuple[str, str]] = []

    for direction in ordered:
        job_id = make_job_id(request.asset_id, JobKind.ANIMATION, spec.name, direction)

        if direction is not None and direction == mirror_target and mirror_source is not None:
            source_id = make_job_id(
                request.asset_id, JobKind.ANIMATION, spec.name, mirror_source
            )
            table.add(
                Job(
                    id=job_id,
                    asset_id=request.asset_id,
                    kind=JobKind.DERIVED,
                    action=spec.name,
                    direction=direction,
                    frames=spec.frames,
                    fps=spec.fps,
                    loop=spec.loop,
                    derived_from=f"{spec.name}_{mirror_source}",
                    transform="flip_horizontal",
                    depends_on=(source_id,),
                )
            )
            derived.append((str(direction), str(mirror_source)))
        else:
            table.add(
                Job(
                    id=job_id,
                    asset_id=request.asset_id,
                    kind=JobKind.ANIMATION,
                    action=spec.name,
                    direction=direction,
                    frames=spec.frames,
                    fps=spec.fps,
                    loop=spec.loop,
                    grid=(layout.cols, layout.rows),
                    physical_size=layout.size,
                    depends_on=(seed_id,),
                )
            )
            generated.append(direction or spec.name)

    return PlannedAnimation(
        action=spec.name,
        directions=ordered,
        frames=spec.frames,
        fps=spec.fps,
        loop=spec.loop,
        layout=layout,
        generated=tuple(generated),
        derived=tuple(derived),
    )


def plan_request(
    request: AssetRequest,
    existing: JobTable | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> PlanResult:
    """把请求编译为任务 DAG。

    传入 ``existing`` 时按 ID 合并 —— 已完成的任务保留其状态，不会被打回 planned。
    这是断点续跑的基础。静态任务的 provider/model 会参与输入指纹；当前未传时使用
    稳定占位值，后续 CLI/pipeline 应传入实际后端。
    """
    table = existing if existing is not None else JobTable(asset_id=request.asset_id)
    if table.asset_id != request.asset_id:
        raise ValueError(
            f"任务表属于 {table.asset_id}，无法并入 {request.asset_id} 的规划结果"
        )

    warnings: list[str] = []

    background = resolve_key_color(
        request.description,
        requested=request.background.color,
        fallbacks=request.background.fallback_colors,
        conflict_hint=request.background.conflict_hint,
        palette=request.style.palette_colors or (),
    )
    if background.downgraded:
        warnings.append(background.explain())

    animation_specs = request.animation_list()
    animations: tuple[PlannedAnimation, ...] = ()

    if request.asset_type in STATIC_ASSET_TYPES and not animation_specs:
        static_id = make_job_id(request.asset_id, JobKind.STATIC)
        static_layout = seed_layout()
        fingerprint = input_fingerprint(
            request,
            provider or "<default-provider>",
            model or "<default-model>",
        )
        table.add(
            Job(
                id=static_id,
                asset_id=request.asset_id,
                kind=JobKind.STATIC,
                physical_size=static_layout.size,
                input_fingerprint=fingerprint,
            )
        )
    else:
        # 种子图：所有动画的身份基准，也是唯一的人工闸门。
        seed_id = make_job_id(request.asset_id, JobKind.SEED)
        seed_grid = seed_layout()
        table.add(
            Job(
                id=seed_id,
                asset_id=request.asset_id,
                kind=JobKind.SEED,
                physical_size=seed_grid.size,
            )
        )

        animations = tuple(
            _plan_animation(request, spec, seed_id, table, warnings)
            for spec in animation_specs
        )

    _collect_advisories(request, animations, warnings)

    return PlanResult(
        request=request,
        jobs=table,
        background=background,
        animations=animations,
        warnings=warnings,
    )


def _collect_advisories(
    request: AssetRequest,
    animations: tuple[PlannedAnimation, ...],
    warnings: list[str],
) -> None:
    if request.style.strict_lighting and request.mirroring and request.mirroring.enabled:
        warnings.append(
            "style.strict_lighting=true 覆盖了 mirroring.enabled=true："
            "四个方向将全部独立生成，左右方向存在身份漂移风险（ADR-006）。"
        )

    if request.mirroring_enabled:
        hits = _mentions_asymmetry(request.description)
        if hits:
            warnings.append(
                f"镜像已启用，但描述中出现非对称物件：{'、'.join(hits)}。"
                "镜像会把角色变成左撇子，且该错误能通过全部自动验证项 —— "
                "请人工确认 mirroring.enabled 是否应为 false。"
            )

    if request.asset_type == "character" and not animations:
        warnings.append("character 资产没有声明任何动作，只会产出种子图。")

    for planned in animations:
        defaults = ACTION_DEFAULTS.get(planned.action)
        if defaults and planned.loop != defaults.loop:
            warnings.append(
                f"{planned.action}：loop={planned.loop} 与默认值 {defaults.loop} 不同。"
                + (
                    "loop=false 会关闭帧序连续性检查，帧序乱掉将无法被自动发现（PLAN §9.2）。"
                    if not planned.loop
                    else "loop=true 会启用帧序连续性检查；非循环动作可能因本身有突变而误报。"
                )
            )


def pending_api_jobs(table: JobTable) -> tuple[Job, ...]:
    """尚未完成、且需要 API 调用的任务。用于成本预估与断点续跑。"""
    return tuple(
        job
        for job in table.topological_order()
        if job.calls_api and job.status in (JobStatus.PLANNED, JobStatus.GENERATING)
    )
