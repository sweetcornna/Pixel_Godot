"""OpenAI Provider 的异常翻译（ADR-002）。

分类只有一个目的：**决定要不要重试**。
分错的代价是具体的 —— 把参数错误判成瞬态，就会对着一个永远不会成功的请求
退避重试到上限，白烧配额；把 429 判成永久，则会让本可自愈的调用直接失败。

因此这里逐条钉死映射关系。SDK 异常绝不允许穿透到上层。
"""

from __future__ import annotations

import httpx
import openai
import pytest

from pixel_asset_forge.errors import (
    InvalidRequestError,
    ModerationBlockedError,
    ProviderAuthError,
    ProviderError,
    TransientProviderError,
)
from pixel_asset_forge.providers.openai_image import OpenAIImageProvider


@pytest.fixture
def provider() -> OpenAIImageProvider:
    return OpenAIImageProvider("gpt-image-2", api_key="sk-test-not-a-real-key")


def _response(status: int, body: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        request=httpx.Request("POST", "https://example.invalid/v1/images/generations"),
        json=body or {"error": {"message": "boom"}},
    )


def _status_error(cls, status: int, message: str = "boom"):
    return cls(message, response=_response(status), body={"message": message})


# -- 瞬态：必须重试 --------------------------------------------------------


def test_rate_limit_is_transient(provider: OpenAIImageProvider) -> None:
    err = provider._translate(_status_error(openai.RateLimitError, 429))
    assert isinstance(err, TransientProviderError)
    assert err.transient is True


def test_server_errors_are_transient(provider: OpenAIImageProvider) -> None:
    for status in (500, 502, 503, 529):
        err = provider._translate(_status_error(openai.InternalServerError, status))
        assert isinstance(err, TransientProviderError), status


def test_timeout_is_transient(provider: OpenAIImageProvider) -> None:
    exc = openai.APITimeoutError(request=httpx.Request("POST", "https://example.invalid"))
    assert isinstance(provider._translate(exc), TransientProviderError)


def test_connection_error_is_transient(provider: OpenAIImageProvider) -> None:
    exc = openai.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))
    assert isinstance(provider._translate(exc), TransientProviderError)


# -- 永久：绝不重试 --------------------------------------------------------


def test_bad_request_is_permanent(provider: OpenAIImageProvider) -> None:
    err = provider._translate(_status_error(openai.BadRequestError, 400, "invalid size"))
    assert isinstance(err, InvalidRequestError)
    assert err.transient is False
    assert "不要重试原请求" in err.message


def test_moderation_is_recognised_and_actionable(provider: OpenAIImageProvider) -> None:
    exc = _status_error(
        openai.BadRequestError, 400,
        "Your request was rejected as a result of our safety system.",
    )
    err = provider._translate(exc)
    assert isinstance(err, ModerationBlockedError)
    assert err.transient is False
    # 报错必须告诉用户下一步做什么，而不只是"被拒了"
    assert "改写" in err.message


@pytest.mark.parametrize(
    "message",
    [
        "blocked by our content_policy",
        "flagged by the moderation system",
        "This prompt is not allowed by our safety policies",
    ],
)
def test_moderation_markers(provider: OpenAIImageProvider, message: str) -> None:
    err = provider._translate(_status_error(openai.BadRequestError, 400, message))
    assert isinstance(err, ModerationBlockedError)


def test_ordinary_400_is_not_mistaken_for_moderation(provider: OpenAIImageProvider) -> None:
    """宁可漏判为普通参数错误（同样不重试），也不要误判。"""
    err = provider._translate(_status_error(openai.BadRequestError, 400, "size is invalid"))
    assert isinstance(err, InvalidRequestError)
    assert not isinstance(err, ModerationBlockedError)


def test_auth_error_points_at_doctor(provider: OpenAIImageProvider) -> None:
    err = provider._translate(_status_error(openai.AuthenticationError, 401))
    assert isinstance(err, ProviderAuthError)
    assert err.transient is False
    assert "doctor" in err.message


def test_permission_denied_is_auth(provider: OpenAIImageProvider) -> None:
    err = provider._translate(_status_error(openai.PermissionDeniedError, 403))
    assert isinstance(err, ProviderAuthError)


def test_other_4xx_is_permanent(provider: OpenAIImageProvider) -> None:
    err = provider._translate(_status_error(openai.NotFoundError, 404))
    assert isinstance(err, InvalidRequestError)
    assert err.transient is False


# -- 兜底 -----------------------------------------------------------------


def test_unknown_exception_still_becomes_an_internal_error(
    provider: OpenAIImageProvider,
) -> None:
    """SDK 异常绝不允许穿透 —— 上层不该 import openai 只为了 except。"""
    err = provider._translate(RuntimeError("something unexpected"))
    assert isinstance(err, ProviderError)
    assert err.transient is False


def test_translation_redacts_secrets(provider: OpenAIImageProvider) -> None:
    """"API Key 不出现在任何日志或错误信息中"是 Sprint 2 的退出门槛。"""
    exc = _status_error(
        openai.BadRequestError, 400,
        "request failed with Authorization: Bearer sk-proj-abcdefghijklmnop",
    )
    err = provider._translate(exc)
    assert "abcdefghijklmnop" not in err.message


def test_client_does_not_double_retry(provider: OpenAIImageProvider) -> None:
    """SDK 自己的重试必须关掉 —— 两层退避叠加会让等待时间不可预测。"""
    assert provider._client.max_retries == 0


# -- 响应解析 --------------------------------------------------------------


class _Item:
    def __init__(self, b64_json=None, url=None):
        self.b64_json = b64_json
        self.url = url


class _Payload:
    def __init__(self, data):
        self.data = data


def test_extract_decodes_base64(provider: OpenAIImageProvider) -> None:
    import base64

    payload = _Payload([_Item(b64_json=base64.b64encode(b"png-bytes").decode())])
    assert provider._extract_image(payload) == b"png-bytes"


def test_url_only_response_is_rejected_with_a_reason(provider: OpenAIImageProvider) -> None:
    """URL 有时效，落盘前就可能失效 —— 会破坏"原图永不覆盖"的前提。"""
    with pytest.raises(ProviderError) as exc:
        provider._extract_image(_Payload([_Item(url="https://example.invalid/img.png")]))
    assert "base64" in exc.value.message


def test_empty_response_is_an_error(provider: OpenAIImageProvider) -> None:
    with pytest.raises(ProviderError):
        provider._extract_image(_Payload([]))
