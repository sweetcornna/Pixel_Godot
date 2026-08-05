"""Golden image 回归测试（PLAN §10.2）。

覆盖范围有明确边界：**只覆盖处理层，覆盖不了生成层**（PLAN §2.7）。
生成层无 seed 参数、同一 prompt 每次结果不同，不可能有 golden。

比较策略分两档，因为两段代码的确定性强度不同：

- **几何链**（键控 → 切帧 → despill → 裁剪 → 最近邻缩放 → 对齐）
  是纯整数运算，断言**字节级完全一致**。
- **完整链**额外含 Pillow 的中位切分量化，其结果随 Pillow 版本可能微调。
  按 PLAN §10.2 的告诫用**容差**比较，否则依赖一升级就大面积假失败。

重新生成基准：``REGENERATE_GOLDEN=1 pytest tests/golden``

基准变更记录（改基准前先确认是**故意**的行为变化，不是回归）：

- 2026-07：两张基准都因**锚点横向基准从包围盒中心改成轮廓质心**重生成。
  剑甩出去时包围盒边界跟着动、中心相对身体就偏了，对齐等于把身体往反方向推
  （用户实测的左右摇摆）。质心按像素数量加权，主体是头和躯干，带不动。
- 2026-08-05：``walk_geometry_expected`` 因**切帧前先居中裁到网格整倍数**重生成。
  夹具输入刻意用 Sprint 0 记录的非整除尺寸 ``1774×887``，正是这条修复生效的场景 ——
  格子由 443/444 混杂变成精确 443×443，几何链要求字节级一致，所以必然重生成。
  ``walk_full_expected`` 未重生成：它按容差 24 比较且 alpha 逐位相等，差异被吸收。
  这是故意的行为变化，不是回归；理由与实测见 ``frame_split.center_crop_to_grid``。
- 2026-07：``walk_full_expected`` 因"放大只允许整数倍"重生成，随后**又改回来**。
  那条规则只砍需要放大的动作、不影响需要缩小的，跨动作缩放基准当场失效
  （实测同一个角色 hurt 占画布 49%、attack 占 78%）。现在整数倍只告警不强制，
  见 ``scale_profile.uneven_upscale``。几何链基准未变，它不走 ``process_grid``。
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from pixel_asset_forge.planning import grid_for_frames
from pixel_asset_forge.processing import (
    ProcessOptions,
    align_frames,
    apply_chroma_key,
    compose_spritesheet,
    crop_all,
    despill,
    hex_to_rgb,
    normalize_cell_sizes,
    process_grid,
    resize_to_fit,
    split_grid,
)

pytestmark = pytest.mark.golden

KEY = hex_to_rgb("#FF00FF")
REGENERATE = os.environ.get("REGENERATE_GOLDEN") == "1"


def geometry_chain(image: np.ndarray) -> np.ndarray:
    layout = grid_for_frames(8)
    keyed = apply_chroma_key(image, KEY)
    frames = normalize_cell_sizes(split_grid(keyed.rgba, layout))
    frames = [despill(f, KEY) for f in frames]
    frames, _ = crop_all(frames)
    frames = [resize_to_fit(f, (32, 32)) for f in frames]
    frames = align_frames(frames, (32, 32))
    sheet, _ = compose_spritesheet(frames)
    return sheet


def full_chain(image: np.ndarray) -> np.ndarray:
    result = process_grid(
        image, grid_for_frames(8), ProcessOptions(target_size=(32, 32), max_colors=24)
    )
    sheet, _ = compose_spritesheet(result.frames)
    return sheet


def compare(actual: np.ndarray, path: Path, *, tolerance: int = 0) -> None:
    if REGENERATE or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(actual, mode="RGBA").save(path)
        if not REGENERATE:
            pytest.skip(f"已生成缺失的基准 {path.name}，请复核后提交")
        return

    expected = np.array(Image.open(path).convert("RGBA"))
    assert actual.shape == expected.shape, f"尺寸变了：{actual.shape} vs {expected.shape}"

    # alpha 通道永远严格比较 —— 透明区错了是致命级问题，不接受任何容差。
    assert np.array_equal(actual[:, :, 3], expected[:, :, 3]), "alpha 通道与基准不一致"

    diff = np.abs(actual[:, :, :3].astype(np.int16) - expected[:, :, :3].astype(np.int16))
    worst = int(diff.max())
    assert worst <= tolerance, (
        f"像素差异 {worst} 超过容差 {tolerance}"
        f"（{int((diff.max(axis=-1) > tolerance).sum())} 个像素越界）"
    )


def test_geometry_chain_is_byte_exact(golden_input: np.ndarray, golden_dir: Path) -> None:
    """纯整数运算，不允许任何漂移。"""
    compare(geometry_chain(golden_input), golden_dir / "walk_geometry_expected.png")


def test_full_chain_within_tolerance(golden_input: np.ndarray, golden_dir: Path) -> None:
    """含 Pillow 量化，按 PLAN §10.2 用容差 —— 严格相等会在依赖升级时大面积假失败。"""
    compare(full_chain(golden_input), golden_dir / "walk_full_expected.png", tolerance=24)


def test_golden_input_actually_exercises_the_quantizer(golden_input: np.ndarray) -> None:
    """基准输入的颜色数必须远超色数预算，否则量化器根本不会被触发。"""
    colors = len(np.unique(golden_input.reshape(-1, 3), axis=0))
    assert colors > 24 * 4, f"基准输入只有 {colors} 色，量化 golden 测试是摆设"


def test_repeated_runs_agree(golden_input: np.ndarray) -> None:
    """确定性是 golden 测试成立的前提，先自证一下。"""
    assert np.array_equal(geometry_chain(golden_input), geometry_chain(golden_input))
    assert np.array_equal(full_chain(golden_input), full_chain(golden_input))


def test_transparent_pixels_are_zeroed_in_the_golden(
    golden_input: np.ndarray, golden_dir: Path
) -> None:
    """致命级验证项，基准本身也必须满足。"""
    sheet = full_chain(golden_input)
    assert (sheet[sheet[:, :, 3] == 0][:, :3] == 0).all()


def test_warm_colours_survive_the_golden_pipeline(golden_input: np.ndarray) -> None:
    """锁住被写坏的 despill 那个回归：褐色/肤色不能被压成橄榄绿。"""
    sheet = full_chain(golden_input)
    opaque = sheet[sheet[:, :, 3] > 0][:, :3].astype(np.int16)
    assert ((opaque[:, 0] - opaque[:, 1]) > 20).mean() > 0.2
