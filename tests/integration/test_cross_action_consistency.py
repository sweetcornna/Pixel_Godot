"""跨动作的形象与尺寸一致性。

单看一个动作全是对的，切换动作才露馅 —— 同一个角色换了配色、或者忽然矮一截。
两条都在 ``process`` 里治：它是唯一看得见全部动作的地方。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from pixel_asset_forge.config import Config
from pixel_asset_forge.constants import ACTION_SIZE_BAND
from pixel_asset_forge.models.manifest import AssetManifest
from pixel_asset_forge.pipelines import (
    approve_seed,
    create_animation,
    create_character,
    run_process,
)
from pixel_asset_forge.storage import ArtifactStore


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        provider="mock", model="mock-image",
        output_dir=tmp_path / "outputs", cache_dir=tmp_path / "cache",
    )


@pytest.fixture
def asset(config: Config, tmp_path: Path, examples_dir: Path) -> ArtifactStore:
    request = tmp_path / "knight.yaml"
    request.write_text((examples_dir / "knight.yaml").read_text(encoding="utf-8"),
                       encoding="utf-8")
    create_character(request, config)
    store = ArtifactStore.for_asset(config.output_dir, "knight_01")
    approve_seed(store.root)
    for action in ("idle", "walk"):
        create_animation(store.root, action=action, direction="down", config=config)
    run_process(store.root)
    return store


def colors_of(store: ArtifactStore, key: str) -> set[tuple[int, ...]]:
    used: set[tuple[int, ...]] = set()
    for path in sorted(store.frames_of(key).glob("*.png")):
        frame = np.array(Image.open(path).convert("RGBA"))
        opaque = frame[frame[:, :, 3] > 0][:, :3]
        used |= {tuple(c) for c in np.unique(opaque, axis=0)}
    return used


def heights_of(store: ArtifactStore, key: str) -> list[int]:
    out = []
    for path in sorted(store.frames_of(key).glob("*.png")):
        rows = np.nonzero(np.array(Image.open(path).convert("RGBA"))[:, :, 3])[0]
        if rows.size:
            out.append(int(rows.max() - rows.min() + 1))
    return out


# -- 形象一致性 ------------------------------------------------------------


def test_every_action_draws_from_one_shared_palette(asset: ArtifactStore) -> None:
    """**不共用会让同一个角色在不同动作里换色。**

    实测 6 个真实角色，各动作各自量化出 32 色，跨动作重合度 **0%** ——
    骑士的绿斗篷在待机和走路里是两种完全不同的绿。播放时切换动作，
    整个角色的配色会跳一下。

    判据是"子集"而不是"完全一致"：某个动作用不到某个色号（史莱姆倒地融化了，
    用不到眼睛的颜色）不代表调色板不同 —— 这个错在关键帧导入那边犯过一次。
    """
    manifest = AssetManifest.load(asset.manifest_path)
    shared = {
        tuple(int(c[i : i + 2], 16) for i in (1, 3, 5))
        for c in manifest.palette.colors
    }
    assert shared, "Manifest 里没有调色板"

    for key in manifest.animations:
        assert colors_of(asset, key) <= shared, f"{key} 用了共用调色板之外的颜色"


# -- 尺寸一致性 ------------------------------------------------------------


def test_standing_actions_stay_within_their_size_band(asset: ArtifactStore) -> None:
    """跨动作缩放基准的前提是"尺寸差异是真实的姿势差异"，于是它把模型的
    随机漂移也原样保住了 —— 实测史莱姆待机 70px、走路 45px。

    走路不会让角色矮三成。真实的姿势差异是有界的，超出的部分判为漂移。
    """
    manifest = AssetManifest.load(asset.manifest_path)
    banded = {
        key: float(np.median(heights_of(asset, key)))
        for key in manifest.animations
        if ACTION_SIZE_BAND.get(key.rsplit("_", 1)[0]) is not None
        and heights_of(asset, key)
    }
    assert len(banded) >= 2, "至少要两个受约束的动作才谈得上一致性"

    standing = float(np.median(list(banded.values())))
    for key, height in banded.items():
        low, high = ACTION_SIZE_BAND[key.rsplit("_", 1)[0]]  # type: ignore[misc]
        assert standing * low - 1 <= height <= standing * high + 1, (
            f"{key} 高 {height:.0f}px，站立基准 {standing:.0f}px，"
            f"超出区间 {low:.0%}~{high:.0%}"
        )


def test_grounded_actions_are_not_clamped() -> None:
    """倒地天然矮一大截，钳它就是把动作毁了。"""
    assert ACTION_SIZE_BAND["death"] is None
    assert ACTION_SIZE_BAND["impact"] is None


def test_an_unknown_action_is_not_clamped() -> None:
    """不知道一个 dodge_roll 该多高，猜一个区间只会把正确产出压坏。"""
    assert ACTION_SIZE_BAND.get("dodge_roll") is None


# -- 补间过的动作 ----------------------------------------------------------


def test_process_skips_interpolated_animations(asset: ArtifactStore) -> None:
    """补间之后帧不再来自单张网格 —— ``process`` 的整条逻辑建立在
    "一个动作对应一张源网格"之上，对它无能为力。

    不跳过的话会撞上"帧下标超出网格容量"，而且是**整个 process 失败**，
    连别的动作也一起出不来。
    """
    from pixel_asset_forge.pipelines.process import _is_interpolated

    manifest = AssetManifest.load(asset.manifest_path)
    entry = manifest.animations["walk_down"]
    assert not _is_interpolated("walk_down", manifest)

    entry.keyframe_count = 2
    assert _is_interpolated("walk_down", manifest), "帧数多于关键帧数就是补过间"


def test_interpolation_intermediates_are_not_mistaken_for_animations() -> None:
    """``source/`` 下现在有三类文件，只有一类是动作原图。

    不区分的话 ``process`` 会把 ``hurt-down-gap00-original.png`` 当成动作
    ``hurt_down_gap00``，查不到帧数直接报错 —— 任何补过间的资产都跑不了 process。
    """
    from pixel_asset_forge.pipelines.process import _source_key

    assert _source_key(Path("walk-down-original.png")) == "walk_down"
    assert _source_key(Path("seed-original.png")) == "seed"
    assert _source_key(Path("walk-down-original.r1.png")) is None
    assert _source_key(Path("hurt-down-gap00-original.png")) is None
    assert _source_key(Path("react-down-key02-original.png")) is None
