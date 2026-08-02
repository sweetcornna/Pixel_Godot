"""Asset Manifest —— 资产的唯一真实来源（PLAN §5.2 / ADR-001）。

硬性约束：**所有导出文件都必须能仅凭 Manifest + ``frames/`` 重建。**
凡是导出器需要而 Manifest 里没有的信息，都是 Manifest 的缺陷，不是导出器的。

``background.color_used`` 尤其重要 —— 没有它，``process`` 就无法脱离原始请求
离线复现键控结果（ADR-004）。
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from .. import MANIFEST_SCHEMA_VERSION, PIPELINE_VERSION
from ..constants import ESCALATED_STAGES, FallbackStage
from ..errors import ProcessingError
from ..schema_registry import check_schema_version, validate_against
from ..storage.atomic import atomic_write_json

HexColor = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")]


def _asset_relative_path(value: str) -> str:
    # 消费方全部用 `store.root / value` 拼接后直接读写;绝对路径或含 `..` 的值
    # 会把读写点带出资产目录(process 重跑时先写盘后检查,后置检查拦不住)。
    for pure in (PurePosixPath(value), PureWindowsPath(value)):
        if not value or pure.is_absolute() or pure.drive or ".." in pure.parts:
            raise ValueError(
                f"必须是资产目录内的相对路径(不得为绝对路径或包含 `..`):{value!r}"
            )
    return value


AssetRelativePath = Annotated[str, AfterValidator(_asset_relative_path)]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderInfo(_Base):
    name: Literal["openai", "mock", "replay"]
    model: str
    """实际使用的模型。透明背景降级会切到 gpt-image-1.5（ADR-004）。"""


class CanvasInfo(_Base):
    width: int = Field(ge=1, le=512)
    height: int = Field(ge=1, le=512)


class AnchorInfo(_Base):
    type: Literal["bottom_center", "center", "top_left"] = "bottom_center"
    x: float = Field(ge=0, le=1, default=0.5)
    y: float = Field(ge=0, le=1, default=1.0)


class BackgroundInfo(_Base):
    mode: Literal["chroma_key", "transparent_model", "rembg", "opaque"]
    """``opaque`` 表示这张图满幅不透明，**从未做过去背景**（tileset 走这条）。

    不是"抠了但没抠出东西"，而是这一步压根没执行 —— 所以下面三个字段对它无意义，
    留空而不是填占位值。
    """

    color_requested: HexColor | None = None
    color_used: HexColor | None = None
    fallback_stage: FallbackStage | None = None

    key_threshold: float | None = Field(default=None, ge=0)
    """**种子图**的自适应色键阈值。

    各动作的阈值另见 :attr:`GeneratedAnimation.key_threshold` ——
    阈值逐图求解，不能共用一个。
    """

    @model_validator(mode="after")
    def _check_keying_fields(self) -> BackgroundInfo:
        if self.mode == "opaque":
            return self
        missing = [
            name
            for name in ("color_requested", "color_used", "fallback_stage")
            if getattr(self, name) is None
        ]
        if missing:
            raise ValueError(f"{self.mode} 背景必须记录 {'、'.join(missing)}")
        return self

    @property
    def key_color(self) -> HexColor:
        """实际生效的键控色。

        做成属性而不是让调用方各自处理 ``None``：会读这个值的全是动画与静态资产
        的处理链，它们结构上必然做过去背景。真读到 opaque 说明调用路径本身错了，
        该当场炸掉，而不是让一个 ``None`` 悄悄流到下游。
        """
        if self.color_used is None:
            raise ProcessingError(
                f"{self.mode} 背景没有键控色 —— 这条链不该走到需要键控色的地方"
            )
        return self.color_used

    @property
    def downgraded(self) -> bool:
        """与请求色不同即说明触发了冲突降级（PLAN §2.4.1）。

        没做过去背景就谈不上降级 —— opaque 恒为 False。
        """
        if self.color_used is None or self.color_requested is None:
            return False
        return self.color_used.upper() != self.color_requested.upper()

    @property
    def escalated(self) -> bool:
        """是否升到了生成后的兜底档位。触发即应在验证报告中告警。"""
        return self.fallback_stage in ESCALATED_STAGES


class PaletteInfo(_Base):
    max_colors: int = Field(ge=2, le=256)
    colors: list[HexColor] = Field(default_factory=list)


class ScaleProfileInfo(_Base):
    """跨动作缩放基准（Sprint 6）。

    没有它，每个动作各自填满画布 —— 同一个角色在 idle 与 death 之间会变大变小。
    实测源图里差 40% 体型的两个动作，输出后高度完全相同。
    """

    reference: str
    subject_ratio: float = Field(gt=0)
    canvas_fraction: float = Field(gt=0)
    needs_reprocess: bool = False
    """基准曾被别的动作顶替；全量 ``process`` 成功后清零。"""


class MirroringInfo(_Base):
    enabled: bool | None = None
    source_direction: Literal["left", "right"] | None = None
    strict_lighting: bool | None = None


class GridInfo(_Base):
    cols: int = Field(ge=1, le=12)
    """列数上限 12：单行条带把整条帧序列排成一行，6 帧就是 6 列（constants.STRIP_LAYOUTS）。"""

    rows: int = Field(ge=1, le=3)

    cell: tuple[int, int]
    """**实际**单元格尺寸，不是名义 512×512。"""

    requested_size: tuple[int, int] | None = None
    """提交给 API 的尺寸。"""

    actual_size: tuple[int, int] | None = None
    """实际返回图尺寸。

    端点不保证按请求尺寸返回且不报错（Sprint 0 / A-1）。切帧按比例进行，
    没有这个字段就无法离线复现当初的格线位置。
    """

    @property
    def snapped(self) -> bool:
        """端点是否发生了尺寸吸附。"""
        if self.requested_size is None or self.actual_size is None:
            return False
        return self.requested_size != self.actual_size


class StaticImageInfo(_Base):
    """静态资产的原图、处理产物与确定性处理参数。"""

    source_image: AssetRelativePath
    image: AssetRelativePath
    requested_size: tuple[int, int]
    actual_size: tuple[int, int]
    key_threshold: float = Field(ge=0)
    grid_block_size: float | None = Field(default=None, gt=0)
    source_hash: str = Field(pattern=r"^[0-9A-Fa-f]{64}$")
    processed_hash: str = Field(pattern=r"^[0-9A-Fa-f]{64}$")


class TileEntry(_Base):
    """tileset 里一块 tile 的原图与成品。"""

    source_image: AssetRelativePath
    image: AssetRelativePath
    source_hash: str = Field(pattern=r"^[0-9A-Fa-f]{64}$")
    processed_hash: str = Field(pattern=r"^[0-9A-Fa-f]{64}$")


class TilesetInfo(_Base):
    """整套 tile 的产物记录。

    ``tile_size`` 记在这里而不是复用 ``canvas``：canvas 说的是"这个资产的画布"，
    对 tileset 而言每块 tile 各占一格，两者恰好相等只是巧合，不该靠巧合读数。
    """

    tile_size: tuple[int, int]
    tiles: dict[str, TileEntry] = Field(min_length=1)


class GeneratedAnimation(_Base):
    fps: int = Field(ge=1, le=60)
    loop: bool
    grid: GridInfo | None = None

    source_image: AssetRelativePath | None = None
    """``source/`` 下的原始生成图路径。永不覆盖。"""

    key_threshold: float | None = Field(default=None, ge=0)
    """本动作原图的自适应色键阈值。

    **阈值是逐图求解的**，所以必须逐动作记录。只在 ``background`` 上存一个
    资产级阈值会导致：第一次 process 时每张图各自求解，写回时只留下其中一个；
    第二次 process 强制所有图共用那一个 —— 除第一张外全部产出改变，
    ``process`` 就不幂等了，离线复现的承诺随之失效（ADR-004）。
    """

    frames: list[AssetRelativePath] = Field(min_length=1)

    keyframe_count: int | None = Field(default=None, ge=2)
    """这段动作里有多少张是**用户给的关键帧**（其余是补出来的）。"""

    keyframe_fps: int | None = Field(default=None, ge=1)
    """关键帧序列**自己**的帧率。

    补完之后 ``fps`` 会被改成目标帧率，关键帧原本的帧率就没处找了 ——
    再想重算一次补间预算就成了无解。实测踩过：补完一次之后再跑 interpolate，
    它按 9fps 算出目标 9 帧、发现盘上正好 9 帧，于是判定"不需要补间"，
    而盘上真正的关键帧只有 3 张。
    """


class DerivedAnimation(_Base):
    derived_from: str
    transform: Literal["flip_horizontal", "flip_vertical"]


AnimationEntry = GeneratedAnimation | DerivedAnimation

ManifestStatus = Literal[
    "planned", "generating", "generated", "processing", "processed",
    "validating", "validated", "validation_failed", "repairing",
    "awaiting_approval", "approved", "exported", "failed",
]


#: 1.x 的 fallback_stage 序号 → 2.0 的具名标识（ADR-004 删掉了「精确色键」档）。
#:
#: 1.x 的第 1 档「精确色键」在 2.0 里不存在，它与第 3 档「宽容距离键控」
#: 合并为 tolerant_key —— 因为 1.x 的实现里第 1 档必然失败并落到第 3 档，
#: 所以任何标着"第 1 档"的旧 manifest，实际生效的都是第 3 档。
_STAGE_MIGRATION_1X: dict[int, FallbackStage] = {
    1: "tolerant_key",
    2: "alt_key_color",
    3: "tolerant_key",
    4: "transparent_model",
    5: "rembg",
    6: "manual",
}


def migrate_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """把 1.x 的 manifest 就地升级为 2.0（PLAN §5.4 要求 MAJOR 升级附带迁移路径）。

    只做两件事：``fallback_stage`` 序号 → 具名标识、``schema_version`` 改写。
    ``grid.actual_size`` 无法追溯（旧 manifest 根本没记），保持缺省 ——
    这意味着 1.x 资产不能靠 ``process`` 精确重跑切帧，只能重新生成。
    这是初版没记录实际尺寸的代价，不是迁移的缺陷。
    """
    out = dict(data)
    background = out.get("background")
    if isinstance(background, dict) and isinstance(background.get("fallback_stage"), int):
        background = dict(background)
        stage = background["fallback_stage"]
        background["fallback_stage"] = _STAGE_MIGRATION_1X.get(stage, "manual")
        out["background"] = background
    out["schema_version"] = MANIFEST_SCHEMA_VERSION
    return out


class AssetManifest(_Base):
    schema_version: str = MANIFEST_SCHEMA_VERSION
    asset_id: str
    asset_type: str
    pipeline_version: str = PIPELINE_VERSION
    provider: ProviderInfo
    canvas: CanvasInfo
    anchor: AnchorInfo = AnchorInfo()
    background: BackgroundInfo
    palette: PaletteInfo
    mirroring: MirroringInfo | None = None
    scale_profile: ScaleProfileInfo | None = None
    static_image: StaticImageInfo | None = None
    tileset: TilesetInfo | None = None
    animations: dict[str, AnimationEntry] = Field(default_factory=dict)
    sheets: dict[str, AssetRelativePath] = Field(default_factory=dict)
    status: ManifestStatus = "planned"

    # -- 序列化 -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    def validate_schema(self) -> None:
        """自检：确保产出的 Manifest 符合对外契约。写盘前必调。"""
        validate_against("asset-manifest", self.to_dict(), what=f"{self.asset_id} 的 manifest")

    def save(self, path: str | Path) -> Path:
        self.validate_schema()
        return atomic_write_json(path, self.to_dict())

    @classmethod
    def load(cls, path: str | Path) -> AssetManifest:
        p = Path(path)
        if not p.exists():
            raise ProcessingError(f"找不到 manifest：{p}")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProcessingError(f"{p}：manifest 不是合法 JSON —— {exc}") from exc

        version = str(data.get("schema_version", MANIFEST_SCHEMA_VERSION))
        major, _minor = check_schema_version(
            version, supported=MANIFEST_SCHEMA_VERSION, what=str(p)
        )
        if major < 2:
            data = migrate_to_v2(data)

        validate_against("asset-manifest", data, what=str(p))
        try:
            return cls.model_validate(data)
        except ValidationError as exc:  # pragma: no cover
            raise ProcessingError(f"{p}：manifest 解析失败 —— {exc}") from exc

    # -- 便利访问 ---------------------------------------------------------

    def generated_animations(self) -> dict[str, GeneratedAnimation]:
        return {k: v for k, v in self.animations.items() if isinstance(v, GeneratedAnimation)}

    def derived_animations(self) -> dict[str, DerivedAnimation]:
        return {k: v for k, v in self.animations.items() if isinstance(v, DerivedAnimation)}

    def resolve_frames(self, key: str) -> list[str]:
        """取某个动作的帧列表；derived 动作回溯到源动作。

        derived 链只允许一层（``right → left``），出现多层说明规划有 bug。
        """
        entry = self.animations.get(key)
        if entry is None:
            raise ProcessingError(f"{self.asset_id}：manifest 中没有动作 {key}")
        if isinstance(entry, GeneratedAnimation):
            return list(entry.frames)
        source = self.animations.get(entry.derived_from)
        if not isinstance(source, GeneratedAnimation):
            raise ProcessingError(
                f"{self.asset_id}：{key} derive 自 {entry.derived_from}，但后者不是生成型动作"
            )
        return list(source.frames)
