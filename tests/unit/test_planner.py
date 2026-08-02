"""Planner 与任务 DAG。

Sprint 1 的三条退出门槛都落在这里：
每个任务有唯一 ID · 同一请求重复执行不创建重复任务 · plan 能输出预计调用次数与依赖关系。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pixel_asset_forge.models import load_pack, load_request, parse_request
from pixel_asset_forge.models.job import JobKind, JobStatus, JobTable
from pixel_asset_forge.planning import plan_pack, plan_request


def test_job_ids_are_unique(examples_dir: Path) -> None:
    result = plan_request(load_request(examples_dir / "knight.yaml"))
    ids = [job.id for job in result.jobs]
    assert len(ids) == len(set(ids))


def test_knight_needs_nine_api_calls(examples_dir: Path) -> None:
    # 不可镜像 → seed + (idle + walk) × 4 方向 = 9 次。
    result = plan_request(load_request(examples_dir / "knight.yaml"))
    assert result.estimated_api_calls == 9
    assert len(result.jobs) == 9


def test_mirroring_removes_two_api_calls(examples_dir: Path) -> None:
    # slime 可镜像 → right 由 left 翻转 derive，两次调用被省掉。
    result = plan_request(load_request(examples_dir / "slime.yaml"))
    assert len(result.jobs) == 9
    assert result.estimated_api_calls == 7
    derived = result.jobs.of_kind(JobKind.DERIVED)
    assert {j.key for j in derived} == {"idle_right", "walk_right"}
    for job in derived:
        assert job.transform == "flip_horizontal"
        assert job.calls_api is False


def test_derived_job_depends_on_its_mirror_source(examples_dir: Path) -> None:
    result = plan_request(load_request(examples_dir / "slime.yaml"))
    right = result.jobs.get("slime_01:walk:right")
    assert right.depends_on == ("slime_01:walk:left",)
    assert right.derived_from == "walk_left"


def test_every_animation_depends_on_the_seed(examples_dir: Path) -> None:
    """seed 是所有动画的身份基准 —— 它没批准前一个动画都不该开跑。"""
    result = plan_request(load_request(examples_dir / "knight.yaml"))
    seed = result.jobs.seed_job
    assert seed is not None
    for job in result.jobs:
        if job.kind is JobKind.ANIMATION:
            assert seed.id in job.depends_on


def test_only_the_seed_is_ready_before_approval(examples_dir: Path) -> None:
    result = plan_request(load_request(examples_dir / "knight.yaml"))
    ready = result.jobs.ready_jobs()
    assert [j.kind for j in ready] == [JobKind.SEED]


def test_replanning_is_idempotent(examples_dir: Path) -> None:
    request = load_request(examples_dir / "knight.yaml")
    first = plan_request(request)
    signature, count = first.jobs.signature(), len(first.jobs)

    second = plan_request(request, existing=first.jobs)
    assert len(second.jobs) == count
    assert second.jobs.signature() == signature


def test_replanning_preserves_completed_work(examples_dir: Path) -> None:
    """断点续跑：重新 plan 不能把已完成的任务打回 planned。"""
    request = load_request(examples_dir / "knight.yaml")
    table = plan_request(request).jobs
    table.get("knight_01:seed").status = JobStatus.APPROVED

    replanned = plan_request(request, existing=table)
    assert replanned.jobs.get("knight_01:seed").status is JobStatus.APPROVED
    assert replanned.estimated_api_calls == 8  # seed 已完成，不再计费


def test_plan_is_a_pure_function(examples_dir: Path) -> None:
    """同一请求必须产出同一批任务 —— 不含时间戳、不含随机数。"""
    request = load_request(examples_dir / "knight.yaml")
    a = plan_request(request, existing=JobTable(asset_id=request.asset_id))
    b = plan_request(request, existing=JobTable(asset_id=request.asset_id))
    assert a.jobs.signature() == b.jobs.signature()
    assert a.to_dict()["jobs"] == b.to_dict()["jobs"]


def test_topological_order_puts_dependencies_first(examples_dir: Path) -> None:
    result = plan_request(load_request(examples_dir / "slime.yaml"))
    order = [job.id for job in result.jobs.topological_order()]
    for job in result.jobs:
        for dep in job.depends_on:
            assert order.index(dep) < order.index(job.id)


def test_generation_order_puts_up_before_right(examples_dir: Path) -> None:
    """``up``（背面）身份一致性最难，应尽早试错而非留到最后（PLAN §8）。"""
    result = plan_request(load_request(examples_dir / "knight.yaml"))
    walk = next(a for a in result.animations if a.action == "walk")
    assert walk.generated == ("down", "left", "up", "right")


def test_projectile_impact_becomes_a_single_directionless_job(examples_dir: Path) -> None:
    result = plan_request(load_request(examples_dir / "fireball.yaml"))
    impact = [j for j in result.jobs if j.action == "impact"]
    assert len(impact) == 1
    assert impact[0].direction is None
    assert impact[0].key == "impact"


def test_asymmetry_warning_when_mirroring_a_sword_carrier(minimal_request: dict) -> None:
    """误判镜像的错误能通过全部自动验证项 —— plan 阶段是唯一的自动拦截机会。"""
    minimal_request["mirroring"] = {"enabled": True}
    minimal_request["description"] = "A knight holding a short sword in the right hand."
    result = plan_request(parse_request(minimal_request))
    assert any("sword" in w for w in result.warnings)


def test_no_asymmetry_warning_for_negated_items(minimal_request: dict) -> None:
    minimal_request["mirroring"] = {"enabled": True}
    minimal_request["description"] = "A round symmetrical blob, no sword, no shield."
    result = plan_request(parse_request(minimal_request))
    assert not any("sword" in w for w in result.warnings)


def test_strict_lighting_conflict_is_surfaced(minimal_request: dict) -> None:
    minimal_request["mirroring"] = {"enabled": True}
    minimal_request["style"]["strict_lighting"] = True
    result = plan_request(parse_request(minimal_request))
    assert any("strict_lighting" in w for w in result.warnings)
    assert result.jobs.of_kind(JobKind.DERIVED) == ()


def test_mirroring_without_source_direction_falls_back_to_generation(
    minimal_request: dict,
) -> None:
    """只请求了 right、没请求 left —— 没有源就没得翻，必须独立生成并告警。"""
    minimal_request["mirroring"] = {"enabled": True, "source_direction": "left"}
    minimal_request["animations"][0]["directions"] = ["down", "right"]
    result = plan_request(parse_request(minimal_request))
    assert result.jobs.of_kind(JobKind.DERIVED) == ()
    assert any("镜像源" in w for w in result.warnings)


def test_plan_dict_reports_cost_and_dependencies(examples_dir: Path) -> None:
    payload = plan_request(load_request(examples_dir / "slime.yaml")).to_dict()
    assert payload["estimated_api_calls"] == 7
    assert payload["background"]["color_used"] == "#00FF00"
    assert payload["background"]["downgraded"] is True
    assert all("depends_on" in job for job in payload["jobs"])


def test_merging_into_a_foreign_table_is_rejected(examples_dir: Path) -> None:
    request = load_request(examples_dir / "knight.yaml")
    with pytest.raises(ValueError):
        plan_request(request, existing=JobTable(asset_id="someone_else"))


def test_static_pickup_has_one_static_job_and_no_seed(examples_dir: Path) -> None:
    request = load_pack(examples_dir / "potion_pack.yaml").expand_requests()[0]
    result = plan_request(request, provider="openai", model="gpt-image-2")

    jobs = tuple(result.jobs)
    assert len(jobs) == 1
    assert jobs[0].id == "health_potion:static"
    assert jobs[0].kind is JobKind.STATIC
    assert jobs[0].calls_api is True
    assert jobs[0].input_fingerprint is not None
    assert result.jobs.of_kind(JobKind.SEED) == ()
    assert result.animations == ()
    assert result.estimated_api_calls == 1
    assert result.to_dict()["animations"] == []


def test_static_weapon_has_one_static_job_and_no_seed(examples_dir: Path) -> None:
    request = load_pack(examples_dir / "potion_pack.yaml").expand_requests()[0].model_copy(
        update={"asset_id": "iron_sword", "asset_type": "weapon"}
    )
    result = plan_request(request, provider="openai", model="gpt-image-2")

    jobs = tuple(result.jobs)
    assert len(jobs) == 1
    assert jobs[0].id == "iron_sword:static"
    assert jobs[0].kind is JobKind.STATIC
    assert jobs[0].calls_api is True
    assert jobs[0].input_fingerprint is not None
    assert result.jobs.of_kind(JobKind.SEED) == ()
    assert result.animations == ()
    assert result.estimated_api_calls == 1


def test_static_planner_avoids_palette_key_color(examples_dir: Path) -> None:
    request = load_pack(examples_dir / "potion_pack.yaml").expand_requests()[0]
    colliding = request.model_copy(
        update={
            "style": request.style.model_copy(
                update={"palette_colors": ("#FF00FF", "#211A2C")}
            )
        }
    )
    result = plan_request(colliding)
    assert result.background.color_used == "#00FF00"


def test_animated_non_pickup_keeps_seed_behavior(examples_dir: Path) -> None:
    result = plan_request(load_request(examples_dir / "fireball.yaml"))
    assert result.jobs.seed_job is not None
    assert result.jobs.of_kind(JobKind.STATIC) == ()


def test_pack_plan_sums_assets_and_accepts_existing_mapping(examples_dir: Path) -> None:
    pack = load_pack(examples_dir / "potion_pack.yaml")
    first = plan_pack(pack, provider="openai", model="gpt-image-2")
    existing = {result.request.asset_id: result.jobs for result in first.assets}
    second = plan_pack(
        pack,
        existing=existing,
        provider="openai",
        model="gpt-image-2",
    )

    assert first.estimated_api_calls == 3
    assert first.total_jobs == 3
    assert second.to_dict()["assets"][0]["asset_id"] == "health_potion"


def test_spell_bundle_plan_separates_seed_and_animation_calls(examples_dir: Path) -> None:
    result = plan_pack(
        load_pack(examples_dir / "spell_bundle.yaml"),
        provider="mock",
        model="mock-image",
    )
    payload = result.to_dict()

    assert result.estimated_seed_api_calls == 3
    assert result.estimated_animation_api_calls == 12
    assert result.estimated_api_calls == 15
    assert result.total_jobs == 15
    assert payload["estimated_seed_api_calls"] == 3
    assert payload["estimated_animation_api_calls"] == 12
    assert all(len(asset.jobs.of_kind(JobKind.SEED)) == 1 for asset in result.assets)
    assert all(len(asset.jobs.of_kind(JobKind.ANIMATION)) == 4 for asset in result.assets)


def test_combat_bundle_plan_separates_seed_and_animation_calls(examples_dir: Path) -> None:
    result = plan_pack(
        load_pack(examples_dir / "combat_bundle.yaml"),
        provider="mock",
        model="mock-image",
    )
    payload = result.to_dict()

    assert result.estimated_seed_api_calls == 1
    assert result.estimated_animation_api_calls == 12
    assert result.estimated_api_calls == 13
    assert result.total_jobs == 13
    assert payload["estimated_seed_api_calls"] == 1
    assert payload["estimated_animation_api_calls"] == 12
    assert len(result.assets[0].jobs.of_kind(JobKind.SEED)) == 1
    assert len(result.assets[0].jobs.of_kind(JobKind.ANIMATION)) == 12
