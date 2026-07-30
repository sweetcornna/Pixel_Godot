"""色键去背景（ADR-004 / Sprint 0 A-5）。

三条实测换来的规则各自对应一组用例：
阈值逐图求解 · 形态学先补边 · 只保留与外缘连通的背景。
"""

from __future__ import annotations

import numpy as np
import pytest

from pixel_asset_forge.errors import ProcessingError
from pixel_asset_forge.processing import (
    apply_chroma_key,
    background_mask,
    color_distance,
    hex_to_rgb,
    otsu_threshold,
    zero_transparent_rgb,
)

MAGENTA = (255, 0, 255)


def scene(
    *,
    bg: tuple[int, int, int] = (242, 4, 234),
    fg: tuple[int, int, int] = (139, 90, 43),
    size: int = 64,
    noise: int = 2,
    rng_seed: int = 0,
) -> np.ndarray:
    """造一张"近洋红背景 + 居中方块"的图。

    背景刻意**不是**精确的 #FF00FF —— 模型从来画不出精确键控色
    （实测精确命中率 0.00%），用精确背景造测试数据会让整组用例失去意义。
    """
    rng = np.random.default_rng(rng_seed)
    img = np.zeros((size, size, 3), dtype=np.uint8)
    img[:, :] = bg
    if noise:
        jitter = rng.integers(-noise, noise + 1, size=(size, size, 3))
        img = np.clip(img.astype(np.int16) + jitter, 0, 255).astype(np.uint8)
    q = size // 4
    img[q : size - q, q : size - q] = fg
    return img


def test_hex_parsing() -> None:
    assert hex_to_rgb("#FF00FF") == MAGENTA
    assert hex_to_rgb("00ff00") == (0, 255, 0)
    with pytest.raises(ProcessingError):
        hex_to_rgb("#FFF")


def test_exact_key_never_matches_real_output() -> None:
    """初版的"精确色键"档在真实数据上命中率为零，这正是它被删掉的原因。"""
    img = scene()
    exact = (color_distance(img, MAGENTA) == 0).mean()
    assert exact == 0.0


def test_otsu_separates_the_two_clusters() -> None:
    """判据是**分离性质**，不是某个数字区间。

    实测阈值高度依赖直方图形状：真实生成图上落在 161–165，
    而合成图的直方图是完美双峰（背景 ~25、前景 ~258、中间全空），
    Otsu 会紧贴背景簇上沿落在 ~28。两者都正确。
    断言具体数值等于把测试绑死在某一种输入分布上。
    """
    img = scene()
    distances = color_distance(img, MAGENTA)
    threshold = otsu_threshold(distances)

    corner = distances[:8, :8]  # 纯背景
    centre = distances[28:36, 28:36]  # 纯前景
    assert corner.max() <= threshold < centre.min()


def test_perfectly_uniform_background_is_still_separated() -> None:
    """无噪声背景会把所有背景像素挤进同一个直方图桶。

    若阈值取桶中心，这个桶会被自己劈成两半，``distance <= threshold``
    全部为假 —— 整张图被判成前景、背景占比 0%。
    讽刺的是这只在模型表现**最好**的时候才会踩中。
    """
    img = scene(noise=0)
    result = apply_chroma_key(img, MAGENTA)
    assert result.background_ratio > 0.5
    assert result.rgba[0, 0, 3] == 0
    assert result.rgba[32, 32, 3] == 255


def test_threshold_is_deterministic() -> None:
    img = scene()
    assert otsu_threshold(color_distance(img, MAGENTA)) == otsu_threshold(
        color_distance(img, MAGENTA)
    )


def test_key_separates_background_from_foreground() -> None:
    result = apply_chroma_key(scene(), MAGENTA)
    alpha = result.rgba[:, :, 3]
    assert alpha[0, 0] == 0  # 角落是背景
    assert alpha[32, 32] == 255  # 中心是前景
    assert 0.5 < result.background_ratio < 0.9


def test_outermost_ring_is_not_swallowed() -> None:
    """scipy 的腐蚀在图像边界按"外部为前景"处理，会吞掉最外一圈背景，
    导致泛洪种子为空、掩膜全黑（表现为前景占比 100%）。这个坑真实踩过。
    """
    result = apply_chroma_key(scene(), MAGENTA)
    alpha = result.rgba[:, :, 3]
    assert alpha[0, :].sum() == 0
    assert alpha[-1, :].sum() == 0
    assert alpha[:, 0].sum() == 0
    assert alpha[:, -1].sum() == 0
    assert result.background_ratio < 1.0


def test_interior_key_coloured_pixels_are_kept() -> None:
    """角色身上真有洋红色块时，不该被抠出洞 —— 只删与外缘连通的背景。"""
    img = scene(fg=(139, 90, 43))
    img[30:34, 30:34] = (250, 6, 240)  # 角色内部一块洋红配饰
    result = apply_chroma_key(img, MAGENTA)
    assert result.rgba[32, 32, 3] == 255, "角色内部的洋红被误抠了"


def test_transparent_pixels_have_zero_rgb() -> None:
    """致命级验证项：alpha=0 的像素 RGB 必须为 0。"""
    result = apply_chroma_key(scene(), MAGENTA)
    transparent = result.rgba[result.rgba[:, :, 3] == 0]
    assert (transparent[:, :3] == 0).all()


def test_zero_transparent_rgb_is_idempotent() -> None:
    rgba = np.zeros((4, 4, 4), dtype=np.uint8)
    rgba[:, :, :3] = 200  # 透明但 RGB 非零
    cleaned = zero_transparent_rgb(rgba)
    assert (cleaned[:, :, :3] == 0).all()
    assert (zero_transparent_rgb(cleaned) == cleaned).all()


def test_supplied_threshold_is_reused_not_resolved() -> None:
    """``process`` 离线重跑必须复用 Manifest 里的阈值，否则结果不可复现。

    验证用的阈值取 300 —— 它高过前景簇，会把整张图判成背景。
    只有阈值真的生效才会出现这个结果；用一个仍落在空谷里的值
    （如 auto+50）证明不了任何事，因为掩膜根本不会变。
    """
    img = scene()
    auto = apply_chroma_key(img, MAGENTA)
    forced = apply_chroma_key(img, MAGENTA, threshold=300.0)

    assert forced.threshold == 300.0
    assert not np.array_equal(auto.rgba, forced.rgba)
    assert forced.background_ratio > auto.background_ratio


def test_same_threshold_gives_identical_bytes() -> None:
    img = scene()
    a = apply_chroma_key(img, MAGENTA, threshold=150.0)
    b = apply_chroma_key(img, MAGENTA, threshold=150.0)
    assert np.array_equal(a.rgba, b.rgba)


def test_all_background_image_is_flagged_as_suspicious() -> None:
    """背景占比越界说明不是双峰分布，Otsu 结果不可信，应升到下一档。"""
    img = np.full((32, 32, 3), (242, 4, 234), dtype=np.uint8)
    assert apply_chroma_key(img, MAGENTA).suspicious


def test_background_mask_returns_the_threshold_used() -> None:
    mask, threshold = background_mask(scene(), MAGENTA)
    assert mask.dtype == bool
    assert threshold > 0


def test_rejects_non_rgb_input() -> None:
    with pytest.raises(ProcessingError):
        apply_chroma_key(np.zeros((8, 8), dtype=np.uint8), MAGENTA)
