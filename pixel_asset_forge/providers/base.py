"""Provider 抽象（ADR-002）。

**边界**：Provider 只回答"调用是否成功"，不回答"产出是否合格"。
帧数不对、姿势跨格、身份漂移 —— 这些都不是 ProviderError，它们走 validate → repair。
把两者混在一起会逼着 Provider 层去理解网格布局与身份一致性，分层就没了。

只暴露 ``generate`` 与 ``edit`` 两个方法，是为了防止上层依赖某个 Provider 的特有能力。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..errors import (
    InvalidRequestError,
    ProviderError,
    RetryLimitExceededError,
    TransientProviderError,
)
from ..logging_utils import get_logger
from ..storage.hashes import hash_bytes, prompt_hash

logger = get_logger("provider")


@dataclass(frozen=True, slots=True)
class ReferenceImage:
    """传给 ``edit`` 的参考图。canonical seed 就是以这个形式进入动画生成的。"""

    name: str
    data: bytes

    @property
    def content_hash(self) -> str:
        return hash_bytes(self.data)


def measure_png(data: bytes) -> tuple[int, int]:
    """从 PNG 头读出尺寸。

    不能相信"请求了多大就是多大"——端点实测会静默返回别的尺寸
    （Sprint 0 / A-1）。只解析 IHDR，不解码像素。
    """
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return (
            int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big"),
        )
    # 非 PNG（或被代理转码了）——退回 Pillow，它认得更多格式。
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(data)) as img:
        return img.size


@dataclass(frozen=True, slots=True)
class GenerationResult:
    """一次成功调用的产物。

    ``request_id`` 与 ``request_summary`` 是硬性要求（ADR-002）——
    失败溯源时没有它们就只能靠猜。

    ``requested_size`` 与 ``actual_size`` 必须分开记：
    实测端点不保证二者一致且不报错（Sprint 0 / A-1）。
    """

    image: bytes
    provider: str
    model: str
    requested_size: tuple[int, int]
    actual_size: tuple[int, int]
    prompt_hash: str
    request_id: str | None = None
    cached: bool = False
    request_summary: dict[str, Any] = field(default_factory=dict)
    attempts: int = 1

    @property
    def content_hash(self) -> str:
        return hash_bytes(self.image)

    @property
    def size_snapped(self) -> bool:
        """端点是否静默改了尺寸。为 True 时切帧必须按 ``actual_size`` 的比例进行。"""
        return self.requested_size != self.actual_size

    @property
    def aspect_drift(self) -> float:
        """返回图与请求的长短边比偏差（相对值）。"""
        from ..planning.grid_layout import aspect_mismatch

        return aspect_mismatch(self.requested_size, self.actual_size)

    def log_entry(self) -> dict[str, Any]:
        """写进 ``generation-log.json`` 的条目。**不含 prompt 原文与响应体。**"""
        return {
            "provider": self.provider,
            "model": self.model,
            "requested_size": list(self.requested_size),
            "actual_size": list(self.actual_size),
            "size_snapped": self.size_snapped,
            "prompt_hash": self.prompt_hash,
            "content_hash": self.content_hash,
            "request_id": self.request_id,
            "cached": self.cached,
            "attempts": self.attempts,
            "bytes": len(self.image),
        }


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """指数退避。**只对瞬态错误生效** —— 参数错误重试多少次都还是参数错误。"""

    max_retries: int = 4
    base_delay: float = 1.0
    max_delay: float = 30.0
    multiplier: float = 2.0

    def delay_for(self, attempt: int) -> float:
        return min(self.base_delay * (self.multiplier ** max(0, attempt - 1)), self.max_delay)


def with_retry[T](
    call: Callable[[], T],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], None] = time.sleep,
    label: str = "provider call",
) -> tuple[T, int]:
    """执行 ``call``，对瞬态错误退避重试。返回 ``(结果, 实际尝试次数)``。

    非瞬态错误立即向上抛 —— 反复重试一个非法请求只会烧配额。
    """
    attempt = 0
    last: TransientProviderError | None = None
    while attempt <= policy.max_retries:
        attempt += 1
        try:
            return call(), attempt
        except TransientProviderError as exc:
            last = exc
            if attempt > policy.max_retries:
                break
            delay = policy.delay_for(attempt)
            logger.warning(
                "%s 遇到瞬态错误，%.1fs 后重试（第 %d/%d 次）：%s",
                label, delay, attempt, policy.max_retries, exc.message,
            )
            sleep(delay)

    raise RetryLimitExceededError(
        f"{label} 重试 {policy.max_retries} 次后仍失败：{last.message if last else '未知原因'}",
        request_id=last.request_id if last else None,
    )


class ImageProvider(ABC):
    """所有生成后端的统一接口。"""

    name: str = "base"

    def __init__(self, model: str, *, retry_policy: RetryPolicy | None = None) -> None:
        self.model = model
        self.retry_policy = retry_policy or RetryPolicy()

    @staticmethod
    def check_requested_size(size: tuple[int, int]) -> None:
        """本地尺寸自检。**放在基类，不放在某个具体实现里。**

        实测目标端点对非法尺寸既不拒绝也不修正 —— 请求 1000×1024（不是 16 的倍数）
        照样返回一张 1254×1254 的图。指望服务端把关是靠不住的。

        自检也省掉一次无谓的往返：本地报错比等 API 返回快得多，
        错误信息也精确得多（能说清违反了四条约束里的哪一条）。
        """
        from ..planning.grid_layout import check_size

        violations = check_size(*size)
        if violations:
            detail = "；".join(f"{v.constraint}（{v.detail}）" for v in violations)
            raise InvalidRequestError(f"尺寸 {size[0]}×{size[1]} 不满足 API 约束：{detail}")

    # -- 子类实现 ---------------------------------------------------------

    @abstractmethod
    def _generate(self, prompt: str, size: tuple[int, int], model: str) -> tuple[bytes, str | None]:
        """返回 ``(png_bytes, request_id)``。异常必须是 :class:`ProviderError` 子类。"""

    @abstractmethod
    def _edit(
        self,
        prompt: str,
        base_image: bytes,
        references: Sequence[ReferenceImage],
        size: tuple[int, int],
        model: str,
    ) -> tuple[bytes, str | None]:
        """返回 ``(png_bytes, request_id)``。"""

    # -- 公开接口 ---------------------------------------------------------

    def generate_key(
        self, prompt: str, size: tuple[int, int], model: str | None = None
    ) -> str:
        """``generate`` 调用的缓存键。包装器复用它，避免两处各算一份而算岔。"""
        return prompt_hash(
            prompt,
            model=model or self.model,
            size=size,
            operation="generate",
            extra={"provider": self.name},
        )

    def edit_key(
        self,
        prompt: str,
        base_image: bytes,
        references: Sequence[ReferenceImage],
        size: tuple[int, int],
        model: str | None = None,
    ) -> str:
        """``edit`` 调用的缓存键。参考图内容参与哈希 —— 换了 seed 就该 miss。"""
        return prompt_hash(
            prompt,
            model=model or self.model,
            size=size,
            operation="edit",
            reference_hashes=[hash_bytes(base_image), *(r.content_hash for r in references)],
            extra={"provider": self.name},
        )

    def generate(
        self,
        prompt: str,
        *,
        size: tuple[int, int],
        model: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> GenerationResult:
        """从零生成一张图：种子图、道具、环境物件、初始 Tileset。"""
        self.check_requested_size(size)
        effective_model = model or self.model
        key = self.generate_key(prompt, size, effective_model)

        image, attempts = with_retry(
            lambda: self._generate(prompt, size, effective_model),
            self.retry_policy,
            sleep=sleep,
            label=f"{self.name}.generate",
        )
        data, request_id = image
        return self._wrap(
            data,
            model=effective_model,
            requested_size=size,
            prompt_hash=key,
            request_id=request_id,
            attempts=attempts,
            summary={
                "operation": "generate",
                "size": list(size),
                "prompt_chars": len(prompt),
            },
        )

    def edit(
        self,
        prompt: str,
        *,
        base_image: bytes,
        size: tuple[int, int],
        references: Sequence[ReferenceImage] = (),
        model: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> GenerationResult:
        """基于参考图编辑：动作网格、方向变体、换装、修复。

        **不传 mask**（ADR-003 / PLAN §2.6）：GPT Image 的 mask 是提示性约束，
        不保证遵循边界。seed 以纯参考图身份传入，身份保持更稳。
        """
        self.check_requested_size(size)
        effective_model = model or self.model
        key = self.edit_key(prompt, base_image, references, size, effective_model)

        image, attempts = with_retry(
            lambda: self._edit(prompt, base_image, references, size, effective_model),
            self.retry_policy,
            sleep=sleep,
            label=f"{self.name}.edit",
        )
        data, request_id = image
        return self._wrap(
            data,
            model=effective_model,
            requested_size=size,
            prompt_hash=key,
            request_id=request_id,
            attempts=attempts,
            summary={
                "operation": "edit",
                "size": list(size),
                "prompt_chars": len(prompt),
                "references": len(references),
            },
        )

    def _wrap(
        self,
        data: bytes,
        *,
        model: str,
        requested_size: tuple[int, int],
        prompt_hash: str,
        request_id: str | None,
        attempts: int,
        summary: dict[str, Any],
    ) -> GenerationResult:
        actual = measure_png(data)
        if actual != requested_size:
            # 不是错误，是这个端点的既定行为（Sprint 0 / A-1）。但必须留痕：
            # 下游按 actual 的比例切帧，排障时要能看出尺寸被改过。
            logger.warning(
                "%s 返回尺寸 %dx%d，与请求的 %dx%d 不同 —— 切帧将按实际尺寸按比例进行",
                self.name, actual[0], actual[1], requested_size[0], requested_size[1],
            )
        return GenerationResult(
            image=data,
            provider=self.name,
            model=model,
            requested_size=requested_size,
            actual_size=actual,
            prompt_hash=prompt_hash,
            request_id=request_id,
            attempts=attempts,
            request_summary=summary,
        )

    def probe(self) -> dict[str, Any]:
        """轻量连通性探测，供 ``doctor`` 使用。不产生计费调用。"""
        return {"provider": self.name, "model": self.model, "reachable": True}


__all__ = [
    "GenerationResult",
    "ImageProvider",
    "ProviderError",
    "ReferenceImage",
    "RetryPolicy",
    "with_retry",
]
