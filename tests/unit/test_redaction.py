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


# -- 按实际值脱敏（config.redact_secret_values）---------------------------
#
# 与本模块上面那套 errors.redact 分工不同：那边按**模式**猜疑似密钥，覆盖面广但会
# 漏掉长得不像密钥的值；这边拿到的是环境里的确切值，可以精确替换。两者互补。


def test_secret_values_are_replaced_by_actual_value() -> None:
    from pixel_asset_forge.config import redact_secret_values

    text = "first=sk-first second=sk-second"
    assert redact_secret_values(text, ["sk-first", "sk-second"]) == (
        "first=[REDACTED] second=[REDACTED]"
    )


def test_the_longest_secret_is_replaced_first() -> None:
    """短值可能是长值的子串。先换短的会把长值切碎、在输出里留下残片。

    这是最容易在重构中丢掉的一行（`key=len, reverse=True`），而丢了它的后果是
    **Key 的一部分仍然出现在日志里** —— 所以单独钉住。
    """
    from pixel_asset_forge.config import redact_secret_values

    # "sk-abc" 是 "sk-abcdef123" 的前缀：先换短的会留下 "def123"。
    out = redact_secret_values("token=sk-abcdef123", ["sk-abc", "sk-abcdef123"])
    assert out == "token=[REDACTED]"
    assert "def123" not in out


def test_every_gate_shares_one_redaction_implementation() -> None:
    """live-gate 与 calibration 原本各写了一份逐行相同的实现。

    密钥脱敏不该有第二个副本 —— 两边漂移的后果是某一侧漏掉一种形态、把 Key 写进
    日志。这条断言的是"它们真的是同一个函数"，而不是"碰巧行为一致"。
    """
    import importlib.util
    import sys
    from pathlib import Path

    from pixel_asset_forge.config import redact_secret_values

    root = Path(__file__).resolve().parents[2] / "tools"
    for index, script in enumerate(
        ("calibration/run_calibration.py", "live-gate/run_live_gate.py")
    ):
        name = f"redaction_parity_gate_{index}"
        spec = importlib.util.spec_from_file_location(name, root / script)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # 先注册再 exec：脚本顶层的 dataclass 处理要按模块名回查，
        # 少了这一步会崩在 dataclasses 内部（实测踩过）。
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
            assert module.redact_text is redact_secret_values, script
        finally:
            sys.modules.pop(name, None)
