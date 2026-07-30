"""Asset Request —— 流水线的输入（PLAN §5.1）。

与 ``schemas/asset-request.schema.json`` 一一对应。解析顺序是刻意的：

1. 先跑 JSON Schema —— 它是对外契约，能抓出拼错的字段名与非法枚举值，
   并给出逐字段路径。
2. 再构造 Pydantic 模型 —— 补默认值、提供类型安全的访问。

反过来做会丢掉 ``additionalProperties: false`` 的诊断能力。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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

ActionName = Literal[
    "idle", "walk", "attack", "hurt", "death", "cast", "travel", "impact", "loop"
]

ExportTarget = Literal["generic-json", "godot", "phaser", "tiled"]


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

    @field_validator("target_size")
    @classmethod
    def _check_logical_size(cls, value: tuple[int, int]) -> tuple[int, int]:
        for side in value:
            if side not in LOGICAL_SIZES:
                raise ValueError(
                    f"逻辑尺寸只支持 {LOGICAL_SIZES}，收到 {side}"
                )
        return value


class BackgroundSpec(_Base):
    mode: Literal["chroma_key", "transparent_model", "rembg"] = "chroma_key"
    color: str = DEFAULT_KEY_COLOR
    fallback_colors: tuple[str, ...] = DEFAULT_FALLBACK_COLORS
    conflict_hint: str | None = None


class MirroringSpec(_Base):
    enabled: bool
    source_direction: Literal["left", "right"] = "left"
    reason: str | None = None


class AnimationSpec(_Base):
    name: ActionName
    directions: tuple[Direction, ...] | None = None
    frames: int
    fps: int = Field(ge=1, le=60)
    loop: bool = True

    @field_validator("frames")
    @classmethod
    def _check_frames(cls, value: int) -> int:
        if value not in ALLOWED_FRAME_COUNTS:
            raise ValueError(
                f"帧数只支持 {ALLOWED_FRAME_COUNTS}（可映射到合规网格的档位），收到 {value}"
            )
        return value

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
    schema_version: str = "1.0"
    asset_id: str
    asset_type: AssetType
    description: str = Field(min_length=8, max_length=2000)
    style: StyleSpec
    background: BackgroundSpec = BackgroundSpec()
    mirroring: MirroringSpec | None = None
    animations: tuple[AnimationSpec, ...] | None = None
    export: ExportSpec

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

    version = data.get("schema_version", "1.0")
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
