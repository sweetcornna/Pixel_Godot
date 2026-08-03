"""按邻接表铺一张地图（PLAN §8.3）。**不调用 API。**

输入是 8.2 写进 Manifest 的邻接表，输出是一张每对相邻格都合法的地图。
求解在 :mod:`..planning.wfc`，这里只负责取输入、落盘、记账。

地图不内联进 Manifest：它自己落一个 JSON，Manifest 记路径、哈希与 ``seed``
—— 凭 Manifest 加文件能重建全部产物，而 ``seed`` 让"怎么铺出来的"也可复现。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..errors import ProcessingError
from ..logging_utils import get_logger
from ..models.manifest import AssetManifest, TileMapEntry
from ..planning.wfc import TileMap, generate_map
from ..storage.artifacts import ArtifactStore
from ..storage.atomic import atomic_write_json
from ..storage.hashes import hash_file

logger = get_logger("pipeline.tilemap")

#: 地图 JSON 的格式号。与导出物的 schema 分开 —— 这是内部产物。
MAP_FORMAT = "pixel-asset/tilemap@1"


@dataclass(frozen=True)
class TileMapResult:
    asset_id: str
    name: str
    path: Path
    width: int
    height: int
    seed: int
    tiles_used: list[str]

    @property
    def single_material(self) -> bool:
        """整张地图只有一种 tile。

        对眼下这套基础地面 tile 这**是正确结果**：邻接表是对角矩阵，而网格连通 ——
        每一步都要求两边相容，于是整张图必然同一种材质。要铺出多材质地图，
        缺的是过渡 tile，不是更好的求解器（PLAN §8.3）。
        """
        return len(self.tiles_used) == 1


def _map_payload(tile_map: TileMap, asset_id: str, name: str) -> dict[str, object]:
    return {
        "format": MAP_FORMAT,
        "asset_id": asset_id,
        "name": name,
        "width": tile_map.width,
        "height": tile_map.height,
        "seed": tile_map.seed,
        "tiles_used": tile_map.tiles_used,
        # 逐行的 tile_id。用 id 而不是索引：索引一旦与 tiles 的顺序脱钩就全错，
        # 而且错得看不出来。
        "rows": [list(row) for row in tile_map.rows],
    }


def create_map(
    asset_dir: str | Path,
    *,
    name: str = "overworld",
    width: int,
    height: int,
    seed: int,
) -> TileMapResult:
    """给一个已经处理好的 tileset 铺一张地图。"""
    store = ArtifactStore(root=Path(asset_dir))
    if not store.manifest_path.exists():
        raise ProcessingError(f"{asset_dir} 下没有 Manifest —— 先跑 create-tileset")

    manifest = AssetManifest.load(store.manifest_path)
    if manifest.tileset is None:
        raise ProcessingError(f"{manifest.asset_id} 不是 tileset，铺不了地图")
    adjacency = manifest.tileset.adjacency
    if adjacency is None:
        raise ProcessingError(
            f"{manifest.asset_id} 的 Manifest 里没有邻接表 —— "
            "它是 8.1 时代的产物，重跑 `create-tileset` 补上（不调用 API）"
        )

    tile_map = generate_map(
        adjacency.right, adjacency.down, width=width, height=height, seed=seed
    )

    store.maps.mkdir(parents=True, exist_ok=True)
    path = atomic_write_json(
        store.maps / f"{name}.json", _map_payload(tile_map, manifest.asset_id, name)
    )

    manifest.tileset.maps[name] = TileMapEntry(
        path=str(path.relative_to(store.root)),
        hash=hash_file(path),
        width=tile_map.width,
        height=tile_map.height,
        seed=tile_map.seed,
        tiles_used=tile_map.tiles_used,
    )
    manifest.save(store.manifest_path)

    logger.info(
        "地图 %s：%d×%d，用到 %d 种 tile（seed=%d）",
        name, tile_map.width, tile_map.height, len(tile_map.tiles_used), seed,
    )
    return TileMapResult(
        asset_id=manifest.asset_id,
        name=name,
        path=path,
        width=tile_map.width,
        height=tile_map.height,
        seed=tile_map.seed,
        tiles_used=tile_map.tiles_used,
    )


def load_map_rows(root: Path, entry: TileMapEntry) -> list[list[str]]:
    """把地图 JSON 读回逐行的 tile_id。验证与导出都要用。"""
    path = root / entry.path
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ProcessingError(f"{path} 里没有 rows")
    return [list(row) for row in rows]
