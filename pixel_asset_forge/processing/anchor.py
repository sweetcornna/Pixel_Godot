"""Bottom-center 锚点对齐。

游戏引擎按锚点摆放精灵。角色的锚点是**脚底中心** —— 脚踩在哪里，角色就站在哪里。
锚点不统一的后果是角色在播放动画时上下抖动或左右漂移。

Sprint 0 实测：**模型不会把脚对齐到统一基线**，8 帧的脚底位置极差达 9~10%
（512px 单元格上约 40~51px）。所以这一步不是"以防万一"，是每一组帧都必须跑。

锚点写入 Manifest（Sprint 3 退出门槛），导出器与引擎据此定位。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..errors import ProcessingError


@dataclass(frozen=True, slots=True)
class Anchor:
    type: str = "bottom_center"
    x: float = 0.5
    y: float = 1.0

    def pixel_position(self, size: tuple[int, int]) -> tuple[int, int]:
        width, height = size
        return (round(self.x * width), round(self.y * height))


BOTTOM_CENTER = Anchor()
CENTER = Anchor(type="center", x=0.5, y=0.5)


def content_anchor(rgba: np.ndarray) -> tuple[float, float] | None:
    """单帧内容的锚点（像素坐标）。全透明帧返回 None。

    纵向取内容底边（脚踩的那条线），横向取**整个轮廓的质心**。

    横向不能用包围盒中心：剑这一帧甩向左、下一帧收回来，包围盒的左边界
    跟着动，中心相对身体就偏了 —— 再把这个中心对齐到画布中央，等于把身体
    往反方向推。用户在 walk_down 上看出的左右摇摆里有一部分是这么来的。

    也不能只取底部一条"脚带"：跨步时前后脚高度不同，底部那条带里几乎只有
    落地的那只脚，质心于是跟着脚走而不是跟着身体走，每迈一步摇一次 ——
    实测锚点漂移 4.5px，比包围盒中心更糟。

    整轮廓质心是按像素数量加权的，主体是头和躯干；剑只有十几个像素，
    甩到最远也只能把质心带偏不到 1px。姿势再怎么变，身体重心本来就是稳的。
    """
    ys, xs = np.nonzero(rgba[:, :, 3])
    if xs.size == 0:
        return None
    return (float(xs.mean()), float(ys.max()) + 1.0)


def place_on_canvas(
    rgba: np.ndarray,
    canvas: tuple[int, int],
    *,
    anchor: Anchor = BOTTOM_CENTER,
) -> np.ndarray:
    """把一帧按锚点贴到指定画布上。超出画布的部分会被裁掉。"""
    if rgba.ndim != 3 or rgba.shape[2] != 4:
        raise ProcessingError(f"place_on_canvas 需要 RGBA，收到 {rgba.shape}")

    width, height = canvas
    out = np.zeros((height, width, 4), dtype=np.uint8)

    if anchor.type == "center":
        ys, xs = np.nonzero(rgba[:, :, 3])
        src = (
            None
            if xs.size == 0
            else (
                (float(xs.min()) + float(xs.max()) + 1.0) / 2.0,
                (float(ys.min()) + float(ys.max()) + 1.0) / 2.0,
            )
        )
    else:
        src = content_anchor(rgba)
    if src is None:
        return out  # 空帧就是空画布，交给 blank_frame 检查去报

    target_x, target_y = anchor.pixel_position(canvas)
    offset_x = round(target_x - src[0])
    offset_y = round(target_y - src[1])

    src_h, src_w = rgba.shape[:2]
    dst_x0, dst_y0 = max(0, offset_x), max(0, offset_y)
    dst_x1, dst_y1 = min(width, offset_x + src_w), min(height, offset_y + src_h)
    if dst_x0 >= dst_x1 or dst_y0 >= dst_y1:
        return out

    src_x0, src_y0 = dst_x0 - offset_x, dst_y0 - offset_y
    out[dst_y0:dst_y1, dst_x0:dst_x1] = rgba[
        src_y0 : src_y0 + (dst_y1 - dst_y0), src_x0 : src_x0 + (dst_x1 - dst_x0)
    ]
    out[out[:, :, 3] == 0] = 0
    return out


def align_frames(
    frames: list[np.ndarray],
    canvas: tuple[int, int],
    *,
    anchor: Anchor = BOTTOM_CENTER,
) -> list[np.ndarray]:
    """把整组帧对齐到同一画布与同一锚点。"""
    if not frames:
        raise ProcessingError("帧列表为空")
    return [place_on_canvas(f, canvas, anchor=anchor) for f in frames]


def anchor_drift(frames: list[np.ndarray], *, anchor: Anchor = BOTTOM_CENTER) -> float:
    """对齐后的最大锚点漂移（像素）。验证引擎按 per-action 阈值判定。"""
    if not frames:
        return 0.0

    height, width = frames[0].shape[:2]
    target_x, target_y = anchor.pixel_position((width, height))

    worst = 0.0
    for frame in frames:
        position = content_anchor(frame)
        if position is None:
            continue
        drift = max(abs(position[0] - target_x), abs(position[1] - target_y))
        worst = max(worst, drift)
    return worst
