"""Sprint 1 的头号退出门槛：**不调用真实 API 即可走完整工作流。**

意义在于"整个流水线的骨架在花第一分钱之前就已经验证过"（ADR-002）。

这里手工驱动 DAG —— 正式的编排器在 Sprint 4 落地（``pipelines/``），
Sprint 3 的处理链与 Sprint 5 的验证引擎也还没有。因此本测试覆盖的是
**骨架能否拼起来**：规划 → 生成 → 落盘 → 状态推进 → 人工闸门 → 缓存 → 级联作废。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pixel_asset_forge.config import Config
from pixel_asset_forge.models import load_request
from pixel_asset_forge.models.job import Job, JobEvent, JobKind, JobStatus
from pixel_asset_forge.models.manifest import (
    AssetManifest,
    BackgroundInfo,
    CanvasInfo,
    DerivedAnimation,
    GeneratedAnimation,
    GridInfo,
    PaletteInfo,
    ProviderInfo,
)
from pixel_asset_forge.planning import grid_for_frames, plan_request, seed_layout
from pixel_asset_forge.providers import (
    MockImageProvider,
    ReferenceImage,
    build_backend,
    get_provider,
)
from pixel_asset_forge.storage import ArtifactStore, GenerationCache


def compile_prompt(job: Job, key_color: str) -> str:
    """占位 prompt 编译器 —— 真正的实现是 Sprint 4 的 ``prompts/compiler.py``。

    这里只保留会影响 Mock 与验证行为的那几条约束（网格、帧数、键控色）。
    """
    if job.kind is JobKind.SEED:
        return f"canonical seed, full body, solid {key_color} background"
    cols, rows = job.grid or (1, 1)
    return (
        f"{job.action} cycle facing {job.direction}, arranged in a {cols}x{rows} grid, "
        f"exactly {job.frames} distinct poses, frames ordered left to right, top to bottom, "
        f"each pose fully inside its own cell, at least 8% margin around each pose, "
        f"solid {key_color} background"
    )


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(provider="mock", model="mock-image", output_dir=tmp_path / "outputs")


def test_full_offline_run_without_an_api_key(
    config: Config, examples_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PIXEL_ASSET_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # 只清环境变量不够：`Config.api_key()` 还会从 cwd 逐级向上找项目 `.env`，
    # 而开发机上 `pixel-asset init` 正好在仓库根写了一份带 Key 的 `.env`。
    # 不换到空目录，这个测试的前提就是"碰巧没人配过 Key"，CI 绿只是因为 CI 没有 `.env`。
    monkeypatch.chdir(tmp_path)
    assert Config.api_key() is None

    request = load_request(examples_dir / "slime.yaml")
    result = plan_request(request)
    table = result.jobs
    key_color = result.background.color_used

    # 这个用例关心的是 DAG 推进，不是缓存 —— 取裸后端，调用计数才好读。
    provider = build_backend(config)
    assert isinstance(provider, MockImageProvider)

    store = ArtifactStore.for_asset(config.output_dir, request.asset_id).ensure()
    cache = GenerationCache(tmp_path / "cache")

    # -- 1. 只有 seed 可以开跑：人工闸门挡住全部动画 -----------------------
    ready = table.ready_jobs()
    assert [j.kind for j in ready] == [JobKind.SEED]

    seed_job = ready[0]
    seed_job.fire(JobEvent.START_EXECUTION)
    seed_result = provider.generate(
        compile_prompt(seed_job, key_color), size=seed_layout().size
    )
    store.write_source(seed_job.key, seed_result.image)
    cache.put(seed_result.prompt_hash, seed_result.image, {"request_id": seed_result.request_id})
    store.append_generation_log(seed_result.log_entry())
    seed_job.fire(JobEvent.PROVIDER_SUCCESS)
    seed_job.prompt_hash = seed_result.prompt_hash

    for event in (
        JobEvent.START_PROCESSING,
        JobEvent.PROCESSING_DONE,
        JobEvent.START_VALIDATION,
        JobEvent.VALIDATION_PASSED,
        JobEvent.REQUIRE_APPROVAL,
    ):
        seed_job.fire(event)
    assert seed_job.status is JobStatus.AWAITING_APPROVAL
    # 闸门未放行时，动画任务依然不 ready
    assert table.ready_jobs() == ()

    seed_job.fire(JobEvent.APPROVE)
    assert seed_job.status is JobStatus.APPROVED

    # -- 2. seed 批准后，动画任务解锁 -------------------------------------
    unlocked = table.ready_jobs()
    assert {j.kind for j in unlocked} == {JobKind.ANIMATION}
    assert len(unlocked) == 6  # slime 可镜像：right 由 left 派生，不在此列

    seed_bytes = store.source_path("seed").read_bytes()
    animations: dict[str, GeneratedAnimation | DerivedAnimation] = {}

    for job in table.topological_order():
        if job.kind is JobKind.SEED or job.status is not JobStatus.PLANNED:
            continue

        if job.kind is JobKind.DERIVED:
            # 镜像派生不调用 API，也不产生原图。
            job.fire(JobEvent.DERIVE_READY)
            animations[job.key] = DerivedAnimation(
                derived_from=job.derived_from, transform=job.transform
            )
        else:
            job.fire(JobEvent.START_EXECUTION)
            layout = grid_for_frames(job.frames)
            blank = MockImageProvider  # 空白键控画布由 Sprint 3 的处理层生成
            del blank
            edit_result = provider.edit(
                compile_prompt(job, key_color),
                base_image=b"blank-keyed-canvas",
                size=layout.size,
                references=[ReferenceImage("seed", seed_bytes)],
            )
            store.write_source(job.key, edit_result.image)
            cache.put(edit_result.prompt_hash, edit_result.image, {})
            store.append_generation_log(edit_result.log_entry())
            job.fire(JobEvent.PROVIDER_SUCCESS)
            job.prompt_hash = edit_result.prompt_hash
            animations[job.key] = GeneratedAnimation(
                fps=job.fps,
                loop=job.loop,
                grid=GridInfo(cols=layout.cols, rows=layout.rows, cell=layout.cell),
                source_image=f"source/{store.source_path(job.key).name}",
                frames=[f"frames/{job.key}/{i:02d}.png" for i in range(job.frames)],
            )

        for event in (
            JobEvent.START_PROCESSING,
            JobEvent.PROCESSING_DONE,
            JobEvent.START_VALIDATION,
            JobEvent.VALIDATION_PASSED,
            JobEvent.EXPORT,
        ):
            job.fire(event)

    # -- 3. 全部任务抵达终态 ----------------------------------------------
    assert all(job.is_terminal for job in table)
    assert {j.status for j in table} == {JobStatus.APPROVED, JobStatus.EXPORTED}

    # -- 4. 只有需要生成的任务真的调了 Provider ---------------------------
    # seed 1 次 generate + 6 次 edit；两个镜像方向不计费。
    assert sum(1 for c in provider.calls if c["operation"] == "generate") == 1
    assert sum(1 for c in provider.calls if c["operation"] == "edit") == 6

    # -- 5. 原图落盘，且镜像方向没有原图 ----------------------------------
    sources = {p.name for p in store.iter_sources()}
    assert "seed-original.png" in sources
    assert "walk-left-original.png" in sources
    assert "walk-right-original.png" not in sources

    # -- 6. Manifest 可写、可读、且能重建导出所需的一切 --------------------
    manifest = AssetManifest(
        asset_id=request.asset_id,
        asset_type=request.asset_type,
        provider=ProviderInfo(name="mock", model=config.model),
        canvas=CanvasInfo(width=32, height=32),
        background=BackgroundInfo(
            mode=request.background.mode,
            color_requested=result.background.color_requested,
            color_used=result.background.color_used,
            fallback_stage=result.background.fallback_stage,
        ),
        palette=PaletteInfo(max_colors=request.style.max_colors, colors=[]),
        animations=animations,
        status="exported",
    )
    store.save_job_table(table)
    manifest.save(store.manifest_path)

    reloaded = AssetManifest.load(store.manifest_path)
    assert reloaded.background.color_used == "#00FF00"  # 冲突降级被持久化
    assert len(reloaded.resolve_frames("walk_right")) == request.animation_list()[1].frames
    assert store.load_job_table() is not None


def test_cache_hit_skips_the_api_call(config: Config, examples_dir: Path, tmp_path: Path) -> None:
    """"重复请求会命中 prompt hash 缓存，所以重跑失败任务是安全的"这条承诺必须成立。

    这条承诺一旦失灵，用户每次调试都在重复付费 —— 所以断言的是
    **底层后端的调用次数没有增加**，而不是"缓存里有东西"。
    """
    config = config.model_copy(update={"cache_dir": tmp_path / "cache"})
    request = load_request(examples_dir / "knight.yaml")
    table = plan_request(request).jobs

    provider = get_provider(config)
    backend = provider.inner.inner  # Caching(Throttled(Mock))
    assert isinstance(backend, MockImageProvider)

    job = table.get("knight_01:seed")
    prompt = compile_prompt(job, "#FF00FF")

    job.fire(JobEvent.START_EXECUTION)
    first = provider.generate(prompt, size=seed_layout().size)
    job.fire(JobEvent.PROVIDER_SUCCESS)
    assert first.cached is False
    assert len(backend.calls) == 1

    # 第二次同样的请求：命中缓存，一次 API 都不打。
    job.status = JobStatus.PLANNED
    second = provider.generate(prompt, size=seed_layout().size)
    job.fire(JobEvent.CACHE_HIT)

    assert job.status is JobStatus.GENERATED
    assert second.cached is True
    assert second.image == first.image
    assert second.prompt_hash == first.prompt_hash
    assert len(backend.calls) == 1, "缓存命中却仍然调用了后端"


def test_cache_miss_when_the_seed_changes(config: Config, tmp_path: Path) -> None:
    """换了 seed 就该 miss —— 否则不同角色会共用同一张动作网格。"""
    config = config.model_copy(update={"cache_dir": tmp_path / "cache"})
    provider = get_provider(config)
    backend = provider.inner.inner

    size = grid_for_frames(4).size
    prompt = "walk cycle, 2x2 grid, exactly 4 distinct poses, solid #FF00FF background"

    provider.edit(prompt, base_image=b"canvas", size=size,
                  references=[ReferenceImage("seed", b"seed-a")])
    provider.edit(prompt, base_image=b"canvas", size=size,
                  references=[ReferenceImage("seed", b"seed-b")])
    assert len(backend.calls) == 2


def test_rejecting_the_seed_invalidates_every_downstream_animation(
    config: Config, examples_dir: Path
) -> None:
    """seed 不对则后续生成的全部动画都要作废重来（PLAN §5.3）。"""
    table = plan_request(load_request(examples_dir / "knight.yaml")).jobs
    for job in table:
        job.status = JobStatus.VALIDATED

    seed = table.get("knight_01:seed")
    seed.status = JobStatus.AWAITING_APPROVAL
    transition = seed.fire(JobEvent.REJECT)
    assert transition.cascades is True

    affected = table.cascade_invalidate(seed.id, reason="seed 被拒绝")
    assert len(affected) == 8
    assert all(j.status is JobStatus.PLANNED for j in affected)
    assert table.estimated_api_calls() == 9  # 全部重来
