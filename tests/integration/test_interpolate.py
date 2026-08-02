"""生成式补间（Sprint 6.8.3）。

两条硬约束，少一条这个功能就没有意义：关键帧原样保留、调色板锁死。
两条都在这里断言，因为它们都**很容易在重构中悄悄失效** ——
产出看着还像那么回事，只是关键帧被改了几十个像素、中间帧多了几种颜色。
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from pixel_asset_forge.config import Config
from pixel_asset_forge.errors import PlanError, ProcessingError
from pixel_asset_forge.models.manifest import AssetManifest
from pixel_asset_forge.pipelines import import_keyframes, run_interpolate
from pixel_asset_forge.storage import ArtifactStore
from tests.integration.test_import_keyframes import keyframe_dir


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        provider="mock", model="mock-image",
        output_dir=tmp_path / "outputs", cache_dir=tmp_path / "cache",
    )


@pytest.fixture
def request_file(tmp_path: Path, examples_dir: Path) -> Path:
    target = tmp_path / "knight.yaml"
    target.write_text((examples_dir / "knight.yaml").read_text(encoding="utf-8"),
                      encoding="utf-8")
    return target


@pytest.fixture
def imported(config: Config, request_file: Path, tmp_path: Path):  # type: ignore[no-untyped-def]
    result = import_keyframes(
        request_file, keyframe_dir(tmp_path, count=3), config,
        action="idle", source_fps=3, loop=True,
    )
    store = ArtifactStore.for_asset(config.output_dir, result.asset_id)
    before = [
        np.array(Image.open(p)) for p in sorted(store.frames_of(result.key).glob("*.png"))
    ]
    return result, store, before


def frames_of(store: ArtifactStore, key: str) -> list[np.ndarray]:
    return [np.array(Image.open(p)) for p in sorted(store.frames_of(key).glob("*.png"))]


def palette_rgb(colors: list[str]) -> set[tuple[int, ...]]:
    return {tuple(int(c[i : i + 2], 16) for i in (1, 3, 5)) for c in colors}


def keyframe_positions(order: list[str]) -> list[int]:
    """关键帧在最终序列里的下标。

    **不能写死**：循环动作有 N 个间隔、一次性动作有 N-1 个，
    每个间隔补几帧还取决于目标帧数 —— 位置随这些参数变。
    """
    return [i for i, tag in enumerate(order) if tag.startswith("key:")]


# -- 第一条硬约束：关键帧原样保留 ------------------------------------------


def test_keyframes_survive_interpolation_byte_for_byte(imported, config) -> None:  # type: ignore[no-untyped-def]
    """用户给的帧是基准，不能被"顺手优化"。

    实测踩过：导入用 Pillow 中位切分做像素映射，补间只能用最近色
    （它拿到的是既定调色板，没有原始像素分布可参考），两边不统一时
    关键帧差了 61 / 61 / 303 个像素。
    """
    result, store, before = imported
    out = run_interpolate(store.root, key=result.key, config=config, target_fps=9)

    after = frames_of(store, result.key)
    assert len(after) == 9
    positions = keyframe_positions(out.order)
    assert len(positions) == len(before)
    for index, position in enumerate(positions):
        assert np.array_equal(before[index], after[position]), (
            f"关键帧 {index}（序列第 {position} 帧）被改动了"
        )


# -- 第二条硬约束：调色板锁死 ----------------------------------------------


def test_generated_frames_use_only_the_keyframe_palette(imported, config) -> None:  # type: ignore[no-untyped-def]
    """中间帧重新量化会解出自己的一套色号，同一块布料在关键帧与中间帧之间
    跳色 —— 播放时整个角色闪。
    """
    result, store, _ = imported
    run_interpolate(store.root, key=result.key, config=config, target_fps=9)

    allowed = palette_rgb(AssetManifest.load(store.manifest_path).palette.colors)
    for index, frame in enumerate(frames_of(store, result.key)):
        opaque = frame[frame[:, :, 3] > 0][:, :3]
        used = {tuple(c) for c in np.unique(opaque, axis=0)}
        assert used <= allowed, f"第 {index} 帧用了调色板外的颜色"


# -- 可追溯性 --------------------------------------------------------------


def test_the_keyframe_fps_survives_so_it_can_be_recomputed(imported, config) -> None:  # type: ignore[no-untyped-def]
    """补完之后 ``fps`` 变成目标帧率。不单独记下关键帧自己的帧率，
    再跑一次 interpolate 就会按 9fps 算出目标 9 帧、发现盘上正好 9 帧，
    判定"不需要补间" —— 而真正的关键帧只有 3 张。
    """
    result, store, _ = imported
    run_interpolate(store.root, key=result.key, config=config, target_fps=9)

    entry = AssetManifest.load(store.manifest_path).animations[result.key]
    assert entry.fps == 9
    assert entry.keyframe_fps == 3
    assert entry.keyframe_count == 3

    # 再补一次到更高帧率仍然算得出来
    again = run_interpolate(store.root, key=result.key, config=config, target_fps=12)
    assert again.budget.keyframes == 3
    assert again.budget.target_frames == 12


def test_the_generated_grids_are_kept_for_offline_reruns(imported, config) -> None:  # type: ignore[no-untyped-def]
    """没有原图就既不能离线重跑，也没法在产出可疑时回头看模型画了什么。"""
    result, store, _ = imported
    run_interpolate(store.root, key=result.key, config=config, target_fps=9)
    stem = result.key.replace("_", "-")
    assert sorted(store.source.glob(f"{stem}-gap*-original.png"))


# -- 几何 ------------------------------------------------------------------


def test_content_height_progresses_smoothly_across_the_sequence(imported, config) -> None:  # type: ignore[no-untyped-def]
    """关键帧与中间帧来自不同分辨率的源，绝对像素高度不可比。

    直接丢进同一个 crop_all 求共用框，求出来的框没有意义 —— 实测中间帧
    全部撑满 128×128、关键帧才 80×100，播放时一跳一跳的。
    """
    result, store, _ = imported
    run_interpolate(store.root, key=result.key, config=config, target_fps=9)

    heights = []
    for frame in frames_of(store, result.key):
        rows = np.nonzero(frame[:, :, 3])[0]
        heights.append(int(rows.max() - rows.min() + 1))

    jumps = [abs(b - a) for a, b in pairwise(heights)]
    assert max(jumps) <= max(4, max(heights) * 0.12), f"帧间高度跳变过大：{heights}"


def test_the_anchor_stays_put(imported, config) -> None:  # type: ignore[no-untyped-def]
    result, store, _ = imported
    out = run_interpolate(store.root, key=result.key, config=config, target_fps=9)
    assert out.anchor_drift_px <= 1.0


# -- 拒绝 ------------------------------------------------------------------


def test_interpolating_without_keyframes_fails_loudly(
    config: Config, request_file: Path, tmp_path: Path
) -> None:
    from pixel_asset_forge.pipelines import create_character

    create_character(request_file, config)
    store = ArtifactStore.for_asset(config.output_dir, "knight_01")
    with pytest.raises(ProcessingError):
        run_interpolate(store.root, key="walk_down", config=config, target_fps=12)


def test_asking_for_no_extra_frames_is_refused(imported, config) -> None:  # type: ignore[no-untyped-def]
    """不需要补间时报错而不是空跑 —— 空跑会让用户以为补过了。"""
    result, store, _ = imported
    with pytest.raises((ProcessingError, PlanError)):
        run_interpolate(store.root, key=result.key, config=config, target_fps=3)


def test_the_frame_order_is_reported(imported, config) -> None:  # type: ignore[no-untyped-def]
    """帧序错乱无法自动检测，一份显式的顺序表是人工核对的唯一依据。"""
    result, store, _ = imported
    out = run_interpolate(store.root, key=result.key, config=config, target_fps=9)
    assert len(out.order) == 9
    assert out.order[0] == "key:0"
    assert sum(1 for tag in out.order if tag.startswith("key:")) == 3


def _four_frame_grid(tmp_path: Path) -> tuple[ArtifactStore, tuple[int, int, int]]:
    """造一个 4 格 × 64px 的源网格，每格一个可区分的实心方块。"""
    key_rgb = (255, 0, 255)
    store = ArtifactStore(tmp_path / "asset")
    store.source.mkdir(parents=True, exist_ok=True)
    grid = Image.new("RGB", (256, 64), key_rgb)
    for index in range(4):
        block = Image.new("RGB", (32, 40), (20 + index * 40, 90, 140))
        grid.paste(block, (index * 64 + 16, 16))
    grid.save(store.source / "hurt-down-original.png")
    return store, key_rgb


def test_reinterpolating_reads_the_grid_not_the_previous_output(tmp_path) -> None:
    """补第二次时，关键帧仍从源网格来 —— 不能被上一轮的成品帧数带偏。

    源网格 4 格、成品 8 帧，拿 8 去切 4 格的图会把每格劈成两半，
    每张"关键帧"都是半个角色，补出来的中间帧整张空白。
    """
    from pixel_asset_forge.models.manifest import GeneratedAnimation, GridInfo
    from pixel_asset_forge.pipelines.interpolate import _keyframes_from_grid

    store, key_rgb = _four_frame_grid(tmp_path)
    entry = GeneratedAnimation(
        fps=12,
        loop=False,
        grid=GridInfo(cols=4, rows=1, cell=(64, 64), requested_size=(256, 64),
                      actual_size=(256, 64)),
        source_image="source/hurt-down-original.png",
        key_threshold=100,
        # 补过一次之后的状态：成品 8 帧，源网格仍是 4 格
        frames=[f"frames/hurt_down/hurt_down_{i:02d}.png" for i in range(8)],
        keyframe_count=4,
        keyframe_fps=12,
    )
    assert len(_keyframes_from_grid(store, "hurt_down", entry, key_rgb)) == 4
