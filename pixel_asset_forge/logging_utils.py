"""结构化日志。

每条日志都过一遍 :func:`~pixel_asset_forge.errors.redact` —— 不是"记得的时候过一下"，
而是在 Formatter 里强制过。人是会忘的，Formatter 不会。
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from .errors import redact

LOGGER_NAME = "pixel_asset"

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "asctime",
    "message",
    "taskName",
}


class RedactingJsonFormatter(logging.Formatter):
    """JSON Lines 格式，且强制脱敏。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = redact(value) if isinstance(value, str) else value
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


class RedactingTextFormatter(logging.Formatter):
    """人读格式，同样强制脱敏。"""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(level: str = "INFO", *, json_output: bool = False) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level.upper())
    logger.handlers.clear()
    logger.propagate = False

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        RedactingJsonFormatter()
        if json_output
        else RedactingTextFormatter("%(levelname)-7s %(message)s")
    )
    logger.addHandler(handler)
    return logger


def get_logger(suffix: str | None = None) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{suffix}" if suffix else LOGGER_NAME)
