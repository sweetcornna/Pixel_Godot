"""Provider 装饰器：缓存与限流的组合行为。

关键的一条：**缓存命中不占用限流名额**。
命中时本来就没有网络调用，占名额只会白白拖慢整批任务。
包装顺序 ``Caching(Throttled(backend))`` 就是为了保证这一点。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pixel_asset_forge.config import Config
from pixel_asset_forge.errors import ModerationBlockedError, RetryLimitExceededError
from pixel_asset_forge.planning import grid_for_frames
from pixel_asset_forge.providers import (
    CachingProvider,
    MockImageProvider,
    ReferenceImage,
    RetryPolicy,
    Throttle,
    ThrottledProvider,
    build_backend,
    get_provider,
)
from pixel_asset_forge.storage import GenerationCache

PROMPT = "walk cycle, 2x2 grid, exactly 4 distinct poses, solid #FF00FF background"


@pytest.fixture
def size() -> tuple[int, int]:
    return grid_for_frames(4).size


@pytest.fixture
def backend() -> MockImageProvider:
    return MockImageProvider("mock-image")


def test_caching_provider_reports_the_backend_name(
    backend: MockImageProvider, tmp_path: Path, size
) -> None:
    """包装是实现细节，不该泄漏到 Manifest 的 provider.name 里。"""
    provider = CachingProvider(backend, GenerationCache(tmp_path / "c"))
    result = provider.generate(PROMPT, size=size)
    assert result.provider == "mock"


def test_second_identical_call_hits_the_cache(
    backend: MockImageProvider, tmp_path: Path, size
) -> None:
    provider = CachingProvider(backend, GenerationCache(tmp_path / "c"))
    first = provider.generate(PROMPT, size=size)
    second = provider.generate(PROMPT, size=size)

    assert len(backend.calls) == 1
    assert first.cached is False and second.cached is True
    assert second.image == first.image
    assert (provider.hits, provider.misses) == (1, 1)


def test_cached_result_still_carries_the_actual_size(
    backend: MockImageProvider, tmp_path: Path, size
) -> None:
    """缓存回放的结果也要能驱动按比例切帧 —— actual_size 不能丢。"""
    provider = CachingProvider(backend, GenerationCache(tmp_path / "c"))
    provider.generate(PROMPT, size=size)
    cached = provider.generate(PROMPT, size=size)
    assert cached.actual_size == size
    assert cached.requested_size == size


def test_different_prompt_misses(backend: MockImageProvider, tmp_path: Path, size) -> None:
    provider = CachingProvider(backend, GenerationCache(tmp_path / "c"))
    provider.generate(PROMPT, size=size)
    provider.generate(PROMPT + " extra", size=size)
    assert len(backend.calls) == 2


def test_different_reference_misses(backend: MockImageProvider, tmp_path: Path, size) -> None:
    """换了 seed 就该 miss，否则不同角色会共用同一张动作网格。"""
    provider = CachingProvider(backend, GenerationCache(tmp_path / "c"))
    for seed in (b"seed-a", b"seed-b"):
        provider.edit(PROMPT, base_image=b"canvas", size=size,
                      references=[ReferenceImage("seed", seed)])
    assert len(backend.calls) == 2


def test_disabled_cache_always_calls_the_backend(
    backend: MockImageProvider, tmp_path: Path, size
) -> None:
    provider = CachingProvider(backend, GenerationCache(tmp_path / "c", enabled=False))
    provider.generate(PROMPT, size=size)
    provider.generate(PROMPT, size=size)
    assert len(backend.calls) == 2


def test_failures_are_not_cached(tmp_path: Path, size, no_sleep) -> None:
    """失败的调用不该污染缓存 —— 否则重跑会永远拿到那次失败。"""
    backend = MockImageProvider(
        "mock-image", fail_times=99, retry_policy=RetryPolicy(max_retries=1, base_delay=0.01)
    )
    provider = CachingProvider(backend, GenerationCache(tmp_path / "c"))
    with pytest.raises(RetryLimitExceededError):
        provider.generate(PROMPT, size=size, sleep=no_sleep)

    backend.fail_times = 0
    result = provider.generate(PROMPT, size=size, sleep=no_sleep)
    assert result.cached is False


def test_moderation_block_is_not_cached(tmp_path: Path, size, no_sleep) -> None:
    backend = MockImageProvider("mock-image")
    provider = CachingProvider(backend, GenerationCache(tmp_path / "c"))
    for _ in range(2):
        with pytest.raises(ModerationBlockedError):
            provider.generate("a gore-filled battlefield", size=size, sleep=no_sleep)


# -- 限流 -----------------------------------------------------------------


def test_throttle_counts_every_retry_as_a_call(tmp_path: Path, size, no_sleep) -> None:
    """重试同样是真实请求。不算进限流的话，一批任务集体撞 429 时会同时重试。"""
    backend = MockImageProvider(
        "mock-image", fail_times=2, retry_policy=RetryPolicy(max_retries=3, base_delay=0.01)
    )
    throttle = Throttle(max_concurrency=2)
    provider = ThrottledProvider(backend, throttle)

    provider.generate(PROMPT, size=size, sleep=no_sleep)
    assert throttle.stats["total"] == 3  # 1 次首发 + 2 次重试


def test_cache_hit_does_not_consume_a_throttle_slot(
    backend: MockImageProvider, tmp_path: Path, size
) -> None:
    """包装顺序的意义所在：命中缓存时根本没有网络调用，不该排队。"""
    throttle = Throttle(max_concurrency=1)
    provider = CachingProvider(ThrottledProvider(backend, throttle),
                               GenerationCache(tmp_path / "c"))

    provider.generate(PROMPT, size=size)
    assert throttle.stats["total"] == 1

    provider.generate(PROMPT, size=size)
    assert throttle.stats["total"] == 1, "缓存命中却占用了限流名额"


# -- 工厂 -----------------------------------------------------------------


def test_factory_builds_the_full_stack(tmp_path: Path) -> None:
    config = Config(provider="mock", cache_dir=tmp_path / "c", max_concurrency=2)
    provider = get_provider(config)
    assert isinstance(provider, CachingProvider)
    assert isinstance(provider.inner, ThrottledProvider)
    assert isinstance(provider.inner.inner, MockImageProvider)
    assert provider.inner.throttle.max_concurrency == 2


def test_factory_reuses_an_injected_throttle(tmp_path: Path) -> None:
    throttle = Throttle(max_concurrency=1)
    provider = get_provider(Config(provider="mock"), throttle=throttle)
    assert isinstance(provider, CachingProvider)
    assert isinstance(provider.inner, ThrottledProvider)
    assert provider.inner.throttle is throttle


def test_factory_omits_cache_when_disabled(tmp_path: Path) -> None:
    config = Config(provider="mock", cache_enabled=False)
    provider = get_provider(config)
    assert isinstance(provider, ThrottledProvider)


def test_build_backend_returns_the_bare_provider() -> None:
    assert isinstance(build_backend(Config(provider="mock")), MockImageProvider)


def test_unknown_provider_lists_the_valid_options() -> None:
    from pixel_asset_forge.errors import ConfigError

    with pytest.raises(ConfigError) as exc:
        build_backend(Config(provider="midjourney"))
    assert "mock" in exc.value.message


def test_openai_backend_requires_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from pixel_asset_forge.errors import MissingApiKeyError

    monkeypatch.delenv("PIXEL_ASSET_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(MissingApiKeyError):
        build_backend(Config(provider="openai"))
