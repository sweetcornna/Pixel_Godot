"""真实 API 测试。**默认跳过**，需显式 ``RUN_LIVE_IMAGE_TESTS=1`` 开启。

这些用例会真的花钱，因此：绝不进 CI 默认流程、每个用例只发一次调用、
用最小的合规尺寸。断言只覆盖 Provider 层的契约（能拿到图、错误分类正确），
**不断言产出质量** —— 那是验证引擎的事（ADR-002 的分层边界）。
"""

from __future__ import annotations

import os

import pytest

from pixel_asset_forge.config import Config
from pixel_asset_forge.errors import InvalidRequestError, ProviderAuthError
from pixel_asset_forge.planning import seed_layout
from pixel_asset_forge.providers import build_backend, get_provider
from pixel_asset_forge.providers.openai_image import OpenAIImageProvider

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_IMAGE_TESTS") != "1",
        reason="需要 RUN_LIVE_IMAGE_TESTS=1 且会产生真实计费",
    ),
]

SEED_PROMPT = (
    "Pixel art character sprite, single small round green slime creature, "
    "front view, crisp pixel art, single dark outline, no anti-aliasing. "
    "Full body visible, centered, at least 10% empty margin on every side. "
    "Background: completely flat solid #FF00FF magenta, no gradient, no shadow, "
    "no scenery, no text."
)


@pytest.fixture
def config() -> Config:
    return Config(
        provider="openai",
        model=os.environ.get("PIXEL_ASSET_MODEL", "gpt-image-2"),
        base_url=os.environ.get("PIXEL_ASSET_BASE_URL"),
        cache_enabled=False,
    )


def test_probe_does_not_cost_anything(config: Config) -> None:
    """doctor --probe 必须能验证鉴权与连通性，且不产生图像调用。"""
    info = build_backend(config).probe()
    assert info["reachable"] is True
    assert info["provider"] == "openai"


def test_generate_returns_a_real_png(config: Config, tmp_path) -> None:
    provider = build_backend(config)
    result = provider.generate(SEED_PROMPT, size=seed_layout().size)

    assert result.image[:8] == b"\x89PNG\r\n\x1a\n"
    assert result.actual_size[0] > 0 and result.actual_size[1] > 0
    assert result.prompt_hash

    # Sprint 0 / A-1：端点可能静默改尺寸。这里不断言"尺寸一致"，
    # 只断言我们**如实记录**了实际尺寸 —— 记录正确，下游按比例切帧才成立。
    if result.size_snapped:
        pytest.xfail(
            f"端点把 {result.requested_size} 改成了 {result.actual_size}"
            f"（长短边比偏差 {result.aspect_drift:.1%}）—— 已知行为，见 Sprint 0 报告 A-1"
        )


def test_illegal_size_is_rejected_locally_not_by_the_api(config: Config) -> None:
    """尺寸自检必须在本地拦下，不要浪费一次往返。"""
    provider = build_backend(config)
    with pytest.raises(InvalidRequestError):
        provider.generate(SEED_PROMPT, size=(1000, 1024))


def test_bad_key_is_an_auth_error() -> None:
    provider = OpenAIImageProvider(
        "gpt-image-2",
        api_key="sk-obviously-invalid-key-for-testing",
        base_url=os.environ.get("PIXEL_ASSET_BASE_URL"),
    )
    with pytest.raises(ProviderAuthError):
        provider.probe()


def test_cache_prevents_a_second_charge(config: Config, tmp_path) -> None:
    """SKILL.md 承诺"重跑失败任务是安全的"——这条必须在真实调用上也成立。"""
    config = config.model_copy(
        update={"cache_enabled": True, "cache_dir": tmp_path / "cache"}
    )
    provider = get_provider(config)

    first = provider.generate(SEED_PROMPT, size=seed_layout().size)
    second = provider.generate(SEED_PROMPT, size=seed_layout().size)

    assert first.cached is False
    assert second.cached is True
    assert second.image == first.image
