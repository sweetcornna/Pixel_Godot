"""Character anchor sheet —— 用图而不是文字压住漂移。

来自 agent-sprite-forge：光靠 prompt 压不住的镜像翻转、体型漂移、脚线漂移，
给一张"每格都已经站好一个正确角色"的模板就压得住。
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

from pixel_asset_forge.pipelines.common import (
    ANCHOR_FEET_RATIO,
    anchor_sheet,
    blank_canvas,
)
from pixel_asset_forge.planning import layout_for_frames

KEY = "#FF00FF"


def seed_image(width: int = 200, height: int = 300) -> np.ndarray:
    """一个左右不对称的"角色"：右手边多一条"剑"。"""
    img = np.full((height, width, 3), (255, 0, 255), dtype=np.uint8)
    img[60:260, 70:130] = (139, 90, 43)          # 躯干
    img[40:80, 85:115] = (222, 176, 132)         # 头
    img[120:230, 132:144] = (220, 220, 200)      # 剑（只在一侧）
    return img


def decode(data: bytes) -> np.ndarray:
    return np.array(Image.open(io.BytesIO(data)).convert("RGB"))


def cells(sheet: np.ndarray, layout) -> list[np.ndarray]:  # type: ignore[no-untyped-def]
    h, w = sheet.shape[:2]
    cw, ch = w // layout.cols, h // layout.rows
    return [
        sheet[(i // layout.cols) * ch : (i // layout.cols + 1) * ch,
              (i % layout.cols) * cw : (i % layout.cols + 1) * cw]
        for i in range(layout.capacity)
    ]


def test_every_cell_gets_the_same_character() -> None:
    layout = layout_for_frames(6)
    sheet = decode(anchor_sheet(seed_image(), layout.size, layout, KEY))
    boxes = cells(sheet, layout)
    for other in boxes[1:]:
        assert np.array_equal(boxes[0], other), "各格不一致，模板就不是基准了"


def test_the_character_is_not_mirrored_between_cells() -> None:
    """镜像正是要修的缺陷 —— 模板自己先不能出现镜像。"""
    layout = layout_for_frames(6)
    boxes = cells(decode(anchor_sheet(seed_image(), layout.size, layout, KEY)), layout)
    assert not np.array_equal(boxes[0], boxes[1][:, ::-1]), "第二格是第一格的镜像"


def test_feet_land_on_the_declared_ground_line() -> None:
    layout = layout_for_frames(4)
    sheet = decode(anchor_sheet(seed_image(), layout.size, layout, KEY))
    cell = cells(sheet, layout)[0]
    subject = np.any(cell != (255, 0, 255), axis=-1)
    bottom = int(np.nonzero(subject)[0].max()) + 1
    assert abs(bottom - round(cell.shape[0] * ANCHOR_FEET_RATIO)) <= 1


def test_background_stays_pure_key_colour() -> None:
    """底图背景一旦不纯，整条抠图链路的前提就没了。"""
    layout = layout_for_frames(4)
    sheet = decode(anchor_sheet(seed_image(), layout.size, layout, KEY))
    corner = sheet[:8, :8].reshape(-1, 3)
    assert np.array_equal(np.unique(corner, axis=0), np.array([[255, 0, 255]]))


def test_a_seed_with_no_subject_falls_back_to_a_blank_canvas() -> None:
    layout = layout_for_frames(4)
    empty = np.full((100, 100, 3), (255, 0, 255), dtype=np.uint8)
    assert anchor_sheet(empty, layout.size, layout, KEY) == blank_canvas(
        layout.size, KEY
    )
