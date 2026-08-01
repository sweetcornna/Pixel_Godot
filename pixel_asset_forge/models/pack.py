"""Asset Pack —— 一组共享静态生成设置的 pickup 请求。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .. import PACK_SCHEMA_VERSION, PIPELINE_VERSION, REQUEST_SCHEMA_VERSION
from ..errors import RequestValidationError
from ..schema_registry import check_schema_version, validate_against
from .request import AssetRequest, BackgroundSpec, ExportSpec, HexColor, StyleSpec


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PackPalette(_Base):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    colors: tuple[HexColor, ...] = Field(min_length=1)


class PackShared(_Base):
    style: StyleSpec
    background: BackgroundSpec
    export: ExportSpec
    palette: PackPalette


class PackAsset(_Base):
    asset_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_]*$", min_length=1, max_length=64)
    description: str = Field(min_length=8, max_length=2000)


class PotionPack(_Base):
    schema_version: str = PACK_SCHEMA_VERSION
    pack_type: Literal["potion_pack"]
    pack_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_]*$", min_length=1, max_length=64)
    shared: PackShared
    assets: tuple[PackAsset, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_pack_contract(self) -> PotionPack:
        ids = [asset.asset_id for asset in self.assets]
        duplicates = sorted({asset_id for asset_id in ids if ids.count(asset_id) > 1})
        if duplicates:
            raise ValueError(f"pack 中 asset_id 重复：{'、'.join(duplicates)}")

        count = len(self.shared.palette.colors)
        if count > self.shared.style.max_colors:
            raise ValueError(
                f"共享色板有 {count} 色，超过 style.max_colors={self.shared.style.max_colors}"
            )

        if self.shared.background.mode == "chroma_key":
            palette = {color.upper() for color in self.shared.palette.colors}
            key_colors = (
                self.shared.background.color,
                *self.shared.background.fallback_colors,
            )
            if key_colors and all(color.upper() in palette for color in key_colors):
                colors = "、".join(key_colors)
                raise ValueError(
                    f"全部候选键控色都出现在显式共享色板中：{colors}。"
                    "请换一个 background.color/fallback_colors，或改用 transparent_model。"
                )
        return self

    def expand_requests(self) -> tuple[AssetRequest, ...]:
        """按 pack 顺序展开为无动画的静态 pickup 请求。"""
        style = self.shared.style.model_copy(
            update={
                "palette_preset": self.shared.palette.name,
                "palette_colors": self.shared.palette.colors,
            }
        )
        return tuple(
            AssetRequest(
                schema_version=REQUEST_SCHEMA_VERSION,
                asset_id=asset.asset_id,
                asset_type="pickup",
                description=asset.description,
                style=style,
                background=self.shared.background,
                animations=None,
                export=self.shared.export,
            )
            for asset in self.assets
        )


def parse_pack(data: dict[str, Any], *, source: str = "<内存>") -> PotionPack:
    """先按公开 JSON Schema、再按 Pydantic 模型解析 pack。"""
    if not isinstance(data, dict):
        raise RequestValidationError(
            f"{source}：pack 必须是一个 YAML 映射（字典），实际是 {type(data).__name__}"
        )

    version = data.get("schema_version", PACK_SCHEMA_VERSION)
    if not isinstance(version, str):
        raise RequestValidationError(
            f"{source}：schema_version 必须是字符串",
            [{"path": "schema_version", "message": f"收到 {type(version).__name__}"}],
        )
    check_schema_version(version, supported=PACK_SCHEMA_VERSION, what=source)
    validate_against("asset-pack", data, what=source)

    try:
        return PotionPack.model_validate(data)
    except ValidationError as exc:
        errors = [
            {"path": ".".join(str(part) for part in error["loc"]), "message": error["msg"]}
            for error in exc.errors()
        ]
        summary = "；".join(error["message"] for error in errors)
        raise RequestValidationError(
            f"{source}：pack 解析失败 —— {summary}",
            errors,
        ) from exc


def load_pack(path: str | Path) -> PotionPack:
    """从 YAML 文件读取 potion pack。"""
    pack_path = Path(path)
    if not pack_path.exists():
        raise RequestValidationError(f"找不到 pack 文件：{pack_path}")
    try:
        data = yaml.safe_load(pack_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RequestValidationError(f"{pack_path}：YAML 解析失败 —— {exc}") from exc
    return parse_pack(data, source=str(pack_path))


def expand_pack(pack: PotionPack) -> tuple[AssetRequest, ...]:
    """函数式入口，等价于 :meth:`PotionPack.expand_requests`。"""
    return pack.expand_requests()


def input_fingerprint(request: AssetRequest, provider: str, model: str) -> str:
    """请求、流水线版本与生成后端的稳定 SHA-256 指纹。"""
    payload = {
        "pipeline_version": PIPELINE_VERSION,
        "provider": provider,
        "model": model,
        "request": request.model_dump(mode="json", exclude_none=True),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
