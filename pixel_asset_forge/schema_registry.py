"""JSON Schema 加载与校验。

为什么 Pydantic 之外还要跑一遍 JSON Schema：

- ``schemas/*.json`` 是**对外契约**（别的工具、别的语言也要读它），
  它必须是权威判据，而不是 Pydantic 模型的附属产物。
- ``additionalProperties: false`` 这类约束能立刻抓出拼错的字段名。
  Pydantic 默认会忽略未知字段，用户就得不到"你把 ``max_color`` 写成单数了"这种提示。

校验错误统一转换为 ``{"path": ..., "message": ...}``，路径用点号 + 数组下标表示，
例如 ``style.target_size.0``。Sprint 1 退出门槛要求错误能准确指出字段路径。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .errors import ConfigError, RequestValidationError, SchemaVersionError

SCHEMA_FILES = {
    "asset-request": "asset-request.schema.json",
    "asset-pack": "asset-pack.schema.json",
    "asset-manifest": "asset-manifest.schema.json",
    "validation-report": "validation-report.schema.json",
}


@lru_cache(maxsize=1)
def schema_dir() -> Path:
    """定位 schemas 目录。

    安装后在包内（``pixel_asset_forge/_schemas``，见 pyproject 的 force-include），
    开发时在仓库根（``<repo>/schemas``）。两处都找不到就是安装坏了，直接报错。
    """
    packaged = Path(__file__).parent / "_schemas"
    if (packaged / SCHEMA_FILES["asset-request"]).exists():
        return packaged

    repo = Path(__file__).parent.parent / "schemas"
    if (repo / SCHEMA_FILES["asset-request"]).exists():
        return repo

    raise ConfigError(
        f"找不到 JSON Schema 目录（已尝试 {packaged} 与 {repo}）。安装可能不完整。"
    )


@lru_cache(maxsize=len(SCHEMA_FILES))
def load_schema(name: str) -> dict[str, Any]:
    if name not in SCHEMA_FILES:
        raise ConfigError(f"未知 schema：{name}")
    path = schema_dir() / SCHEMA_FILES[name]
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@lru_cache(maxsize=len(SCHEMA_FILES))
def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(load_schema(name))


def _format_path(parts: Any) -> str:
    return ".".join(str(p) for p in parts)


def collect_errors(name: str, data: Any) -> list[dict[str, str]]:
    """返回全部校验错误，按字段路径排序。不抛异常。"""
    errors = sorted(_validator(name).iter_errors(data), key=lambda e: list(e.absolute_path))
    return [
        {"path": _format_path(err.absolute_path), "message": err.message}
        for err in errors
    ]


def validate_against(name: str, data: Any, *, what: str) -> None:
    """校验 ``data``，不通过则抛 :class:`RequestValidationError`（含逐字段路径）。"""
    errors = collect_errors(name, data)
    if errors:
        raise RequestValidationError(f"{what} 不符合 {name} schema（{len(errors)} 处问题）", errors)


def parse_version(version: str, *, what: str) -> tuple[int, int]:
    try:
        major, minor = (int(p) for p in version.split(".", 1))
    except ValueError as exc:
        raise SchemaVersionError(f"{what} 的 schema_version 非法：{version!r}") from exc
    return major, minor


def check_schema_version(version: str, *, supported: str, what: str) -> tuple[int, int]:
    """MAJOR 高于当前支持版本时拒绝读取（PLAN §5.4）。

    MINOR 更高是允许的 —— 只需忽略未知字段，这是向前兼容的前提。
    返回解析出的 ``(major, minor)``，供调用方决定是否需要迁移。
    """
    major, minor = parse_version(version, what=what)
    supported_major = int(supported.split(".", 1)[0])
    if major > supported_major:
        raise SchemaVersionError(
            f"{what} 的 schema_version 为 {version}，高于本工具支持的 {supported}。"
            f"请升级 pixel-asset，或运行 `pixel-asset migrate`。"
        )
    return major, minor
