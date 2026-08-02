"""tile 的确定性处理链。

**不复用静态资产那条链**，四步逐条不适用（PLAN §8.1）：tile 是满幅不透明的地面，
去背景会把地面本身键掉、主体包围盒对满幅图恒等于整幅、bottom-center 锚点无从谈起、
``scale_profile`` 求的是"内容占画布的比例"而 tile 要的是**精确等于** ``tile_size``。

于是这里只剩两件事：精确重采样，以及**整套 tile 共用一份调色板**。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..errors import ProcessingError
from .palette import PaletteResult, quantize_frames
from .resize import block_median_resize


@dataclass(frozen=True)
class TileSetResult:
    """一整套 tile 的处理结果。"""

    tiles: dict[str, np.ndarray]
    """``tile_id`` → RGBA，尺寸精确等于 ``tile_size``。"""

    palette: PaletteResult
    tile_size: tuple[int, int]


def _as_rgba(image: np.ndarray) -> np.ndarray:
    """tile 满幅不透明；来源是 RGB 就补一层全不透明的 alpha。"""
    if image.ndim != 3 or image.shape[2] not in (3, 4):
        raise ProcessingError(f"tile 原图必须是 HxWx3 或 HxWx4，收到 {image.shape}")
    if image.shape[2] == 4:
        return image
    alpha = np.full((*image.shape[:2], 1), 255, dtype=np.uint8)
    return np.concatenate([image, alpha], axis=2)


def process_tiles(
    sources: dict[str, np.ndarray],
    *,
    tile_size: tuple[int, int],
    max_colors: int,
) -> TileSetResult:
    """把一批 tile 原图处理成同尺寸、同调色板的成品。

    ``tile_id`` 的处理顺序按字典序固定 —— 共享调色板是把整批拼成一张长图一次量化
    得出的，输入顺序会改变量化结果，不定死就不幂等了。
    """
    if not sources:
        raise ProcessingError("没有可处理的 tile")

    width, height = tile_size
    ordered = sorted(sources)
    resized = [block_median_resize(_as_rgba(sources[key]), tile_size) for key in ordered]

    for key, tile in zip(ordered, resized, strict=True):
        actual = (tile.shape[1], tile.shape[0])
        if actual != (width, height):
            # 尺寸完全一致是 tileset 的硬性前提（PLAN §8 退出门槛第一条）：
            # 差一个像素，Godot 与 Tiled 的网格就整体错位。宁可这里炸掉。
            raise ProcessingError(
                f"tile {key} 处理后是 {actual}，不等于要求的 {(width, height)}"
            )

    quantized = quantize_frames(resized, max_colors)
    return TileSetResult(
        tiles=dict(zip(ordered, quantized.frames, strict=True)),
        palette=quantized,
        tile_size=(width, height),
    )
