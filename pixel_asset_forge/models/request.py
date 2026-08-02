"""Asset Request —— 流水线的输入（PLAN §5.1）。

与 ``schemas/asset-request.schema.json`` 一一对应。解析顺序是刻意的：

1. 先跑 JSON Schema —— 它是对外契约，能抓出拼错的字段名与非法枚举值，
   并给出逐字段路径。
2. 再构造 Pydantic 模型 —— 补默认值、提供类型安全的访问。

反过来做会丢掉 ``additionalProperties: false`` 的诊断能力。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Final, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .. import REQUEST_SCHEMA_VERSION
from ..constants import (
    ACTION_DEFAULTS,
    ALLOWED_FRAME_COUNTS,
    DEFAULT_FALLBACK_COLORS,
    DEFAULT_KEY_COLOR,
    DIRECTIONS,
    LOGICAL_SIZES,
    Direction,
)
from ..errors import RequestValidationError
from ..schema_registry import check_schema_version, validate_against

AssetType = Literal[
    "character", "prop", "weapon", "projectile", "impact",
    "spell", "pickup", "ui_icon", "environment_object", "tileset",
]
STATIC_ASSET_TYPES: Final[frozenset[AssetType]] = frozenset(
    {"pickup", "weapon", "prop", "ui_icon", "environment_object"}
)

#: 角色的移动形态。姿势节拍按它分支。
#:
#: **必须分。** ``walk`` 的节拍写的是"左脚向前迈、右脚在后"—— 这套描述对没有腿的
#: 角色是灾难：实测史莱姆的 idle / attack / hurt / death 都是无腿的圆团，
#: 唯独 walk 被模型**长出了两条腿和脚**，同一个角色四个动作一个样、第五个换了物种。
#: 用户报的"形象不统一"就是这个。
Locomotion = Literal["biped", "legless", "floating", "quadruped"]

#: 描述里出现这些词就默认走对应的移动形态。
#:
#: 只在请求没写 ``locomotion`` 时兜底 —— 显式写了以显式的为准。存量 YAML
#: 一个字不用改也能立刻受益，这是它存在的唯一理由。词表刻意保守：
#: 宁可漏判回落到 biped（现状），也不要把一个有腿的角色判成无腿。
_LOCOMOTION_HINTS: Final[tuple[tuple[Locomotion, tuple[str, ...]], ...]] = (
    ("legless", ("slime", "blob", "ooze", "jelly", "pudding", "snake", "serpent",
                 "worm", "caterpillar", "snail", "史莱姆", "黏液", "软泥", "蛇")),
    ("floating", ("ghost", "spirit", "wraith", "phantom", "eyeball", "floating",
                  "hovering", "levitating", "cloud", "wisp", "orb",
                  "幽灵", "魂", "漂浮", "悬浮")),
    ("quadruped", ("wolf", "dog", "cat", "horse", "boar", "bear", "spider",
                   "lizard", "beetle", "crab", "quadruped", "four-legged",
                   "狼", "犬", "猫", "马", "熊", "蜘蛛", "四足")),
)


def infer_locomotion(description: str) -> Locomotion:
    """从描述里猜移动形态。猜不出回落 ``biped``。

    拉丁词按**整词**匹配，不能用子串 —— ``bear`` 命中过 "white bear**d**"，
    把一个拄杖的老法师判成了四足动物，走路节拍于是要求它用对角腿交替。
    中日韩没有词边界，仍按子串匹配。
    """
    lowered = description.lower()
    for kind, words in _LOCOMOTION_HINTS:
        for word in words:
            if word.isascii():
                if re.search(rf"\b{re.escape(word)}\b", lowered):
                    return kind
            elif word in lowered:
                return kind
    return "biped"


#: 内置动作。它们自带姿势模板与 per-action 验证阈值。
BUILTIN_ACTIONS: Final = (
    "idle", "walk", "attack", "hurt", "death", "cast", "travel", "impact", "loop",
)

#: 自定义动作名的形状：小写字母、数字、下划线。
#:
#: 不能带 ``_`` 之外的分隔符 —— 动作键是 ``{action}_{direction}``，
#: 名字里再有分隔符就没法反解出方向了。
ACTION_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"

#: 自定义动作的循环方式。对应 ``PoseCycle`` 的三个开关。
CycleKind = Literal["one_shot", "loop", "gait"]

ExportTarget = Literal["generic-json", "godot", "phaser", "tiled"]
HexColor = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StyleSpec(_Base):
    perspective: Literal["top_down_3_4", "top_down", "side_view", "isometric"]
    target_size: tuple[int, int]
    max_colors: int = Field(ge=2, le=256)
    outline: Literal["none", "single_pixel_dark", "single_pixel_colored"] = "single_pixel_dark"
    shading: Literal["flat", "two_tone", "three_tone"] = "two_tone"
    antialiasing: bool = False
    lighting: Literal["fixed_top_left", "fixed_top", "fixed_top_right", "none"] = "fixed_top_left"
    strict_lighting: bool = False
    """true 时禁用一切镜像，四方向全部独立生成（ADR-006）。"""
    palette_preset: str | None = None
    palette_colors: tuple[HexColor, ...] | None = None
    """显式锁定的共享色板。pack 展开时写入；数量不得超过 ``max_colors``。"""

    @field_validator("target_size")
    @classmethod
    def _check_logical_size(cls, value: tuple[int, int]) -> tuple[int, int]:
        for side in value:
            if side not in LOGICAL_SIZES:
                raise ValueError(
                    f"逻辑尺寸只支持 {LOGICAL_SIZES}，收到 {side}"
                )
        return value

    @field_validator("palette_colors")
    @classmethod
    def _check_palette_not_empty(
        cls, value: tuple[HexColor, ...] | None
    ) -> tuple[HexColor, ...] | None:
        if value is not None and not value:
            raise ValueError("显式 palette_colors 不能为空")
        return value

    @model_validator(mode="after")
    def _check_palette_limit(self) -> StyleSpec:
        if self.palette_colors is not None and len(self.palette_colors) > self.max_colors:
            raise ValueError(
                f"显式色板有 {len(self.palette_colors)} 色，超过 max_colors={self.max_colors}"
            )
        return self


class BackgroundSpec(_Base):
    mode: Literal["chroma_key", "transparent_model", "rembg"] = "chroma_key"
    color: str = DEFAULT_KEY_COLOR
    fallback_colors: tuple[str, ...] = DEFAULT_FALLBACK_COLORS
    conflict_hint: str | None = None


class TileSpec(_Base):
    """tileset 里的一块 tile。"""

    tile_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    description: str = Field(min_length=8, max_length=2000)


class TilesetSpec(_Base):
    """一套 tile 的共同约束。

    ``tile_size`` 不复用 ``style.target_size``：静态资产那套说的是"内容占画布的
    比例"，而 tile 要的是**精确等于**这个尺寸，两者不是一回事（PLAN §8.1）。
    """

    tile_size: tuple[int, int]
    tiles: tuple[TileSpec, ...] = Field(min_length=1)

    @field_validator("tile_size")
    @classmethod
    def _check_tile_size(cls, value: tuple[int, int]) -> tuple[int, int]:
        for side in value:
            if side not in LOGICAL_SIZES:
                raise ValueError(f"逻辑尺寸只支持 {LOGICAL_SIZES}，收到 {side}")
        return value

    @model_validator(mode="after")
    def _check_unique_tile_ids(self) -> TilesetSpec:
        ids = [tile.tile_id for tile in self.tiles]
        duplicates = sorted({t for t in ids if ids.count(t) > 1})
        if duplicates:
            raise ValueError(f"tile_id 必须唯一，重复的有：{', '.join(duplicates)}")
        return self


class MirroringSpec(_Base):
    enabled: bool
    source_direction: Literal["left", "right"] = "left"
    reason: str | None = None


class BeatSpec(_Base):
    """自定义动作的一拍。"""

    name: str = Field(min_length=1, max_length=32)
    """节拍名，如 ``WINDUP``。会原样出现在 prompt 里，用大写更醒目。"""

    description: str = Field(min_length=8)
    """这一拍身体在做什么。**必须具体到肢体**。

    "准备攻击"这种描述模型不会自己拆解成姿势 —— Sprint 0 / A-2 实测
    整体描述会产出 N 张几乎一样的站姿。写成"重心压到后脚、武器拉到肩后、
    身体蓄力"才画得出来。
    """


class AnimationSpec(_Base):
    name: str = Field(pattern=ACTION_NAME_PATTERN)
    directions: tuple[Direction, ...] | None = None
    frames: int
    fps: int = Field(ge=1, le=60)
    loop: bool = True

    beats: tuple[BeatSpec, ...] | None = None
    """自定义动作的节拍序列。内置动作留空即可（用内置模板）。

    **模板外的动作必须给 beats。** 代码不猜 —— 静默退回泛泛描述正是
    Sprint 0 里产出一排站姿的原因，把已知失败模式请回来没有意义。
    """

    cycle: CycleKind | None = None
    """节拍怎么展开成帧。只对自定义动作有效。

    - ``one_shot`` —— 一次性动作（挥砍、倒地）。采样保留首尾，丢了起手或收势就不成立。
    - ``loop`` —— 循环动作（待机、悬浮）。循环采样，不重复首尾。
    - ``gait`` —— 步态。beats 描述**半个**周期，另一半由左右互换生成，
      所以每一拍都必须带 left/right 字样，否则互换后与原文一字不差。
    """

    @field_validator("frames")
    @classmethod
    def _check_frames(cls, value: int) -> int:
        if value not in ALLOWED_FRAME_COUNTS:
            raise ValueError(
                f"帧数只支持 {ALLOWED_FRAME_COUNTS}（可映射到合规网格的档位），收到 {value}"
            )
        return value

    @model_validator(mode="after")
    def _custom_actions_need_beats(self) -> AnimationSpec:
        """模板外的动作必须自带节拍。

        这条与 ``pose_sequence`` 对未知动作抛错是同一条原则：拿不准就报错，
        不要猜。静默退回"画一个 dodge_roll 动画"这种整体描述，
        产出的是 N 张几乎一样的站姿（Sprint 0 / A-2）。
        """
        if self.name in BUILTIN_ACTIONS:
            if self.beats is not None:
                raise ValueError(
                    f"{self.name} 是内置动作，已有姿势模板 —— 不要再给 beats。"
                    f"要改它的姿势就换个自定义动作名。"
                )
            return self
        if not self.beats:
            builtin = " / ".join(BUILTIN_ACTIONS)
            raise ValueError(
                f"{self.name!r} 不是内置动作（内置的有 {builtin}），必须给 beats "
                f"说明每一拍身体在做什么。代码不会替你猜 —— "
                f"泛泛的整体描述实测会产出一排几乎一样的站姿。"
            )
        if self.name.rsplit("_", 1)[-1] in DIRECTIONS:
            raise ValueError(
                f"动作名 {self.name!r} 以方向词结尾 —— 动作键是 {{action}}_{{direction}}，"
                f"{self.name}_down 与 {self.name.rsplit('_', 1)[0]} 朝 "
                f"{self.name.rsplit('_', 1)[-1]} 从字符串上分不开。换个名字，"
                f"比如把 charge_up 叫成 charging。"
            )
        if len(self.beats) < 2:
            raise ValueError(f"{self.name} 只给了 1 拍，动画至少要两拍才有变化")
        if self.cycle == "gait":
            missing = [
                beat.name for beat in self.beats
                if "left" not in beat.description.lower()
                and "right" not in beat.description.lower()
            ]
            if missing:
                raise ValueError(
                    f"{self.name} 是 gait，另外半个周期靠左右互换生成，"
                    f"所以每一拍都要带 left/right 字样。这些拍没有：{missing}。"
                    f"互换后它们与原文一字不差，与 no two cells may be identical 冲突。"
                )
        return self

    def resolved_directions(self, asset_type: AssetType) -> tuple[Direction, ...]:
        """未声明 directions 时的补全规则。

        - 角色资产的方向性动作 → 四方向全出。
        - ``impact`` 这类各向同性动作 → 无方向（返回空元组，产生单个无方向任务）。
        """
        if self.directions is not None:
            return self.directions
        default = ACTION_DEFAULTS.get(self.name)
        directional = default.directional if default else True
        if asset_type == "character" and directional:
            return DIRECTIONS
        return ()


class ExportSpec(_Base):
    targets: tuple[ExportTarget, ...] = Field(min_length=1)


class AssetRequest(_Base):
    schema_version: str = REQUEST_SCHEMA_VERSION
    asset_id: str
    asset_type: AssetType
    description: str = Field(min_length=8, max_length=2000)
    locomotion: Locomotion | None = None
    """角色怎么移动。留空则从 ``description`` 推断（见 :func:`infer_locomotion`）。"""
    style: StyleSpec
    background: BackgroundSpec = BackgroundSpec()
    mirroring: MirroringSpec | None = None
    animations: tuple[AnimationSpec, ...] | None = None
    tileset: TilesetSpec | None = None
    export: ExportSpec

    @model_validator(mode="after")
    def _check_tileset_contract(self) -> AssetRequest:
        """``tileset`` 与其余资产类型互斥。

        JSON Schema 那层还额外拒收 tileset 上的 ``background`` —— 那个字段有默认值，
        到 pydantic 这里已经分不清"没写"和"写了默认值"了。
        """
        if self.asset_type == "tileset":
            if self.tileset is None:
                raise ValueError("tileset 资产必须声明 tileset.tiles")
            if self.animations:
                raise ValueError("tileset 是静态贴图，不接受 animations")
        elif self.tileset is not None:
            raise ValueError(f"{self.asset_type} 不接受 tileset 字段")
        return self

    @property
    def tile_list(self) -> tuple[TileSpec, ...]:
        return self.tileset.tiles if self.tileset else ()

    @property
    def mirroring_enabled(self) -> bool:
        """镜像的最终生效值。

        ``strict_lighting`` 是一票否决 —— 它的语义就是"宁可承担身份漂移风险，
        也不要左上角光源被翻到右上角"（ADR-006）。
        """
        if self.style.strict_lighting:
            return False
        return bool(self.mirroring and self.mirroring.enabled)

    @property
    def mirror_source(self) -> Direction:
        return self.mirroring.source_direction if self.mirroring else "left"

    @property
    def resolved_locomotion(self) -> Locomotion:
        """显式优先，其次按描述推断。prompt 只该读这个，不该读原始字段。"""
        return self.locomotion or infer_locomotion(self.description)

    def animation_list(self) -> tuple[AnimationSpec, ...]:
        return self.animations or ()


def parse_request(data: dict[str, Any], *, source: str = "<内存>") -> AssetRequest:
    """校验并解析一份 request 字典。

    先 JSON Schema 后 Pydantic；两层的错误都带字段路径。
    """
    if not isinstance(data, dict):
        raise RequestValidationError(
            f"{source}：请求必须是一个 YAML 映射（字典），实际是 {type(data).__name__}"
        )

    version = data.get("schema_version", REQUEST_SCHEMA_VERSION)
    if not isinstance(version, str):
        raise RequestValidationError(
            f"{source}：schema_version 必须是字符串",
            [{"path": "schema_version", "message": f"收到 {type(version).__name__}"}],
        )
    check_schema_version(version, supported=REQUEST_SCHEMA_VERSION, what=source)

    validate_against("asset-request", data, what=source)

    try:
        return AssetRequest.model_validate(data)
    except ValidationError as exc:  # pragma: no cover - schema 通过后极少触发
        errors = [
            {"path": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
            for e in exc.errors()
        ]
        raise RequestValidationError(f"{source}：请求解析失败", errors) from exc


def load_request(path: str | Path) -> AssetRequest:
    """从 YAML 文件读取 Asset Request。"""
    p = Path(path)
    if not p.exists():
        raise RequestValidationError(f"找不到请求文件：{p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RequestValidationError(f"{p}：YAML 解析失败 —— {exc}") from exc
    return parse_request(data, source=str(p))
