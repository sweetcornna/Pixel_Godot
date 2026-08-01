"""配置加载。

优先级（PLAN §8 Sprint 1）：**命令行覆盖 > 环境变量 > 项目级 YAML > 用户级 YAML > 内置默认值**。

API Key 只从环境变量读，**永远不从配置文件读、也永远不写入配置文件**。
这不是为了省事 —— 是为了让"Key 泄漏到仓库里"这件事在结构上不可能发生。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .constants import (
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_MAX_REPAIR_ROUNDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
)
from .errors import ConfigError, MissingApiKeyError

PROJECT_CONFIG_NAME = "pixel-asset.yaml"
USER_CONFIG_PATH = Path.home() / ".config" / "pixel-asset" / "config.yaml"

#: 按序探测，第一个非空的胜出。
API_KEY_ENV_VARS = ("PIXEL_ASSET_API_KEY", "OPENAI_API_KEY")

ENV_PREFIX = "PIXEL_ASSET_"

#: 环境变量名 → 配置字段。刻意用白名单：不希望任意环境变量都能改行为。
_ENV_FIELDS = {
    "PIXEL_ASSET_PROVIDER": "provider",
    "PIXEL_ASSET_MODEL": "model",
    "PIXEL_ASSET_FALLBACK_MODEL": "fallback_model",
    "PIXEL_ASSET_OUTPUT_DIR": "output_dir",
    "PIXEL_ASSET_MAX_CONCURRENCY": "max_concurrency",
    "PIXEL_ASSET_RPM": "requests_per_minute",
    "PIXEL_ASSET_MAX_RETRIES": "max_retries",
    "PIXEL_ASSET_MAX_REPAIR_ROUNDS": "max_repair_rounds",
    "PIXEL_ASSET_TIMEOUT": "timeout_seconds",
    "PIXEL_ASSET_CACHE": "cache_enabled",
    "PIXEL_ASSET_LOG_LEVEL": "log_level",
    "PIXEL_ASSET_BASE_URL": "base_url",
}

_INT_FIELDS = {
    "max_concurrency", "max_retries", "max_repair_rounds", "timeout_seconds",
    "requests_per_minute",
}
_BOOL_FIELDS = {"cache_enabled"}


class Config(BaseModel):
    """运行时配置。**不含 API Key** —— Key 单独走 :meth:`require_api_key`。"""

    model_config = ConfigDict(extra="forbid")

    provider: str = "openai"
    model: str = "gpt-image-2"
    fallback_model: str = "gpt-image-1.5"
    """透明背景降级路径使用的模型（ADR-004 第 4 档）。"""

    base_url: str | None = None

    output_dir: Path = Path("outputs")
    cache_dir: Path = Path(".pixel-asset-cache")

    max_concurrency: int = Field(default=DEFAULT_MAX_CONCURRENCY, ge=1, le=16)
    requests_per_minute: int | None = Field(default=None, ge=1, le=1000)
    """请求频率上限。None 表示只受 ``max_concurrency`` 约束。"""

    max_retries: int = Field(default=DEFAULT_MAX_RETRIES, ge=0, le=10)
    max_repair_rounds: int = Field(default=DEFAULT_MAX_REPAIR_ROUNDS, ge=0, le=5)
    timeout_seconds: int = Field(default=DEFAULT_TIMEOUT_SECONDS, ge=10, le=1800)

    cache_enabled: bool = True
    log_level: str = "INFO"

    #: 配置来源轨迹，供 doctor 展示"这个值是从哪来的"。
    sources: list[str] = Field(default_factory=list)

    # -- API Key ----------------------------------------------------------

    @staticmethod
    def api_key() -> SecretStr | None:
        for var in API_KEY_ENV_VARS:
            value = os.environ.get(var, "").strip()
            if value:
                return SecretStr(value)
        return None

    @staticmethod
    def api_key_env_var() -> str | None:
        """返回实际提供了 Key 的环境变量名（只回名字，不回值）。"""
        for var in API_KEY_ENV_VARS:
            if os.environ.get(var, "").strip():
                return var
        return None

    def require_api_key(self) -> SecretStr:
        key = self.api_key()
        if key is None:
            raise MissingApiKeyError(
                "未检测到 API Key。请设置环境变量 "
                + " 或 ".join(API_KEY_ENV_VARS)
                + "。绝不要把 Key 写进 request 文件或配置文件。"
            )
        return key

    def asset_dir(self, asset_id: str) -> Path:
        return self.output_dir / asset_id


def _coerce(field: str, raw: str) -> Any:
    if field in _INT_FIELDS:
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(f"{field} 需要整数，收到 {raw!r}") from exc
    if field in _BOOL_FIELDS:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return raw


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}：配置文件 YAML 解析失败 —— {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}：配置文件顶层必须是映射（字典）")

    for var in API_KEY_ENV_VARS:
        if var.lower() in data or var in data:
            raise ConfigError(
                f"{path}：配置文件中不允许出现 API Key。请改用环境变量 {var}。"
            )
    if "api_key" in data:
        raise ConfigError(
            f"{path}：配置文件中不允许出现 api_key 字段。请改用环境变量 "
            + " 或 ".join(API_KEY_ENV_VARS)
            + "。"
        )
    return data


def find_project_config(start: Path | None = None) -> Path | None:
    """从 ``start`` 逐级向上找 ``pixel-asset.yaml``。"""
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        path = candidate / PROJECT_CONFIG_NAME
        if path.exists():
            return path
    return None


def load_config(
    *,
    project_config: Path | None = None,
    user_config: Path | None = None,
    overrides: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> Config:
    """按优先级合并配置。"""
    env = dict(os.environ if env is None else env)
    merged: dict[str, Any] = {}
    sources: list[str] = ["内置默认值"]

    user_path = user_config if user_config is not None else USER_CONFIG_PATH
    user_data = _read_yaml(user_path)
    if user_data:
        merged.update(user_data)
        sources.append(f"用户级配置 {user_path}")

    project_path = project_config if project_config is not None else find_project_config()
    if project_path is not None:
        project_data = _read_yaml(project_path)
        if project_data:
            merged.update(project_data)
            sources.append(f"项目级配置 {project_path}")

    env_data = {
        field: _coerce(field, env[var]) for var, field in _ENV_FIELDS.items() if env.get(var)
    }
    if env_data:
        merged.update(env_data)
        sources.append("环境变量")

    if overrides:
        merged.update({k: v for k, v in overrides.items() if v is not None})
        sources.append("命令行参数")

    unknown = set(merged) - set(Config.model_fields) - {"sources"}
    if unknown:
        raise ConfigError(
            f"配置中存在未知字段：{', '.join(sorted(unknown))}。"
            f"可用字段：{', '.join(sorted(set(Config.model_fields) - {'sources'}))}"
        )

    merged["sources"] = sources
    return Config.model_validate(merged)


DEFAULT_CONFIG_TEMPLATE = f"""\
# Pixel Asset Forge 项目配置
#
# 优先级：命令行覆盖 > 环境变量 > 本文件 > 用户级配置（{USER_CONFIG_PATH}）> 内置默认值
#
# ⚠️ 绝不要在这里写 API Key。Key 只从环境变量读取：
#      {" 或 ".join(API_KEY_ENV_VARS)}

provider: openai
model: gpt-image-2

# 透明背景降级路径使用的模型（ADR-004 第 4 档，兜底而非主路径）
fallback_model: gpt-image-1.5

output_dir: outputs

# 并发上限。Sprint 4 单个角色就有 21 次调用，别开太大以免撞速率限制。
max_concurrency: {DEFAULT_MAX_CONCURRENCY}

# 请求频率上限（次/分钟）。留空表示只受 max_concurrency 约束。
# requests_per_minute: 20

# 仅对 429 / 5xx 等瞬态错误退避重试；参数错误与 moderation 不重试。
max_retries: {DEFAULT_MAX_RETRIES}

# 单个任务的最大修复轮次，超过即判定 failed。
max_repair_rounds: {DEFAULT_MAX_REPAIR_ROUNDS}

timeout_seconds: {DEFAULT_TIMEOUT_SECONDS}

# prompt hash + 输入图 hash 缓存。开启后重跑失败任务不会重复计费。
cache_enabled: true

log_level: INFO
"""
