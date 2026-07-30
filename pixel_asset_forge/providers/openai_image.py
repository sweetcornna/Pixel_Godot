"""OpenAI 图像 Provider（ADR-002）。

这个文件的**唯一职责**是把 OpenAI SDK 的世界翻译成项目内部的世界：

- SDK 异常 → 内部错误类型。上层永远不该 ``import openai`` 只为了 ``except``。
- 分类的目的只有一个：**决定要不要重试**。瞬态错误重试，永久错误立刻停 ——
  反复重试一个非法请求只会烧配额，而且永远不会成功。

边界仍然是 ADR-002 那条：Provider 只回答"调用是否成功"，
**不回答"产出是否合格"**。帧数不对、姿势跨格、身份漂移都不是这里的事。
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from io import BytesIO
from typing import Any

from ..errors import (
    ConfigError,
    InvalidRequestError,
    ModerationBlockedError,
    ProviderAuthError,
    ProviderError,
    TransientProviderError,
    redact,
)
from ..logging_utils import get_logger
from .base import ImageProvider, ReferenceImage, RetryPolicy

logger = get_logger("provider.openai")

#: 判定 400 是不是内容审核拦截。SDK 不给稳定的机器码，只能看文本 ——
#: 因此宁可漏判（当成普通参数错误、不重试），也不要误判成可重试。
_MODERATION_MARKERS = (
    "moderation",
    "safety system",
    "content_policy",
    "content policy",
    "rejected as a result of our safety",
    "not allowed by our safety",
)


def _import_openai() -> Any:
    try:
        import openai
    except ImportError as exc:  # pragma: no cover - 取决于安装方式
        raise ConfigError(
            "未安装 openai SDK。请执行 `uv sync --all-extras`，"
            "或 `pip install 'pixel-asset-forge[openai]'`。"
        ) from exc
    return openai


class OpenAIImageProvider(ImageProvider):
    """`gpt-image-2` / `gpt-image-1.5` 的真实实现。"""

    name = "openai"

    def __init__(
        self,
        model: str = "gpt-image-2",
        *,
        api_key: str,
        base_url: str | None = None,
        timeout: float = 300.0,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        super().__init__(model, retry_policy=retry_policy)
        openai = _import_openai()
        self._openai = openai
        # max_retries=0：退避策略由本项目的 with_retry 统一负责（ADR-002）。
        # 让 SDK 也重试会导致两层退避叠加，实际等待时间不可预测。
        self._client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
        )

    # -- 异常翻译 ---------------------------------------------------------

    def _translate(self, exc: Exception) -> ProviderError:
        openai = self._openai
        request_id = getattr(exc, "request_id", None)
        message = redact(str(exc))

        # 顺序有讲究：先判具体子类，再判状态码。
        if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
            return ProviderAuthError(
                f"认证失败或无权限：{message}。用 `pixel-asset doctor` 检查环境变量中的 Key。",
                request_id=request_id,
            )

        if isinstance(exc, openai.RateLimitError):
            return TransientProviderError(f"触发速率限制：{message}", request_id=request_id)

        if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
            return TransientProviderError(f"网络或超时错误：{message}", request_id=request_id)

        if isinstance(exc, openai.BadRequestError):
            lowered = message.lower()
            if any(marker in lowered for marker in _MODERATION_MARKERS):
                return ModerationBlockedError(
                    f"内容被审核拦截：{message}。请改写角色描述，避免暴力/血腥表述后重新提交。",
                    request_id=request_id,
                )
            return InvalidRequestError(
                f"请求参数非法：{message}。修正 request 后再跑，不要重试原请求。",
                request_id=request_id,
            )

        if isinstance(exc, openai.APIStatusError):
            status = getattr(exc, "status_code", None)
            if status is not None and status >= 500:
                return TransientProviderError(
                    f"服务端错误 {status}：{message}", request_id=request_id
                )
            return InvalidRequestError(
                f"请求被拒绝（HTTP {status}）：{message}", request_id=request_id
            )

        if isinstance(exc, openai.APIError):
            return ProviderError(f"OpenAI SDK 错误：{message}", request_id=request_id)

        # 兜底：未知异常也必须变成内部类型，绝不让 SDK 异常穿透到上层。
        return ProviderError(f"未预期的错误（{type(exc).__name__}）：{message}")

    # -- 响应解析 ---------------------------------------------------------

    @staticmethod
    def _request_id(raw: Any) -> str | None:
        headers = getattr(raw, "headers", None)
        if headers is None:
            return None
        for key in ("x-request-id", "request-id", "x-requestid"):
            value = headers.get(key)
            if value:
                return str(value)
        return None

    def _extract_image(self, payload: Any) -> bytes:
        data = getattr(payload, "data", None)
        if not data:
            raise ProviderError("响应中没有图像数据")

        item = data[0]
        b64 = getattr(item, "b64_json", None)
        if b64:
            return base64.b64decode(b64)

        url = getattr(item, "url", None)
        if url:
            raise ProviderError(
                "端点返回的是图像 URL 而非 base64。本项目要求 base64 —— "
                "URL 是有时效的，落盘前就可能失效，破坏原图永不覆盖的前提。"
            )
        raise ProviderError("响应中既无 b64_json 也无 url")

    def _call(self, fn: Any, **kwargs: Any) -> tuple[bytes, str | None]:
        try:
            raw = fn(**kwargs)
        except Exception as exc:
            raise self._translate(exc) from exc

        request_id = self._request_id(raw)
        try:
            payload = raw.parse()
        except Exception as exc:
            raise self._translate(exc) from exc
        return self._extract_image(payload), request_id

    # -- 接口实现 ---------------------------------------------------------

    def _generate(
        self, prompt: str, size: tuple[int, int], model: str
    ) -> tuple[bytes, str | None]:
        return self._call(
            self._client.images.with_raw_response.generate,
            model=model,
            prompt=prompt,
            size=f"{size[0]}x{size[1]}",
            n=1,
        )

    def _edit(
        self,
        prompt: str,
        base_image: bytes,
        references: Sequence[ReferenceImage],
        size: tuple[int, int],
        model: str,
    ) -> tuple[bytes, str | None]:
        """基于参考图编辑。**不传 mask**（ADR-003 / PLAN §2.6）。

        base image 在前、参考图在后 —— canonical seed 走的就是参考图这条路。
        """
        images = [_named(base_image, "canvas.png")]
        images += [_named(ref.data, f"{ref.name}.png") for ref in references]

        return self._call(
            self._client.images.with_raw_response.edit,
            model=model,
            prompt=prompt,
            image=images if len(images) > 1 else images[0],
            size=f"{size[0]}x{size[1]}",
            n=1,
        )

    # -- doctor ------------------------------------------------------------

    def probe(self) -> dict[str, Any]:
        """列一次模型清单验证连通性与鉴权。**不产生计费的图像调用。**"""
        try:
            models = self._client.models.list()
        except Exception as exc:
            raise self._translate(exc) from exc

        ids = sorted(m.id for m in models.data)
        return {
            "provider": self.name,
            "model": self.model,
            "reachable": True,
            "model_available": self.model in ids,
            "image_models": [m for m in ids if "image" in m],
        }


def _named(data: bytes, filename: str) -> BytesIO:
    """SDK 用文件名推断 MIME 类型，裸 BytesIO 会被当成未知类型。"""
    buffer = BytesIO(data)
    buffer.name = filename
    return buffer
