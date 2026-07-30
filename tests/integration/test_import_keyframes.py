"""导入一段关键帧（Sprint 6.8.2）。不调用 API。

与单张导入不同，这里多出三件事，每一件不做都会在播放时看出来：
共用调色板、共用裁剪框与锚点、顺序由文件名定。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from pixel_asset_forge.config import Config
from pixel_asset_forge.errors import ProcessingError
from pixel_asset_forge.models.manifest import AssetManifest
from pixel_asset_forge.pipelines import collect_keyframes, import_keyframes
from pixel_asset_forge.storage import ArtifactStore


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


def keyframe_dir(tmp_path: Path, *, count: int = 3, size: int = 96) -> Path:
    """一段关键帧：同一个角色，只有手臂在动，且**每帧多一种独有颜色**。

    独有颜色是刻意的：它让"各帧调色板必须完全一致"这种错误判据当场失败，
    而正确判据（同一套调色板的子集）仍然成立。
    """
    folder = tmp_path / "keys"
    folder.mkdir(parents=True, exist_ok=True)
    accents = [(200, 60, 80), (60, 160, 200), (240, 200, 60)]
    for index in range(count):
        rgba = np.zeros((size, size, 4), dtype=np.uint8)
        cx = size // 2
        rgba[20:70, cx - 12 : cx + 12] = (139, 90, 43, 255)
        rgba[10:26, cx - 9 : cx + 9] = (222, 176, 132, 255)
        rgba[62:74, cx - 15 : cx + 15] = (90, 60, 30, 255)
        arm = cx + 12 + index * 3
        rgba[34 : 42 + index * 2, arm : arm + 6] = (139, 90, 43, 255)
        rgba[12:18, cx - 4 : cx + 4] = (*accents[index % len(accents)], 255)
        Image.fromarray(rgba).save(folder / f"{index:02d}.png")
    return folder


def frames_of(store: ArtifactStore, key: str) -> list[np.ndarray]:
    return [
        np.array(Image.open(p).convert("RGBA"))
        for p in sorted(store.frames_of(key).glob("*.png"))
    ]


def opaque_colors(frame: np.ndarray) -> set[tuple[int, ...]]:
    opaque = frame[frame[:, :, 3] > 0][:, :3]
    return {tuple(c) for c in np.unique(opaque, axis=0)}


# -- 顺序 ------------------------------------------------------------------


def test_keyframes_are_ordered_by_filename(tmp_path: Path) -> None:
    """``glob`` 的返回顺序不保证，而帧序错乱无法自动检测。"""
    folder = keyframe_dir(tmp_path, count=3)
    assert [p.name for p in collect_keyframes(folder)] == ["00.png", "01.png", "02.png"]


def test_an_empty_folder_fails_loudly(tmp_path: Path) -> None:
    empty = tmp_path / "nothing"
    empty.mkdir()
    with pytest.raises(ProcessingError, match="没有关键帧图片"):
        collect_keyframes(empty)


def test_a_single_image_is_not_a_keyframe_sequence(
    config: Config, request_file: Path, tmp_path: Path
) -> None:
    folder = keyframe_dir(tmp_path, count=1)
    with pytest.raises(ProcessingError, match="至少要两张"):
        import_keyframes(request_file, folder, config, action="idle")


# -- 调色板 ----------------------------------------------------------------


def test_every_frame_draws_from_one_shared_palette(
    config: Config, request_file: Path, tmp_path: Path
) -> None:
    """判据是"同一套调色板的子集"，不是"各帧完全一致"。

    某一帧没有某个色号，只说明它不用那个颜色 —— 夹具里每帧都有一种独有强调色，
    正是为了让"必须完全一致"这种错误判据当场失败。
    """
    result = import_keyframes(
        request_file, keyframe_dir(tmp_path), config, action="idle"
    )
    store = ArtifactStore.for_asset(config.output_dir, result.asset_id)
    shared = {tuple(int(h[i : i + 2], 16) for i in (1, 3, 5)) for h in result.palette}

    per_frame = [opaque_colors(f) for f in frames_of(store, result.key)]
    assert len(per_frame) == 3
    for index, colors in enumerate(per_frame):
        assert colors <= shared, f"第 {index} 帧用了调色板外的颜色"
    assert len(set.union(*per_frame)) > len(set.intersection(*per_frame)), (
        "夹具没造出各帧独有的颜色，这条用例就测不到要测的东西"
    )


def test_the_palette_lands_in_the_manifest(
    config: Config, request_file: Path, tmp_path: Path
) -> None:
    """补间生成的中间帧要锁死到它 —— 取不到就锁不住。"""
    result = import_keyframes(
        request_file, keyframe_dir(tmp_path), config, action="idle"
    )
    store = ArtifactStore.for_asset(config.output_dir, result.asset_id)
    assert AssetManifest.load(store.manifest_path).palette.colors == result.palette


# -- 几何 ------------------------------------------------------------------


def test_frames_share_one_canvas_and_anchor(
    config: Config, request_file: Path, tmp_path: Path
) -> None:
    """逐帧各裁各的等于给每帧施加不同平移，播放时角色会跳。"""
    result = import_keyframes(
        request_file, keyframe_dir(tmp_path), config, action="idle"
    )
    store = ArtifactStore.for_asset(config.output_dir, result.asset_id)
    frames = frames_of(store, result.key)

    assert {f.shape[:2] for f in frames} == {(result.canvas[1], result.canvas[0])}
    assert result.anchor_drift_px <= 1.0


def test_mismatched_sizes_are_refused(
    config: Config, request_file: Path, tmp_path: Path
) -> None:
    folder = keyframe_dir(tmp_path, count=2)
    odd = np.zeros((64, 48, 4), dtype=np.uint8)
    odd[10:50, 10:38] = (139, 90, 43, 255)
    Image.fromarray(odd).save(folder / "99.png")
    with pytest.raises(ProcessingError, match="尺寸不一致"):
        import_keyframes(request_file, folder, config, action="idle")


# -- 原图与预算 ------------------------------------------------------------


def test_the_originals_are_kept_for_offline_reruns(
    config: Config, request_file: Path, tmp_path: Path
) -> None:
    result = import_keyframes(
        request_file, keyframe_dir(tmp_path), config, action="idle"
    )
    store = ArtifactStore.for_asset(config.output_dir, result.asset_id)
    kept = sorted(store.source.glob("idle-down-key*-original.png"))
    assert len(kept) == 3


def test_a_target_fps_produces_a_budget_but_no_frames(
    config: Config, request_file: Path, tmp_path: Path
) -> None:
    """预算是算出来的，中间帧是生成的 —— 这一步只做前者。"""
    result = import_keyframes(
        request_file, keyframe_dir(tmp_path), config,
        action="idle", source_fps=3, target_fps=9, loop=True,
    )
    assert result.budget is not None
    assert result.budget.target_frames == 9
    assert result.budget.generated_frames == 6
    assert len(result.frame_paths) == 3, "这一步不该产出中间帧"


def test_no_budget_without_a_target(
    config: Config, request_file: Path, tmp_path: Path
) -> None:
    result = import_keyframes(
        request_file, keyframe_dir(tmp_path), config, action="idle"
    )
    assert result.budget is None


def test_source_keeps_the_flattened_original_not_a_derivative(
    config: Config, request_file: Path, tmp_path: Path
) -> None:
    """``source/`` 处处都是"永不覆盖的原始输入"。

    这里曾经存的是抠完背景的 RGBA。下游按"原图是纯色背景"去读它，
    RGBA 转 RGB 把透明变成黑色，键控找到 0% 背景 —— 整幅图都算前景，
    裁剪框形同虚设，补间产出的帧大小全乱。
    """
    result = import_keyframes(
        request_file, keyframe_dir(tmp_path), config, action="idle"
    )
    store = ArtifactStore.for_asset(config.output_dir, result.asset_id)
    for path in sorted(store.source.glob("idle-down-key*-original.png")):
        stored = Image.open(path)
        assert stored.mode == "RGB", f"{path.name} 存成了 {stored.mode}"
        corner = np.array(stored.convert("RGB"))[0, 0]
        assert tuple(corner) == (255, 0, 255), "背景不是键控色，下游会把它当前景"
