"""导入用户自有素材作为 canonical seed。

存在的理由：用户已经有角色了，只是缺动画。让他们先花一次生成去换一张
"风格接近但不是同一个角色"的 seed，既费钱又把身份基准换掉了。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from pixel_asset_forge.config import Config
from pixel_asset_forge.errors import ProcessingError
from pixel_asset_forge.models.job import JobStatus
from pixel_asset_forge.models.manifest import AssetManifest
from pixel_asset_forge.pipelines import approve_seed, create_animation, import_seed
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


def user_sprite(tmp_path: Path, *, alpha: bool = True, size: int = 96) -> Path:
    """用户导出的素材：透明背景、硬边、1:1 像素画。"""
    rgba = np.zeros((size, size, 4), dtype=np.uint8)
    cx = size // 2
    rgba[20:70, cx - 12 : cx + 12] = (139, 90, 43, 255)
    rgba[10:26, cx - 9 : cx + 9] = (222, 176, 132, 255)
    rgba[62:74, cx - 15 : cx + 15] = (90, 60, 30, 255)
    if not alpha:
        flat = np.full((size, size, 3), (12, 200, 30), np.uint8)
        opaque = rgba[:, :, 3] > 0
        flat[opaque] = rgba[opaque][:, :3]
        path = tmp_path / "sprite_rgb.png"
        Image.fromarray(flat).save(path)
        return path
    path = tmp_path / "sprite.png"
    Image.fromarray(rgba).save(path)
    return path


def test_import_costs_no_api_call(
    config: Config, request_file: Path, tmp_path: Path
) -> None:
    result = import_seed(request_file, user_sprite(tmp_path), config)
    store = ArtifactStore.for_asset(config.output_dir, result.asset_id)
    assert not (store.root / "generation-log.jsonl").exists(), (
        "没有 prompt、没有 request_id，硬凑一条日志只会让溯源记录里出现查无此物的调用"
    )


def test_transparent_art_is_composited_onto_the_key_colour(
    config: Config, request_file: Path, tmp_path: Path
) -> None:
    """整条链的前提是"背景是一片纯键控色"，用户素材通常带 alpha。

    不合成的话去背景那一步无事可做，后面的连通域抽帧、despill 全部落空。
    """
    result = import_seed(request_file, user_sprite(tmp_path), config)
    assert result.had_alpha
    assert any("透明通道" in w for w in result.warnings)

    source = np.array(Image.open(result.seed_path).convert("RGBA"))
    assert (source[:, :, 3] == 255).all(), "存下来的原图不该还带 alpha"


def test_the_imported_seed_survives_the_processing_chain(
    config: Config, request_file: Path, tmp_path: Path
) -> None:
    result = import_seed(request_file, user_sprite(tmp_path), config)
    pixel = np.array(Image.open(result.pixel_path).convert("RGBA"))
    assert (pixel[:, :, 3] > 0).any(), "处理完角色没了"
    # 合成用的洋红必须被完全抠掉，不能在角色身上留粉边
    opaque = pixel[pixel[:, :, 3] > 0][:, :3].astype(int)
    pink = (opaque[:, 0] > 150) & (opaque[:, 2] > 150) & (opaque[:, 1] < opaque[:, 0] - 40)
    assert not pink.any(), f"角色上残留 {pink.sum()} 个键控色像素"


def test_import_stops_at_the_human_gate(
    config: Config, request_file: Path, tmp_path: Path
) -> None:
    """seed 是所有动画的身份基准 —— 导入的也一样要人看过才放行。"""
    result = import_seed(request_file, user_sprite(tmp_path), config)
    store = ArtifactStore.for_asset(config.output_dir, result.asset_id)

    manifest = AssetManifest.load(store.manifest_path)
    assert manifest.status == "awaiting_approval"

    table = store.load_job_table()
    assert table is not None and table.seed_job is not None
    assert table.seed_job.status is JobStatus.AWAITING_APPROVAL

    with pytest.raises(ProcessingError, match="尚未获批准"):
        create_animation(store.root, action="walk", direction="down", config=config)


def test_an_imported_seed_drives_animation_generation(
    config: Config, request_file: Path, tmp_path: Path
) -> None:
    """导入之后整条生成链应当原样可用 —— 这才是这个入口的意义。"""
    result = import_seed(request_file, user_sprite(tmp_path), config)
    store = ArtifactStore.for_asset(config.output_dir, result.asset_id)
    approve_seed(store.root)

    animation = create_animation(
        store.root, action="walk", direction="down", config=config
    )
    assert animation.frames > 0
    assert AssetManifest.load(store.manifest_path).animations["walk_down"].frames


def test_opaque_art_needs_no_compositing(
    config: Config, request_file: Path, tmp_path: Path
) -> None:
    result = import_seed(request_file, user_sprite(tmp_path, alpha=False), config)
    assert not result.had_alpha
    assert not any("透明通道" in w for w in result.warnings)


def test_reimporting_without_replace_is_refused(
    config: Config, request_file: Path, tmp_path: Path
) -> None:
    """原图永不覆盖 —— 覆盖会摧毁离线重跑能力。"""
    import_seed(request_file, user_sprite(tmp_path), config)
    with pytest.raises(ProcessingError):
        import_seed(request_file, user_sprite(tmp_path, size=64), config)
