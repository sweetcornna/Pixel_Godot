"""导出器基类。

硬性约束（ADR-001）：**所有导出文件必须能仅凭 Manifest + ``frames/`` 重建。**
凡是导出器需要而 Manifest 里没有的信息，都是 Manifest 的缺陷，不是导出器的 ——
导出器不许自己去猜、去测量、去从文件名反推。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from ..errors import ExportError
from ..models.manifest import AssetManifest, DerivedAnimation, GeneratedAnimation


@dataclass(frozen=True, slots=True)
class AnimationView:
    """导出器看到的一个动作。derived 与 generated 在这里被抹平。"""

    key: str
    fps: int
    loop: bool
    frames: list[str]
    derived_from: str | None = None

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def duration(self) -> float:
        return self.frame_count / self.fps if self.fps else 0.0


@dataclass(frozen=True, slots=True)
class TileView:
    """导出器看到的一块 tile。"""

    tile_id: str
    image: str


def tile_views(manifest: AssetManifest) -> list[TileView]:
    """把 Manifest 的 tileset 展开成导出器要的形态。

    按 ``tile_id`` 排序 —— 图集里的坐标必须可复现，否则同一套 tile 每次导出
    落在不同格子上，地图里已经摆好的 tile 会集体错位。
    """
    if manifest.tileset is None:
        return []
    return [
        TileView(tile_id=tile_id, image=entry.image)
        for tile_id, entry in sorted(manifest.tileset.tiles.items())
    ]


def load_tiles(root: Path, views: list[TileView]) -> list[np.ndarray]:
    images = []
    for view in views:
        path = root / view.image
        if not path.is_file():
            raise ExportError(f"tile {view.tile_id} 的成品缺失：{path}")
        images.append(np.array(Image.open(path).convert("RGBA")))
    return images


@dataclass
class ExportResult:
    target: str
    files: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return f"{self.target}：{len(self.files)} 个文件"


def animation_views(manifest: AssetManifest, root: Path) -> list[AnimationView]:
    """把 Manifest 的动作展开成导出器要的形态。

    ``derived`` 动作在磁盘上有自己的帧（翻转后写盘），所以这里优先用它们；
    只有在磁盘上没有时才回溯到源方向 —— 后者意味着导出的是**未翻转**的帧，
    必须让调用方知道，不能悄悄导出反的。
    """
    views: list[AnimationView] = []
    for key in sorted(manifest.animations):
        entry = manifest.animations[key]

        if isinstance(entry, GeneratedAnimation):
            views.append(
                AnimationView(key=key, fps=entry.fps, loop=entry.loop, frames=list(entry.frames))
            )
            continue

        if not isinstance(entry, DerivedAnimation):  # pragma: no cover - 类型收窄
            continue

        source = manifest.animations.get(entry.derived_from)
        if not isinstance(source, GeneratedAnimation):
            raise ExportError(
                f"{key} derive 自 {entry.derived_from}，但后者不是生成型动作 —— "
                "Manifest 不自洽，无法导出"
            )

        own = sorted((root / "frames" / key).glob("*.png"))
        if own:
            frames = [str(p.relative_to(root)) for p in own]
        else:
            raise ExportError(
                f"{key} 是镜像派生动作，但 frames/{key}/ 下没有帧文件。"
                "先跑 create-animation 生成它 —— 直接引用源方向的帧会导出成朝向相反的资产。"
            )

        views.append(
            AnimationView(
                key=key, fps=source.fps, loop=source.loop,
                frames=frames, derived_from=entry.derived_from,
            )
        )
    return views


def load_frames(root: Path, view: AnimationView) -> list[np.ndarray]:
    frames = []
    for relative in view.frames:
        path = root / relative
        if not path.exists():
            raise ExportError(f"{view.key} 的帧文件缺失：{relative}")
        frames.append(np.array(Image.open(path).convert("RGBA")))
    return frames


def load_static_image(manifest: AssetManifest, root: Path) -> np.ndarray:
    entry = manifest.static_image
    if entry is None:
        raise ExportError(f"{manifest.asset_id} 的 Manifest 没有 static_image")
    path = root / entry.image
    if not path.exists():
        raise ExportError(f"静态成品文件缺失：{entry.image}")
    return np.array(Image.open(path).convert("RGBA"))


class Exporter(ABC):
    """导出器接口。"""

    target: str = "base"

    @abstractmethod
    def export(self, manifest: AssetManifest, root: Path, out_dir: Path) -> ExportResult:
        """把资产导出到 ``out_dir``。"""

    @staticmethod
    def ensure_dir(path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        return path
