"""从处理后像素独立推出 tile 四角的地形（PLAN §8.5）。

**不调用 API，也不相信请求声明。** 每个象限只与各地形 base tile 的对应象限
比较，声明只在调用方拿来核对实测结果。

距离取两条判据的较大值，任一超阈值即不相容：

- 逐 RGB 通道排序后，等位像素绝对差的平均值。它等价于三个**一维边缘分布**的
  Wasserstein-1 距离再取平均，能消除同材质随机颗粒落点不同造成的假差异，但不保留
  RGB 通道之间的联合颜色成分。
- 固定联合 RGB 投影上的 max-sliced Wasserstein-1。每个投影混合两个或三个通道，补上
  前一条对 ``{红, 绿}`` 与 ``{黄, 黑}`` 这类同边缘分布、不同颜色成分输入的盲区。

两条都刻意不看象限内空间排布：同一地形颗粒换位置不该改变地形归类。每个
Wasserstein 距离都除以固定的象限像素数；联合投影的系数绝对值和固定为 1，所以读数
仍是 RGB 通道差的单位。相邻调色板颜色间的质量迁移只付相邻颜色的距离，不会再像
直方图总变差那样把增大 ``max_colors`` 误当成材质变化。

真实共享调色板量化链跨 ``tile_size={16,24,32}``、``max_colors={16,64,128,256}``
扫描，同材质独立样本实测 0.816406～2.784722；工程阈值 12.0 是健康最大值的 4.31 倍。
32px 合成过渡四角实测 1.128906～1.472656；红绿/黄黑联合颜色反例为 127.5，草对土
为 52.0，高亮离群纹理最近距离 144.152344～145.722656。样本是合成数据，故
``calibrated=False``；阈值必须随 Manifest 记死，旧产物按当时阈值复算。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

TERRAIN_DISTANCE_MAX = 12.0
"""角落归类的绝对距离上限；超过即 unknown，不强塞最近地形。"""

_JOINT_PROJECTIONS = np.array(
    [
        (1, 1, 0),
        (1, -1, 0),
        (1, 0, 1),
        (1, 0, -1),
        (0, 1, 1),
        (0, 1, -1),
        (1, 1, 1),
        (1, 1, -1),
        (1, -1, 1),
        (-1, 1, 1),
    ],
    dtype=np.float64,
)
_JOINT_PROJECTIONS /= np.abs(_JOINT_PROJECTIONS).sum(axis=1, keepdims=True)
"""混合通道的固定投影；L1 归一化令投影距离与单通道距离使用同一量纲。"""

CORNER_NAMES = ("top_left", "top_right", "bottom_left", "bottom_right")


@dataclass(frozen=True)
class TerrainCornerResult:
    """一个角的最近地形与绝对距离；``terrain=None`` 表示 unknown。"""

    terrain: str | None
    distance: float


@dataclass(frozen=True)
class TerrainTileResult:
    """一块 tile 的四角结果，顺序与请求契约完全相同。"""

    corners: tuple[str | None, str | None, str | None, str | None]
    distances: tuple[float, float, float, float]


@dataclass(frozen=True)
class TerrainResult:
    """整套 tile 的角落地形推导结果。"""

    tiles: dict[str, TerrainTileResult]
    distance_max: float


def _rgb(image: np.ndarray) -> np.ndarray:
    if image.ndim != 3 or image.shape[2] not in (3, 4):
        raise ValueError(f"tile 必须是 HxWx3 或 HxWx4，收到 {image.shape}")
    return image[:, :, :3]


def _quadrants(image: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    rgb = _rgb(image)
    height, width = rgb.shape[:2]
    if height < 2 or width < 2:
        raise ValueError(f"tile 太小，无法分四个象限：{width}x{height}")
    mid_y, mid_x = height // 2, width // 2
    return (
        rgb[:mid_y, :mid_x],
        rgb[:mid_y, mid_x:],
        rgb[mid_y:, :mid_x],
        rgb[mid_y:, mid_x:],
    )


def quadrant_distance(first: np.ndarray, second: np.ndarray) -> float:
    """比较等尺寸象限的通道边缘分布与联合 RGB 成分，返回两者较大值。

    逐通道排序只保留 R/G/B 各自的一维边缘分布；它**看不见**通道如何组合成实际
    RGB 颜色。第二条判据把像素投影到固定的联合 RGB 轴，在每条轴上计算排序后的
    Wasserstein-1 距离并取最大值。颜色质量移到相邻颜色的代价很小，移到远处的代价
    很大，因此不会随共享调色板的桶数漂移。

    两条判据都对像素乱序不变，这是刻意保留的性质：这里核验的是一个象限属于哪种
    地形，不是象限内部纹理结构；结构连续性由 seam / adjacency 判据负责。
    """
    first_rgb, second_rgb = _rgb(first), _rgb(second)
    if first_rgb.shape != second_rgb.shape:
        raise ValueError(f"象限尺寸必须一致：{first_rgb.shape} != {second_rgb.shape}")
    first_pixels = first_rgb.reshape(-1, 3).astype(np.float64)
    second_pixels = second_rgb.reshape(-1, 3).astype(np.float64)

    left = np.sort(first_pixels, axis=0)
    right = np.sort(second_pixels, axis=0)
    marginal_distance = float(np.abs(left - right).mean())

    first_joint = np.sort(first_pixels @ _JOINT_PROJECTIONS.T, axis=0)
    second_joint = np.sort(second_pixels @ _JOINT_PROJECTIONS.T, axis=0)
    joint_distance = float(np.abs(first_joint - second_joint).mean(axis=0).max())
    return round(max(marginal_distance, joint_distance), 6)


def derive_terrain_corners(
    tiles: dict[str, np.ndarray],
    base_by_terrain: dict[str, np.ndarray],
    *,
    distance_max: float = TERRAIN_DISTANCE_MAX,
) -> TerrainResult:
    """逐 tile、逐象限与每个地形 base 的对应象限比较，取足够近的最近者。"""
    if not tiles:
        raise ValueError("没有可推导地形的 tile")
    if not base_by_terrain:
        raise ValueError("没有地形 base，不能从像素核验角落地形")
    if distance_max <= 0:
        raise ValueError(f"地形距离阈值必须大于 0，收到 {distance_max}")

    expected_shape = next(iter(tiles.values())).shape[:2]
    for tile_id, image in {**tiles, **base_by_terrain}.items():
        if image.shape[:2] != expected_shape:
            raise ValueError(
                f"tile {tile_id} 尺寸 {image.shape[:2]} 与 {expected_shape} 不一致"
            )

    bases = {
        terrain: _quadrants(image)
        for terrain, image in sorted(base_by_terrain.items())
    }
    measured: dict[str, TerrainTileResult] = {}
    for tile_id, image in sorted(tiles.items()):
        corners: list[str | None] = []
        distances: list[float] = []
        for index, quadrant in enumerate(_quadrants(image)):
            candidates = sorted(
                (
                    quadrant_distance(quadrant, base_quadrants[index]),
                    terrain,
                )
                for terrain, base_quadrants in bases.items()
            )
            distance, nearest = candidates[0]
            corners.append(nearest if distance <= distance_max else None)
            distances.append(distance)
        measured[tile_id] = TerrainTileResult(
            corners=(corners[0], corners[1], corners[2], corners[3]),
            distances=(distances[0], distances[1], distances[2], distances[3]),
        )
    return TerrainResult(tiles=measured, distance_max=distance_max)
