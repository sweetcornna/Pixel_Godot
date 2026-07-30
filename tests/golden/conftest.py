"""Golden image 测试的共享输入。

输入必须**足够丰富**才有回归价值：只有 3 种纯色的图，量化器根本不会被触发，
golden 测试就成了摆设。这里造一个带明暗层次、轮廓线与细节的角色，
颜色数远超 24 色预算，量化器必须真的做取舍。
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

GOLDEN_DIR = Path(__file__).parent.parent / "fixtures" / "golden"

KEY_BG = (242, 4, 234)
"""近洋红，不是精确 #FF00FF —— 模型从来画不出精确键控色（Sprint 0 / A-5）。"""


def _shaded(base: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    """按 ``t``（0~1）给基色打明暗，制造连续色阶。"""
    factor = 0.55 + 0.55 * t
    return tuple(min(255, int(c * factor)) for c in base)  # type: ignore[return-value]


def rich_grid(frames: int = 8, size: tuple[int, int] = (1774, 887)) -> np.ndarray:
    """带明暗层次的合成动作网格。

    刻意用"只有单通道高"的暖色（皮甲、肤色）—— 它们正是被写坏的 despill
    摧毁的那一类，golden 测试要能锁住这个回归。
    """
    from pixel_asset_forge.planning import grid_for_frames

    layout = grid_for_frames(frames)
    width, height = size
    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[:, :] = KEY_BG

    leather = (139, 90, 43)
    skin = (222, 176, 132)
    cloak = (60, 110, 55)
    outline = (26, 20, 30)

    for index in range(frames):
        x0, y0, x1, y1 = layout.cell_box(index, size)
        cw, ch = x1 - x0, y1 - y0
        pad_x, pad_y = int(cw * 0.22), int(ch * 0.16)
        swing = math.sin(index / frames * 2 * math.pi)

        left = x0 + pad_x + int(swing * cw * 0.03)
        right = x1 - pad_x + int(swing * cw * 0.03)
        top = y0 + pad_y
        bottom = y1 - pad_y

        body_h = bottom - top
        for row in range(top, bottom):
            t = (row - top) / max(1, body_h - 1)
            if t < 0.25:
                colour = _shaded(skin, 1.0 - t)
            elif t < 0.65:
                colour = _shaded(leather, 1.0 - (t - 0.25) / 0.4)
            else:
                colour = _shaded(cloak, 1.0 - (t - 0.65) / 0.35)
            img[row, left:right] = colour

        img[top, left:right] = outline
        img[bottom - 1, left:right] = outline
        img[top:bottom, left] = outline
        img[top:bottom, right - 1] = outline

    return img


@pytest.fixture(scope="session")
def golden_dir() -> Path:
    return GOLDEN_DIR


@pytest.fixture(scope="session")
def golden_input() -> np.ndarray:
    return rich_grid()
