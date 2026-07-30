"""生成后端。上层只通过 :class:`ImageProvider` 接口调用（ADR-002）。"""

from __future__ import annotations

from ..config import Config
from ..errors import ConfigError
from ..storage.cache import GenerationCache
from .base import (
    GenerationResult,
    ImageProvider,
    ReferenceImage,
    RetryPolicy,
    measure_png,
    with_retry,
)
from .mock import MockImageProvider
from .throttle import Throttle
from .wrappers import CachingProvider, ThrottledProvider, bypass_cache

KNOWN_PROVIDERS = ("openai", "mock")


def build_backend(config: Config) -> ImageProvider:
    """只构造裸后端，不套缓存与限流。"""
    policy = RetryPolicy(max_retries=config.max_retries)

    if config.provider == "mock":
        return MockImageProvider(config.model, retry_policy=policy)

    if config.provider == "openai":
        from .openai_image import OpenAIImageProvider

        return OpenAIImageProvider(
            config.model,
            api_key=config.require_api_key().get_secret_value(),
            base_url=config.base_url,
            timeout=float(config.timeout_seconds),
            retry_policy=policy,
        )

    raise ConfigError(
        f"未知 provider：{config.provider}。可选：{', '.join(KNOWN_PROVIDERS)}"
    )


def get_provider(config: Config, *, cache: GenerationCache | None = None) -> ImageProvider:
    """按配置构造完整的 Provider 栈。

    包装顺序是 ``Caching(Throttled(backend))`` —— 缓存在外层，
    命中时不占用限流名额（本来就没有网络调用）。
    """
    provider: ImageProvider = build_backend(config)

    provider = ThrottledProvider(
        provider,
        Throttle.from_rpm(config.max_concurrency, config.requests_per_minute),
    )

    if config.cache_enabled:
        provider = CachingProvider(
            provider, cache or GenerationCache(config.cache_dir, enabled=True)
        )

    return provider


__all__ = [
    "KNOWN_PROVIDERS",
    "CachingProvider",
    "GenerationResult",
    "ImageProvider",
    "MockImageProvider",
    "ReferenceImage",
    "RetryPolicy",
    "Throttle",
    "ThrottledProvider",
    "build_backend",
    "bypass_cache",
    "get_provider",
    "measure_png",
    "with_retry",
]
