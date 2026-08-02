"""整条处理链与 ``pixel-asset process``。

Sprint 3 的退出门槛在这里一次性验完，外加两条 Sprint 0 换来的教训：

- **process 必须幂等**。这条曾经真的挂过：阈值是逐图求解的，却被按资产存了一个，
  于是第二次运行时所有图被强制用同一个阈值，除第一张外产出全变。
- **配色不能被 despill 毁掉**。这条只有肉眼或颜色断言能发现 ——
  当时数值检查全部通过，而骑士的褐色皮甲已经变成橄榄绿。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from typer.testing import CliRunner

from pixel_asset_forge.cli import EXIT_OK, app
from pixel_asset_forge.models.manifest import AssetManifest
from pixel_asset_forge.pipelines import run_process
from pixel_asset_forge.planning import GridLayout, grid_for_frames
from pixel_asset_forge.processing import ProcessOptions, palette_overflow_ratio, process_grid

runner = CliRunner()

KEY = (255, 0, 255)
#: 刻意选一组"只有单通道高"的颜色 —— 它们正是被写坏的 despill 摧毁的那一类。
SKIN = (222, 176, 132)
LEATHER = (139, 90, 43)
CLOAK = (60, 110, 55)


def synthetic_grid(
    frames: int = 8, size: tuple[int, int] = (1774, 887), *, bg=(242, 4, 234)
) -> np.ndarray:
    """造一张近洋红背景的动作网格，尺寸取 Sprint 0 实测的 1774×887。

    ``frames=1`` 表示单幅立绘（种子图），走 1×1 布局。
    """
    layout = (
        GridLayout(frames=1, cols=1, rows=1, cell=size)
        if frames == 1
        else grid_for_frames(frames)
    )
    width, height = size
    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[:, :] = bg

    for index in range(frames):
        x0, y0, x1, y1 = layout.cell_box(index, size)
        cw, ch = x1 - x0, y1 - y0
        pad_x, pad_y = int(cw * 0.2), int(ch * 0.15)
        bob = index % 3

        body = (x0 + pad_x, y0 + pad_y + bob, x1 - pad_x, y1 - pad_y + bob)
        img[body[1] : body[3], body[0] : body[2]] = LEATHER
        head_h = (body[3] - body[1]) // 4
        img[body[1] : body[1] + head_h, body[0] : body[2]] = SKIN
        img[body[3] - head_h : body[3], body[0] : body[2]] = CLOAK

    return img


@pytest.fixture
def asset_dir(tmp_path: Path, examples_dir: Path) -> Path:
    root = tmp_path / "outputs" / "knight_01"
    (root / "source").mkdir(parents=True)
    (root / "request.yaml").write_text(
        (examples_dir / "knight.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    Image.fromarray(synthetic_grid()).save(root / "source" / "walk-down-original.png")
    Image.fromarray(synthetic_grid(4, (1024, 1024))).save(
        root / "source" / "idle-down-original.png"
    )
    return root


# -- 处理链 ---------------------------------------------------------------


def test_pipeline_meets_every_sprint3_gate() -> None:
    result = process_grid(
        synthetic_grid(), grid_for_frames(8),
        ProcessOptions(target_size=(32, 32), max_colors=24),
    )

    assert len(result.frames) == 8
    # 所有输出帧尺寸完全一致
    assert {f.shape for f in result.frames} == {(32, 32, 4)}
    # 完全透明像素的 RGB 均为 0
    for frame in result.frames:
        assert (frame[frame[:, :, 3] == 0][:, :3] == 0).all()
    # 锚点对齐后不再漂移
    assert result.anchor_drift_px <= 1
    # 调色板不越界
    assert result.palette.color_count <= 24
    assert palette_overflow_ratio(result.frames, result.palette.colors) == 0.0
    # 阈值被求解出来并可回写 Manifest
    assert result.key_threshold > 0


def test_pipeline_records_the_actual_source_size() -> None:
    """端点不保证按请求尺寸返回；Manifest 必须记实际值（Sprint 0 / A-1）。"""
    result = process_grid(synthetic_grid(), grid_for_frames(8))
    assert result.source_size == (1774, 887)
    # cell_size 现在是**抽帧后的共用视口**尺寸，不再是格线等分值 ——
    # 按连通域定位 sprite 之后，格子边界不再参与切分（ADR-003 修订）。
    assert result.cell_size[0] > 0 and result.cell_size[1] > 0


def test_component_split_is_the_normal_path() -> None:
    """正常产出应当走连通域抽帧，而不是退回等分切格。"""
    result = process_grid(synthetic_grid(), grid_for_frames(8))
    assert result.split is not None
    assert result.split.method.value == "components"
    assert len(result.frames) == 8


def test_offset_layout_is_not_a_defect() -> None:
    """整体偏移但姿势彼此分离 —— 这不是缺陷，旧的格线判据会把它判成 fatal。"""
    shifted = np.roll(synthetic_grid(), 60, axis=1)
    result = process_grid(shifted, grid_for_frames(8))
    assert result.split is not None
    assert result.split.method.value == "components"
    assert result.split.overlapping_pairs == 0
    assert len(result.frames) == 8


def test_pipeline_is_deterministic() -> None:
    """golden 测试能覆盖处理层，靠的就是这条（PLAN §2.7）。"""
    image, layout = synthetic_grid(), grid_for_frames(8)
    a = process_grid(image, layout)
    b = process_grid(image, layout)
    for x, y in zip(a.frames, b.frames, strict=True):
        assert np.array_equal(x, y)
    assert a.key_threshold == b.key_threshold


def test_despill_does_not_destroy_warm_colours() -> None:
    """被写坏的 despill 会把褐色/肤色压成橄榄绿，而所有数值检查照样通过。

    判据取"暖色仍然存在"：R 明显高于 G 的像素必须还在。
    """
    result = process_grid(
        synthetic_grid(), grid_for_frames(8),
        ProcessOptions(target_size=(32, 32), max_colors=24),
    )
    opaque = np.concatenate(
        [f[f[:, :, 3] > 0][:, :3].astype(np.int16) for f in result.frames]
    )
    warm = (opaque[:, 0] - opaque[:, 1]) > 20
    assert warm.mean() > 0.15, "暖色几乎消失 —— despill 把配色压垮了"


def test_clean_synthetic_grid_reports_no_overflow() -> None:
    assert process_grid(synthetic_grid(), grid_for_frames(8)).overflow.clean


# -- process 命令 ---------------------------------------------------------


def test_process_writes_the_expected_artifacts(asset_dir: Path) -> None:
    summaries = run_process(asset_dir)
    keys = {s["key"] for s in summaries}
    assert keys == {"walk_down", "idle_down"}

    assert len(list((asset_dir / "frames" / "walk_down").glob("*.png"))) == 8
    assert (asset_dir / "sheets" / "walk_down.png").exists()
    assert (asset_dir / "previews" / "walk_down.gif").exists()
    assert (asset_dir / "asset-manifest.json").exists()


def test_process_never_touches_the_source(asset_dir: Path) -> None:
    """原始生成图永不覆盖 —— 这是 process 能离线重跑的全部前提。"""
    source = asset_dir / "source" / "walk-down-original.png"
    before = source.read_bytes()
    run_process(asset_dir)
    run_process(asset_dir)
    assert source.read_bytes() == before


def test_process_is_idempotent(asset_dir: Path) -> None:
    """曾经挂过：阈值逐图求解却按资产存了一个，第二次运行产出全变。"""

    def fingerprint() -> bytes:
        parts = sorted((asset_dir / "frames").rglob("*.png"))
        return b"".join(p.read_bytes() for p in parts)

    run_process(asset_dir)
    first = fingerprint()
    run_process(asset_dir)
    assert fingerprint() == first
    run_process(asset_dir)
    assert fingerprint() == first


def test_each_animation_records_its_own_threshold(asset_dir: Path) -> None:
    """阈值是逐图求解的，共用一个会让 process 不幂等。"""
    run_process(asset_dir)
    manifest = AssetManifest.load(asset_dir / "asset-manifest.json")

    walk = manifest.animations["walk_down"].key_threshold
    idle = manifest.animations["idle_down"].key_threshold
    assert walk is not None and idle is not None


def test_manifest_records_the_size_snapping(asset_dir: Path) -> None:
    run_process(asset_dir)
    grid = AssetManifest.load(asset_dir / "asset-manifest.json").animations["walk_down"].grid
    assert grid is not None
    assert grid.actual_size == (1774, 887)
    assert grid.requested_size == (2048, 1024)
    assert grid.snapped is True
    assert grid.cell == (444, 444)  # 实际格子，不是名义 512


def test_manifest_stays_schema_valid(asset_dir: Path) -> None:
    run_process(asset_dir)
    AssetManifest.load(asset_dir / "asset-manifest.json").validate_schema()


def test_process_only_targets_one_action(asset_dir: Path) -> None:
    summaries = run_process(asset_dir, only="walk_down")
    assert [s["key"] for s in summaries] == ["walk_down"]
    assert not (asset_dir / "frames" / "idle_down").exists()


def test_process_only_keeps_the_pending_reprocess_flag(asset_dir: Path) -> None:
    """``--only`` 沿用既有基准，收敛不了别的动作，所以待收敛标记必须留着。

    清零过一次就等于把"基准被顶替过"抹掉了 —— 之后 combat_bundle 批量跑完
    不会补那次全量处理，用户拿到一批基准不一致的动作且毫不知情。
    """
    run_process(asset_dir)
    manifest_path = asset_dir / "asset-manifest.json"
    manifest = AssetManifest.load(manifest_path)
    assert manifest.scale_profile is not None
    manifest.scale_profile.needs_reprocess = True
    manifest.save(manifest_path)

    run_process(asset_dir, only="walk_down")
    profile = AssetManifest.load(manifest_path).scale_profile
    assert profile is not None
    assert profile.needs_reprocess is True

    # 全量跑才看得见所有动作，也只有它有资格清零。
    run_process(asset_dir)
    profile = AssetManifest.load(manifest_path).scale_profile
    assert profile is not None
    assert profile.needs_reprocess is False


def test_process_ignores_archived_sources(asset_dir: Path) -> None:
    """归档的历史版本（``.r1``）不该被当成新动作再处理一遍。"""
    archived = asset_dir / "source" / "walk-down-original.r1.png"
    Image.fromarray(synthetic_grid()).save(archived)
    assert {s["key"] for s in run_process(asset_dir)} == {"walk_down", "idle_down"}


def test_missing_asset_dir_fails_clearly(tmp_path: Path) -> None:
    from pixel_asset_forge.errors import ProcessingError

    with pytest.raises(ProcessingError):
        run_process(tmp_path / "nope")


# -- CLI ------------------------------------------------------------------


def test_cli_process_reports_a_summary(asset_dir: Path) -> None:
    result = runner.invoke(app, ["process", str(asset_dir)])
    assert result.exit_code == EXIT_OK
    assert "walk_down" in result.stdout
    assert "连通域" in result.stdout, "抽帧方式应当报告为连通域定位"


def test_cli_process_does_not_need_an_api_key(
    asset_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5/9 命令完全不碰 API —— 没有 Key 也必须能跑。"""
    monkeypatch.delenv("PIXEL_ASSET_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert runner.invoke(app, ["process", str(asset_dir)]).exit_code == EXIT_OK


def test_cli_process_on_empty_source_fails(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    (root / "source").mkdir(parents=True)
    assert runner.invoke(app, ["process", str(root)]).exit_code != EXIT_OK


def test_manifest_round_trips_through_json(asset_dir: Path) -> None:
    run_process(asset_dir)
    raw = json.loads((asset_dir / "asset-manifest.json").read_text(encoding="utf-8"))
    assert raw["schema_version"] == "2.0"
    assert raw["background"]["fallback_stage"] == "tolerant_key"
    # background.key_threshold 是**种子图**的阈值；这个资产没有 seed 原图，
    # 所以它缺省是对的。各动作的阈值在自己的条目里。
    assert "key_threshold" not in raw["background"]
    assert raw["animations"]["walk_down"]["key_threshold"] > 0


def test_seed_threshold_lands_on_the_background_block(
    asset_dir: Path, examples_dir: Path
) -> None:
    """有 seed 原图时，它的阈值写在 background 上。"""
    Image.fromarray(synthetic_grid(1, (1024, 1024))).save(
        asset_dir / "source" / "seed-original.png"
    )
    run_process(asset_dir)
    raw = json.loads((asset_dir / "asset-manifest.json").read_text(encoding="utf-8"))
    assert raw["background"]["key_threshold"] > 0
    assert (asset_dir / "seed-pixel.png").exists()
