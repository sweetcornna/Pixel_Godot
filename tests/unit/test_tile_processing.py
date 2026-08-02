"""tile 处理链：尺寸必须精确、调色板必须共享、结果必须幂等。"""

from __future__ import annotations

import numpy as np
import pytest

from pixel_asset_forge.errors import ProcessingError
from pixel_asset_forge.processing.tile import process_tiles

SOURCE = 1024


def _source(seed: int, base: tuple[int, int, int]) -> np.ndarray:
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 14, size=(SOURCE, SOURCE, 3))
    return np.clip(np.array(base) + noise, 0, 255).astype(np.uint8)


def _sources() -> dict[str, np.ndarray]:
    return {
        "grass_base": _source(1, (90, 130, 70)),
        "dirt_path": _source(2, (140, 105, 70)),
    }


@pytest.mark.parametrize("tile_size", [(16, 16), (32, 32), (48, 48), (64, 64)])
def test_every_tile_is_exactly_the_requested_size(tile_size) -> None:
    """Sprint 8 总门槛第一条。差一个像素，Godot 与 Tiled 的网格就整体错位。"""
    result = process_tiles(_sources(), tile_size=tile_size, max_colors=16)
    width, height = tile_size
    assert {tile.shape for tile in result.tiles.values()} == {(height, width, 4)}
    assert result.tile_size == tile_size


def test_tiles_share_one_palette() -> None:
    """不共享的话，同一张地图里草地与土路会像两套美术。"""
    result = process_tiles(_sources(), tile_size=(32, 32), max_colors=16)
    shared = {tuple(int(c) for c in colour) for colour in _colours(result)}
    assert len(result.palette.colors) <= 16
    # 每块 tile 的用色都必须是共享色板的子集。
    palette = {_hex(c) for c in shared}
    assert palette <= set(result.palette.colors)


def _colours(result) -> set[tuple[int, int, int]]:
    seen: set[tuple[int, int, int]] = set()
    for tile in result.tiles.values():
        rgb = tile[:, :, :3].reshape(-1, 3)
        seen |= {tuple(int(v) for v in row) for row in np.unique(rgb, axis=0)}
    return seen


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def test_processing_is_order_independent() -> None:
    """共享色板由整批拼图一次量化得出，输入顺序不能改变结果。"""
    sources = _sources()
    reversed_sources = dict(reversed(list(sources.items())))
    first = process_tiles(sources, tile_size=(32, 32), max_colors=16)
    second = process_tiles(reversed_sources, tile_size=(32, 32), max_colors=16)

    assert first.palette.colors == second.palette.colors
    for key in sources:
        assert np.array_equal(first.tiles[key], second.tiles[key])


def test_rgb_sources_are_accepted_as_opaque() -> None:
    """tile 满幅不透明，来源没有 alpha 通道是常态。"""
    result = process_tiles(_sources(), tile_size=(32, 32), max_colors=16)
    for tile in result.tiles.values():
        assert (tile[:, :, 3] == 255).all()


def test_empty_input_is_rejected() -> None:
    with pytest.raises(ProcessingError):
        process_tiles({}, tile_size=(32, 32), max_colors=16)


def test_malformed_source_is_rejected() -> None:
    with pytest.raises(ProcessingError):
        process_tiles(
            {"bad": np.zeros((SOURCE, SOURCE), dtype=np.uint8)},
            tile_size=(32, 32),
            max_colors=16,
        )
