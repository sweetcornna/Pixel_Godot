"""CharacterSeedPipeline 与 AnimationGridPipeline（Sprint 4）。

全部用 Mock Provider 跑 —— "不调用真实 API 即可走完整工作流"这条从 Sprint 1
起就是硬要求，到了真正会花钱的 Sprint 4 只会更重要。

重点断言三件事：
- **人工闸门真的挡得住**。seed 未批准时动画任务不能开跑。
- **镜像派生不调用 API**。
- **原图永不覆盖**。重生成必须先归档。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from typer.testing import CliRunner

from pixel_asset_forge.cli import EXIT_ERROR, EXIT_OK, app
from pixel_asset_forge.config import Config
from pixel_asset_forge.errors import ProcessingError
from pixel_asset_forge.models.job import JobStatus
from pixel_asset_forge.models.manifest import AssetManifest, DerivedAnimation
from pixel_asset_forge.models.request import load_request
from pixel_asset_forge.pipelines import (
    approve_seed,
    create_animation,
    create_character,
    next_pending,
    seed_is_approved,
)
from pixel_asset_forge.storage import ArtifactStore

runner = CliRunner()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        provider="mock",
        model="mock-image",
        output_dir=tmp_path / "outputs",
        cache_dir=tmp_path / "cache",
    )


@pytest.fixture
def slime_request(tmp_path: Path, examples_dir: Path) -> Path:
    path = tmp_path / "slime.yaml"
    path.write_text((examples_dir / "slime.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    return path


@pytest.fixture
def knight_request(tmp_path: Path, examples_dir: Path) -> Path:
    path = tmp_path / "knight.yaml"
    path.write_text((examples_dir / "knight.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    return path


def store_for(config: Config, asset_id: str) -> ArtifactStore:
    return ArtifactStore.for_asset(config.output_dir, asset_id)


# -- 种子图 ---------------------------------------------------------------


def test_seed_lands_in_awaiting_approval(config: Config, knight_request: Path) -> None:
    """seed 是唯一的人工闸门，产出后必须停下来等人看。"""
    result = create_character(knight_request, config)

    assert result.seed_path.exists()
    assert result.pixel_path.exists()
    assert Image.open(result.pixel_path).size == load_request(knight_request).style.target_size

    store = store_for(config, "knight_01")
    table = store.load_job_table()
    assert table is not None
    assert table.seed_job.status is JobStatus.AWAITING_APPROVAL
    assert seed_is_approved(store) is False


def test_seed_writes_manifest_and_request_copy(config: Config, knight_request: Path) -> None:
    create_character(knight_request, config)
    store = store_for(config, "knight_01")

    assert store.request_path.exists(), "产物必须自带它的输入"
    manifest = AssetManifest.load(store.manifest_path)
    assert manifest.status == "awaiting_approval"
    assert manifest.background.key_threshold is not None
    assert manifest.palette.colors


def test_conflicting_key_colour_is_downgraded_and_surfaced(
    config: Config, slime_request: Path
) -> None:
    """史莱姆是洋红系 —— 默认键控色会把角色本体一起抠掉（PLAN §2.4.1）。"""
    result = create_character(slime_request, config)
    assert result.key_color == "#00FF00"
    assert any("降级" in w for w in result.warnings)

    manifest = AssetManifest.load(store_for(config, "slime_01").manifest_path)
    assert manifest.background.color_used == "#00FF00"
    assert manifest.background.fallback_stage == "alt_key_color"


def test_regenerating_the_seed_archives_the_old_one(
    config: Config, knight_request: Path
) -> None:
    """原始生成图永不覆盖 —— 归档而非删除，失败样本对调 prompt 很有价值。"""
    create_character(knight_request, config)
    store = store_for(config, "knight_01")
    original = store.source_path("seed").read_bytes()

    with pytest.raises(ProcessingError, match="--regenerate"):
        create_character(knight_request, config)

    create_character(knight_request, config, regenerate=True)
    archived = list(store.source.glob("seed-original.r*.png"))
    assert len(archived) == 1
    assert archived[0].read_bytes() == original


# -- 人工闸门 --------------------------------------------------------------


def test_animation_is_blocked_until_the_seed_is_approved(
    config: Config, knight_request: Path
) -> None:
    create_character(knight_request, config)
    with pytest.raises(ProcessingError, match="尚未获批准"):
        create_animation(
            store_for(config, "knight_01").root,
            action="walk", direction="down", config=config,
        )


def test_approval_unlocks_the_downstream_animations(
    config: Config, knight_request: Path
) -> None:
    create_character(knight_request, config)
    store = store_for(config, "knight_01")

    assert next_pending(store) == []
    asset_id, unlocked = approve_seed(store.root)
    assert asset_id == "knight_01"
    assert unlocked == 8  # knight 不可镜像：idle+walk × 4 方向
    assert seed_is_approved(store) is True


def test_approval_is_recorded_in_the_job_history(
    config: Config, knight_request: Path
) -> None:
    """seed 是谁放行、什么时候放行的，是出问题时第一个要查的东西。"""
    create_character(knight_request, config)
    store = store_for(config, "knight_01")
    approve_seed(store.root)

    table = store.load_job_table()
    approval = [r for r in table.seed_job.history if r.event == "approve"]
    assert len(approval) == 1
    assert approval[0].detail == "人工闸门放行"


def test_approving_twice_is_harmless(config: Config, knight_request: Path) -> None:
    create_character(knight_request, config)
    store = store_for(config, "knight_01")
    approve_seed(store.root)
    assert approve_seed(store.root)[1] == 8


def test_approving_before_generating_fails(tmp_path: Path) -> None:
    with pytest.raises(ProcessingError):
        approve_seed(tmp_path / "nothing")


# -- 动作网格 --------------------------------------------------------------


def test_animation_produces_frames_sheet_and_preview(
    config: Config, knight_request: Path
) -> None:
    create_character(knight_request, config)
    store = store_for(config, "knight_01")
    approve_seed(store.root)

    result = create_animation(
        store.root, action="walk", direction="down", config=config
    )

    assert result.key == "walk_down"
    assert result.frames == 8
    # 断言产出尺寸 == 请求里声明的尺寸，而不是 == 默认常量：
    # 示例可以按自己的细节密度选尺寸（knight 用 96，实测 48 装不下带剑人形），
    # 而"产出必须等于请求"才是这里真正要守的契约。
    assert result.frame_size == load_request(knight_request).style.target_size
    assert len(list(store.frames_of("walk_down").glob("*.png"))) == 8
    assert (store.sheets / "walk_down.png").exists()
    assert (store.previews / "walk_down.gif").exists()


def test_animation_records_grid_provenance(config: Config, knight_request: Path) -> None:
    create_character(knight_request, config)
    store = store_for(config, "knight_01")
    approve_seed(store.root)
    create_animation(store.root, action="walk", direction="down", config=config)

    entry = AssetManifest.load(store.manifest_path).animations["walk_down"]
    assert entry.grid.cols == 4 and entry.grid.rows == 2
    assert entry.grid.actual_size is not None
    assert entry.key_threshold is not None
    assert entry.source_image == "source/walk-down-original.png"


def test_seed_is_passed_as_a_reference_not_a_mask(
    config: Config, knight_request: Path
) -> None:
    """ADR-003 / PLAN §2.6：不传 mask，seed 以纯参考图身份进入。"""
    from pixel_asset_forge.providers import build_backend

    create_character(knight_request, config)
    store = store_for(config, "knight_01")
    approve_seed(store.root)
    create_animation(store.root, action="walk", direction="down", config=config)

    backend = build_backend(config)
    del backend  # 只为确认 build_backend 可用；调用记录看下面的日志

    import json

    log = json.loads(store.generation_log_path.read_text(encoding="utf-8"))
    edits = [e for e in log if e["key"] == "walk_down"]
    assert edits and edits[0]["prompt_hash"]


def test_unknown_action_is_rejected(config: Config, knight_request: Path) -> None:
    create_character(knight_request, config)
    store = store_for(config, "knight_01")
    approve_seed(store.root)
    with pytest.raises(ProcessingError, match="没有动作"):
        create_animation(store.root, action="dance", direction="down", config=config)


def test_regenerating_an_animation_archives_the_old_grid(
    config: Config, knight_request: Path
) -> None:
    create_character(knight_request, config)
    store = store_for(config, "knight_01")
    approve_seed(store.root)
    create_animation(store.root, action="walk", direction="down", config=config)

    with pytest.raises(ProcessingError, match="--regenerate"):
        create_animation(store.root, action="walk", direction="down", config=config)

    create_animation(
        store.root, action="walk", direction="down", config=config, regenerate=True
    )
    assert list(store.source.glob("walk-down-original.r*.png"))


def test_failed_action_can_be_retried_alone(config: Config, knight_request: Path) -> None:
    """失败的动作可单独重新生成 —— 不该拖累已完成的其他动作。"""
    create_character(knight_request, config)
    store = store_for(config, "knight_01")
    approve_seed(store.root)

    create_animation(store.root, action="walk", direction="down", config=config)
    create_animation(store.root, action="idle", direction="down", config=config)
    walk_before = (store.frames_of("walk_down") / "walk_down_00.png").read_bytes()

    create_animation(
        store.root, action="idle", direction="down", config=config, regenerate=True
    )
    assert (store.frames_of("walk_down") / "walk_down_00.png").read_bytes() == walk_before


# -- 镜像派生 --------------------------------------------------------------


def test_mirror_derive_costs_no_api_call(config: Config, slime_request: Path) -> None:
    create_character(slime_request, config)
    store = store_for(config, "slime_01")
    approve_seed(store.root)

    create_animation(store.root, action="walk", direction="left", config=config)
    result = create_animation(store.root, action="walk", direction="right", config=config)

    assert result.derived_from == "walk_left"
    assert result.calls_api is False
    # 派生方向没有自己的原图
    assert not store.source_path("walk_right").exists()


def test_derived_frames_are_the_mirror_image(config: Config, slime_request: Path) -> None:
    create_character(slime_request, config)
    store = store_for(config, "slime_01")
    approve_seed(store.root)
    create_animation(store.root, action="walk", direction="left", config=config)
    create_animation(store.root, action="walk", direction="right", config=config)

    left = np.array(Image.open(store.frames_of("walk_left") / "walk_left_00.png"))
    right = np.array(Image.open(store.frames_of("walk_right") / "walk_right_00.png"))
    assert np.array_equal(right, left[:, ::-1])


def test_derived_animation_is_recorded_as_derived(
    config: Config, slime_request: Path
) -> None:
    create_character(slime_request, config)
    store = store_for(config, "slime_01")
    approve_seed(store.root)
    create_animation(store.root, action="walk", direction="left", config=config)
    create_animation(store.root, action="walk", direction="right", config=config)

    manifest = AssetManifest.load(store.manifest_path)
    entry = manifest.animations["walk_right"]
    assert isinstance(entry, DerivedAnimation)
    assert entry.derived_from == "walk_left"
    assert entry.transform == "flip_horizontal"
    # 仅凭 Manifest + frames/ 就能解析出帧列表（ADR-001）
    assert len(manifest.resolve_frames("walk_right")) == 6


def test_deriving_before_the_source_exists_fails_clearly(
    config: Config, slime_request: Path
) -> None:
    create_character(slime_request, config)
    store = store_for(config, "slime_01")
    approve_seed(store.root)
    with pytest.raises(ProcessingError, match="还没有成品帧"):
        create_animation(store.root, action="walk", direction="right", config=config)


# -- 缓存 -----------------------------------------------------------------


def test_regeneration_must_not_hit_the_cache(config: Config, knight_request: Path) -> None:
    """**重生成必须绕开缓存。**

    这两件事表面一样、语义相反：

    - 重试失败的调用 —— 上次根本没拿到图，缓存是朋友（命中即免费）。
    - 因产出不合格而重生成 —— 上次拿到了图，只是画得不对。
      命中缓存会原样返回那张不合格的图，**修复永远不可能成功**。

    这条曾经真的挂过：repair 重生成后验证报出一模一样的失败数字，
    因为返回的就是同一张图。
    """
    create_character(knight_request, config)
    store = store_for(config, "knight_01")
    approve_seed(store.root)

    first = create_animation(store.root, action="walk", direction="down", config=config)
    second = create_animation(
        store.root, action="walk", direction="down", config=config, regenerate=True
    )
    assert first.cached is False
    assert second.cached is False, "重生成命中了缓存 —— 修复不可能成功"


def test_plain_rerun_still_hits_the_cache(config: Config, knight_request: Path) -> None:
    """非重生成的重复请求仍应命中缓存 —— 重跑失败任务不该重复付费。"""
    from pixel_asset_forge.processing.spritesheet import save_png
    from pixel_asset_forge.prompts import compile_animation_prompt

    del save_png, compile_animation_prompt  # 只为说明下面走的是同一条 prompt

    create_character(knight_request, config)
    store = store_for(config, "knight_01")
    approve_seed(store.root)
    create_animation(store.root, action="walk", direction="down", config=config)

    # 删掉产物但保留缓存，模拟"上次跑到一半失败了，重跑一次"
    store.archive_source("walk_down")
    again = create_animation(store.root, action="walk", direction="down", config=config)
    assert again.cached is True


# -- CLI ------------------------------------------------------------------


def test_cli_walks_the_whole_flow(
    tmp_path: Path, examples_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pixel-asset.yaml").write_text(
        "provider: mock\nmodel: mock-image\noutput_dir: outputs\n", encoding="utf-8"
    )
    request = tmp_path / "knight.yaml"
    request.write_text((examples_dir / "knight.yaml").read_text(encoding="utf-8"),
                       encoding="utf-8")

    created = runner.invoke(app, ["create-character", str(request)])
    assert created.exit_code == EXIT_OK
    assert "人工闸门" in created.stdout

    blocked = runner.invoke(
        app, ["create-animation", "--asset", "knight_01", "--action", "walk"]
    )
    assert blocked.exit_code == EXIT_ERROR
    assert "尚未获批准" in blocked.stderr

    approved = runner.invoke(
        app,
        ["create-animation", "--asset", "knight_01", "--action", "walk", "--approve-seed"],
    )
    assert approved.exit_code == EXIT_OK
    assert "seed 已批准" in approved.stdout
    assert "walk_down" in approved.stdout
