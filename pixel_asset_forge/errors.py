"""统一的内部错误类型。

两条铁律：

1. **Provider SDK 异常绝不外泄** —— 全部转换为这里的类型（ADR-002）。
   上层代码不得 ``import openai`` 只为捕获异常。
2. **错误信息里绝不出现 API Key** —— 所有面向用户的文本先过 :func:`redact`。
"""

from __future__ import annotations

import re
from typing import Any

# sk-... / sk-proj-... 及常见 Bearer 头。宁可多打码也不要漏。
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s,;\"']{8,}"),
)

REDACTED = "***REDACTED***"


def redact(text: str) -> str:
    """抹掉文本中疑似密钥的片段。所有错误信息与日志都必须经过它。"""
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(
            lambda m: (m.group(1) + REDACTED) if m.re.groups else REDACTED, out
        )
    return out


class PixelAssetError(Exception):
    """所有项目内部错误的基类。"""

    #: 稳定的机器可读标识，供 CLI 退出码与日志分类使用。
    code: str = "pixel_asset_error"

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(redact(message))
        self.message = redact(message)
        self.context = context

    def __str__(self) -> str:  # pragma: no cover - 平凡实现
        return self.message


# ---------------------------------------------------------------------------
# 配置与环境
# ---------------------------------------------------------------------------


class ConfigError(PixelAssetError):
    """配置缺失或非法。用 ``pixel-asset doctor`` 排查。"""

    code = "config_error"


class MissingApiKeyError(ConfigError):
    """未设置 API Key。永远只提示环境变量名，不回显任何值。"""

    code = "missing_api_key"


# ---------------------------------------------------------------------------
# 请求解析
# ---------------------------------------------------------------------------


class RequestValidationError(PixelAssetError):
    """Asset Request 不合法。

    ``errors`` 中每项形如 ``{"path": "style.target_size.0", "message": "..."}``。
    Sprint 1 退出门槛要求错误能准确指出字段路径，所以路径必须逐字段给出，
    不能只丢一句"schema 校验失败"。
    """

    code = "request_validation_error"

    def __init__(self, message: str, errors: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []

    def format_errors(self) -> str:
        if not self.errors:
            return self.message
        lines = [self.message]
        lines.extend(f"  {e['path'] or '<root>'}: {e['message']}" for e in self.errors)
        return "\n".join(lines)


class SchemaVersionError(PixelAssetError):
    """Manifest 的 MAJOR 版本高于读取器所支持的版本（PLAN §5.4）。"""

    code = "schema_version_error"


# ---------------------------------------------------------------------------
# 规划
# ---------------------------------------------------------------------------


class PlanError(PixelAssetError):
    """请求无法编译为合法任务 DAG。"""

    code = "plan_error"


class GridLayoutError(PlanError):
    """帧数无法映射到合规的物理网格（PLAN §2.3）。"""

    code = "grid_layout_error"


class StateTransitionError(PixelAssetError):
    """非法的 Job 状态转移（PLAN §5.3）。"""

    code = "state_transition_error"

    def __init__(self, job_id: str, current: str, event: str) -> None:
        super().__init__(f"任务 {job_id} 处于 {current}，不接受事件 {event}")
        self.job_id = job_id
        self.current = current
        self.event = event


# ---------------------------------------------------------------------------
# Provider（ADR-002 的错误分类表）
# ---------------------------------------------------------------------------


class ProviderError(PixelAssetError):
    """生成层错误的基类。

    注意边界：Provider 只回答"调用是否成功"，**不回答"产出是否合格"**。
    帧数错误、构图越界不是 ProviderError —— 那些走 validate → repair。
    """

    code = "provider_error"

    #: 是否属于瞬态错误。只有 True 才允许退避重试。
    transient: bool = False

    def __init__(self, message: str, *, request_id: str | None = None, **context: Any) -> None:
        super().__init__(message, **context)
        self.request_id = request_id


class TransientProviderError(ProviderError):
    """429 / 5xx / 网络抖动 —— Provider 内部指数退避重试。"""

    code = "provider_transient_error"
    transient = True


class RetryLimitExceededError(ProviderError):
    """退避重试已达上限，转为终态失败。"""

    code = "provider_retry_limit_exceeded"


class InvalidRequestError(ProviderError):
    """参数错误 —— **不重试**。反复重试同一个非法请求只会浪费配额。"""

    code = "provider_invalid_request"


class ModerationBlockedError(ProviderError):
    """内容被审核拦截 —— 不重试，需改写描述后重新提交。"""

    code = "provider_moderation_blocked"


class ProviderAuthError(ProviderError):
    """认证失败（Key 无效 / 无权限）。不重试。"""

    code = "provider_auth_error"


class InvalidImageResponseError(ProviderError):
    """Provider 报告成功，但响应体不是可解析的图像。

    这不是网络抖动或 5xx：远端调用已经给出了成功响应，盲目自动重试可能重复
    计费。把它持久化为明确失败，交给显式的 ``--retry-failed`` 恢复。
    """

    code = "provider_invalid_image"


# ---------------------------------------------------------------------------
# 处理、验证、修复
# ---------------------------------------------------------------------------


class ProcessingError(PixelAssetError):
    """确定性处理链失败。这类错误应当可复现。"""

    code = "processing_error"


class ValidationFailedError(PixelAssetError):
    """存在 fatal / high 级别的验证失败项。绝不允许被吞掉。"""

    code = "validation_failed"


class PauseRequested(PixelAssetError):
    """批次已请求协作暂停；当前资产已停在可恢复阶段边界。"""

    code = "pause_requested"


class RepairLimitExceededError(PixelAssetError):
    """超过 ``max_repair_rounds``。"""

    code = "repair_limit_exceeded"


class ExportError(PixelAssetError):
    """导出失败。"""

    code = "export_error"


class NotImplementedYetError(PixelAssetError):
    """功能已在 PLAN 中排期但尚未实现。

    比裸 ``NotImplementedError`` 好在：CLI 能给出"这属于哪个 Sprint"的明确出口，
    而不是一个看起来像 bug 的堆栈。
    """

    code = "not_implemented_yet"

    def __init__(self, feature: str, sprint: str) -> None:
        super().__init__(f"{feature} 尚未实现（排期：{sprint}）")
        self.feature = feature
        self.sprint = sprint
