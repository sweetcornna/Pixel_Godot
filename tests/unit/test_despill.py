"""Despill。

这个模块曾经有一个把整张图配色毁掉的 bug，所以用例围绕它来写：
**只有当键控色的所有满值通道同时超标时才算溢色。**

原实现对洋红把 R 和 B 各自独立压到 G 的水平，于是褐色 (139,90,43)
被压成 (90,90,43) 橄榄绿 —— 皮甲、皮肤、木头、红色全部报废。
更要命的是所有数值检查（透明像素归零、锚点漂移、帧数）**全部通过**，
只有肉眼能发现。
"""

from __future__ import annotations

import numpy as np
import pytest

from pixel_asset_forge.processing import despill, spill_ratio
from pixel_asset_forge.processing.despill import spill_amount

MAGENTA = (255, 0, 255)
GREEN = (0, 255, 0)


def pixels(*colors: tuple[int, int, int]) -> np.ndarray:
    arr = np.zeros((1, len(colors), 4), dtype=np.uint8)
    for i, c in enumerate(colors):
        arr[0, i, :3] = c
        arr[0, i, 3] = 255
    return arr


@pytest.mark.parametrize(
    ("color", "name"),
    [
        ((139, 90, 43), "褐色皮甲"),
        ((222, 176, 132), "肤色"),
        ((180, 40, 40), "红色"),
        ((200, 120, 30), "橙色"),
        ((90, 140, 60), "绿色"),
        ((60, 80, 160), "蓝色"),
    ],
)
def test_ordinary_colours_are_untouched(color: tuple[int, int, int], name: str) -> None:
    """R 高于 G 的颜色（褐/肤/红/橙）绝不能被当成洋红溢色。"""
    out = despill(pixels(color), MAGENTA, edge_only=False)
    assert tuple(int(v) for v in out[0, 0, :3]) == color, name


@pytest.mark.parametrize(
    ("color", "expected"),
    [
        ((255, 0, 255), (0, 0, 0)),      # 纯洋红：全部压掉
        ((200, 50, 200), (50, 50, 50)),  # 洋红边：压到参考通道水平
        # 溢出量取 min(R,B)-G = 160-100 = 60，R 与 B 各减 60，
        # 相对差保留下来（B 本来就比 R 高 20，压完还是高 20）。
        ((160, 100, 180), (100, 100, 120)),
    ],
)
def test_magenta_spill_is_suppressed(
    color: tuple[int, int, int], expected: tuple[int, int, int]
) -> None:
    out = despill(pixels(color), MAGENTA, edge_only=False)
    assert tuple(int(v) for v in out[0, 0, :3]) == expected


def test_spill_amount_requires_all_high_channels_to_exceed() -> None:
    """判据的核心。褐色只有 R 高，B 远低于 G —— 不是溢色。"""
    rgb = np.array([[[139, 90, 43], [200, 50, 200]]], dtype=np.uint8)
    amount = spill_amount(rgb, MAGENTA)
    assert amount[0, 0] == 0, "褐色被误判为溢色"
    assert amount[0, 1] == 150


def test_green_key_uses_its_own_channel_layout() -> None:
    """键控色不是硬编码的洋红 —— 纯绿键控时满值通道只有 G。"""
    out = despill(pixels((60, 220, 70)), GREEN, edge_only=False)
    assert int(out[0, 0, 1]) < 220  # G 被压下来
    out2 = despill(pixels((139, 90, 43)), GREEN, edge_only=False)
    assert tuple(int(v) for v in out2[0, 0, :3]) == (139, 90, 43)


def test_edge_only_protects_interior_pixels() -> None:
    """溢色只发生在轮廓上。角色身上真有洋红配饰时不该被削掉。"""
    img = np.zeros((16, 16, 4), dtype=np.uint8)
    img[2:14, 2:14, :3] = (139, 90, 43)
    img[2:14, 2:14, 3] = 255
    img[7:9, 7:9, :3] = (240, 40, 240)  # 深处的洋红配饰

    kept = despill(img, MAGENTA, edge_only=True, edge_width=1)
    assert tuple(int(v) for v in kept[7, 7, :3]) == (240, 40, 240)

    stripped = despill(img, MAGENTA, edge_only=False)
    assert tuple(int(v) for v in stripped[7, 7, :3]) != (240, 40, 240)


def test_transparent_pixels_stay_zeroed() -> None:
    img = np.zeros((4, 4, 4), dtype=np.uint8)
    img[0, 0] = (255, 0, 255, 255)
    out = despill(img, MAGENTA, edge_only=False)
    assert (out[out[:, :, 3] == 0][:, :3] == 0).all()


def test_fully_transparent_input_is_returned_unchanged() -> None:
    img = np.zeros((4, 4, 4), dtype=np.uint8)
    assert np.array_equal(despill(img, MAGENTA), img)


def test_spill_ratio_drops_after_despill() -> None:
    img = pixels((255, 0, 255), (220, 30, 220), (139, 90, 43))
    before = spill_ratio(img, MAGENTA)
    after = spill_ratio(despill(img, MAGENTA, edge_only=False), MAGENTA)
    assert before > 0
    assert after == 0.0


def test_keyless_colour_is_skipped() -> None:
    """纯黑/纯白没有通道差异可利用，跳过而不是乱压。"""
    img = pixels((139, 90, 43))
    assert np.array_equal(despill(img, (255, 255, 255), edge_only=False), img)
    assert np.array_equal(despill(img, (0, 0, 0), edge_only=False), img)


def test_rejects_non_rgba() -> None:
    with pytest.raises(ValueError):
        despill(np.zeros((4, 4, 3), dtype=np.uint8), MAGENTA)
