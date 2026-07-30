"""密钥脱敏。

"API Key 不出现在任何日志或错误信息中"是 Sprint 2 的退出门槛，
但脱敏函数本身现在就得可靠 —— 它是最后一道防线。
"""

from __future__ import annotations

import logging

import pytest

from pixel_asset_forge.errors import REDACTED, PixelAssetError, redact
from pixel_asset_forge.logging_utils import RedactingJsonFormatter, RedactingTextFormatter


@pytest.mark.parametrize(
    "text",
    [
        "调用失败，key=sk-proj-abcdefghijklmnop",
        "Authorization: Bearer sk-abcdefghijklmnop",
        "请求头 api_key: abcdefghijklmnop",
        "API-KEY=abcdefghijklmnop 无效",
    ],
)
def test_secrets_are_scrubbed(text: str) -> None:
    out = redact(text)
    assert "abcdefghijklmnop" not in out
    assert REDACTED in out


def test_ordinary_text_is_untouched() -> None:
    text = "walk_down 帧数不足：期望 8，实际 6"
    assert redact(text) == text


def test_error_messages_are_scrubbed_at_construction() -> None:
    exc = PixelAssetError("provider rejected sk-proj-abcdefghijklmnop")
    assert "abcdefghijklmnop" not in str(exc)
    assert "abcdefghijklmnop" not in exc.message


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, __file__, 1, message, None, None)


def test_text_formatter_scrubs() -> None:
    out = RedactingTextFormatter("%(message)s").format(_record("key sk-abcdefghijklmnop"))
    assert "abcdefghijklmnop" not in out


def test_json_formatter_scrubs_message_and_extras() -> None:
    record = _record("normal message")
    record.detail = "Bearer sk-abcdefghijklmnop"  # type: ignore[attr-defined]
    out = RedactingJsonFormatter().format(record)
    assert "abcdefghijklmnop" not in out
    assert '"detail"' in out
