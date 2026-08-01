"""Job 状态机（PLAN §5.3）。

核心断言：**非法转移必须报错，不能静默容忍。**
一个允许任意跳转的状态机等于没有状态机 —— 那样"验证失败绝不标记为成功"
这条规则就没有任何结构性保障。
"""

from __future__ import annotations

import pytest

from pixel_asset_forge.errors import StateTransitionError
from pixel_asset_forge.models.job import (
    TERMINAL_STATES,
    Job,
    JobEvent,
    JobKind,
    JobStatus,
    JobTable,
    make_job_id,
)


def make_job(kind: JobKind = JobKind.ANIMATION, **kwargs) -> Job:
    return Job(
        id=kwargs.pop("id", "a:walk:down"),
        asset_id="a",
        kind=kind,
        action=kwargs.pop("action", "walk"),
        direction=kwargs.pop("direction", "down"),
        **kwargs,
    )


def test_happy_path_reaches_exported() -> None:
    job = make_job()
    for event in (
        JobEvent.START_EXECUTION,
        JobEvent.PROVIDER_SUCCESS,
        JobEvent.START_PROCESSING,
        JobEvent.PROCESSING_DONE,
        JobEvent.START_VALIDATION,
        JobEvent.VALIDATION_PASSED,
        JobEvent.EXPORT,
    ):
        job.fire(event)
    assert job.status is JobStatus.EXPORTED
    assert job.is_terminal


def test_cache_hit_skips_the_api_call() -> None:
    job = make_job()
    transition = job.fire(JobEvent.CACHE_HIT)
    assert job.status is JobStatus.GENERATED
    assert transition.calls_api is False


def test_static_job_calls_the_api_and_has_static_id() -> None:
    job = Job(
        id=make_job_id("potion", JobKind.STATIC),
        asset_id="potion",
        kind=JobKind.STATIC,
    )
    assert job.id == "potion:static"
    assert job.key == "static"
    assert job.calls_api is True


def test_derived_job_never_calls_the_api() -> None:
    job = make_job(JobKind.DERIVED, derived_from="walk_left", transform="flip_horizontal")
    assert job.calls_api is False
    job.fire(JobEvent.DERIVE_READY)
    assert job.status is JobStatus.GENERATED


def test_illegal_transition_raises() -> None:
    job = make_job()
    # planned 状态下不存在"导出"这回事。
    with pytest.raises(StateTransitionError) as exc:
        job.fire(JobEvent.EXPORT)
    assert exc.value.current == "planned"
    assert exc.value.event == "export"


def test_cannot_skip_validation_on_the_way_to_export() -> None:
    """"不要跳过 validate 直接交付"这条规则必须由状态机强制，而不是靠自觉。"""
    job = make_job()
    job.fire(JobEvent.START_EXECUTION)
    job.fire(JobEvent.PROVIDER_SUCCESS)
    job.fire(JobEvent.START_PROCESSING)
    job.fire(JobEvent.PROCESSING_DONE)
    with pytest.raises(StateTransitionError):
        job.fire(JobEvent.EXPORT)


def test_validation_failed_cannot_be_exported() -> None:
    job = make_job(status=JobStatus.VALIDATING)
    job.fire(JobEvent.VALIDATION_FAILED)
    with pytest.raises(StateTransitionError):
        job.fire(JobEvent.EXPORT)


def test_transient_errors_self_loop_and_count_attempts() -> None:
    job = make_job()
    job.fire(JobEvent.START_EXECUTION)
    for _ in range(3):
        job.fire(JobEvent.TRANSIENT_ERROR)
    assert job.status is JobStatus.GENERATING
    assert job.attempts == 4  # 1 次首发 + 3 次重试


def test_permanent_error_is_terminal_and_records_the_reason() -> None:
    job = make_job()
    job.fire(JobEvent.START_EXECUTION)
    job.fire(JobEvent.PERMANENT_ERROR, detail="moderation blocked")
    assert job.status is JobStatus.FAILED
    assert job.error == "moderation blocked"
    assert job.status in TERMINAL_STATES


def test_repair_rounds_are_counted() -> None:
    job = make_job(status=JobStatus.VALIDATION_FAILED)
    job.fire(JobEvent.PLAN_REPAIR)
    assert job.repair_rounds == 1


def test_local_repair_does_not_call_the_api() -> None:
    """能离线解决的就离线解决 —— 本地修复不该产生任何计费调用。"""
    job = make_job(status=JobStatus.REPAIRING)
    transition = job.fire(JobEvent.REPAIR_LOCAL)
    assert job.status is JobStatus.PROCESSING
    assert transition.calls_api is False


def test_grid_regeneration_calls_the_api_and_resets_attempts() -> None:
    job = make_job(status=JobStatus.REPAIRING, attempts=3)
    transition = job.fire(JobEvent.REPAIR_REGENERATE_GRID)
    assert job.status is JobStatus.GENERATING
    assert transition.calls_api is True
    assert job.attempts == 0


def test_history_records_every_transition() -> None:
    job = make_job()
    job.fire(JobEvent.START_EXECUTION)
    job.fire(JobEvent.PROVIDER_SUCCESS)
    assert [r.event for r in job.history] == [
        JobEvent.START_EXECUTION,
        JobEvent.PROVIDER_SUCCESS,
    ]
    assert job.history[0].from_status is JobStatus.PLANNED


# -- 级联作废 -------------------------------------------------------------


def build_table() -> JobTable:
    table = JobTable(asset_id="a")
    seed_id = make_job_id("a", JobKind.SEED)
    table.add(Job(id=seed_id, asset_id="a", kind=JobKind.SEED))
    left = make_job_id("a", JobKind.ANIMATION, "walk", "left")
    table.add(
        Job(id=left, asset_id="a", kind=JobKind.ANIMATION, action="walk",
            direction="left", depends_on=(seed_id,))
    )
    table.add(
        Job(id=make_job_id("a", JobKind.ANIMATION, "walk", "right"), asset_id="a",
            kind=JobKind.DERIVED, action="walk", direction="right",
            derived_from="walk_left", transform="flip_horizontal", depends_on=(left,))
    )
    return table


def test_regenerating_the_seed_invalidates_the_whole_downstream() -> None:
    """seed 是身份基准；它一废，所有下游动画都必须重来（PLAN §5.3）。"""
    table = build_table()
    for job in table:
        if job.kind is not JobKind.SEED:
            job.status = JobStatus.VALIDATED

    affected = table.cascade_invalidate("a:seed", reason="seed 重生成")
    assert {j.id for j in affected} == {"a:walk:left", "a:walk:right"}
    assert all(j.status is JobStatus.PLANNED for j in affected)


def test_cascade_reaches_transitively_through_derived_jobs() -> None:
    table = build_table()
    affected = table.transitive_dependents_of("a:walk:left")
    assert [j.id for j in affected] == ["a:walk:right"]


def test_seed_rejection_is_marked_as_cascading() -> None:
    job = Job(id="a:seed", asset_id="a", kind=JobKind.SEED,
              status=JobStatus.AWAITING_APPROVAL)
    transition = job.fire(JobEvent.REJECT)
    assert job.status is JobStatus.PLANNED
    assert transition.cascades is True


def test_job_table_add_is_idempotent() -> None:
    table = build_table()
    before = len(table)
    table.add(Job(id="a:seed", asset_id="a", kind=JobKind.SEED))
    assert len(table) == before


def test_job_table_rejects_same_id_with_different_fingerprints() -> None:
    table = JobTable(asset_id="potion")
    table.add(
        Job(
            id="potion:static",
            asset_id="potion",
            kind=JobKind.STATIC,
            input_fingerprint="a" * 64,
        )
    )
    with pytest.raises(ValueError, match="input_fingerprint 冲突"):
        table.add(
            Job(
                id="potion:static",
                asset_id="potion",
                kind=JobKind.STATIC,
                input_fingerprint="b" * 64,
            )
        )


def test_job_table_still_reuses_when_either_fingerprint_is_missing() -> None:
    table = JobTable(asset_id="potion")
    existing = Job(id="potion:static", asset_id="potion", kind=JobKind.STATIC)
    assert table.add(existing) is existing
    assert table.add(
        Job(
            id="potion:static",
            asset_id="potion",
            kind=JobKind.STATIC,
            input_fingerprint="b" * 64,
        )
    ) is existing


def test_cycle_detection() -> None:
    table = JobTable(asset_id="a")
    table.add(Job(id="x", asset_id="a", kind=JobKind.ANIMATION, depends_on=("y",)))
    table.add(Job(id="y", asset_id="a", kind=JobKind.ANIMATION, depends_on=("x",)))
    with pytest.raises(ValueError, match="环"):
        table.topological_order()
