"""Provider 装饰器：缓存与限流。

两个各管一件事的薄包装，组合使用：

```text
CachingProvider(ThrottledProvider(OpenAIImageProvider(...)))
```

顺序不能反。缓存在外层意味着**命中时根本不占用限流名额** ——
本来就没有网络调用，占名额是白白拖慢整批任务。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from typing import Any

from ..logging_utils import get_logger
from ..storage.cache import GenerationCache
from .base import GenerationResult, ImageProvider, ReferenceImage, measure_png
from .throttle import Throttle

logger = get_logger("provider.wrapper")


class _Delegating(ImageProvider):
    """把抽象方法转发给内层 Provider 的基类。"""

    def __init__(self, inner: ImageProvider) -> None:
        super().__init__(inner.model, retry_policy=inner.retry_policy)
        self.inner = inner
        # 实例属性遮蔽类属性：产出记录里要出现的是真实后端名（openai），
        # 而不是包装器名 —— 包装是实现细节，不该泄漏到 Manifest 里。
        self.name = inner.name

    def _generate(
        self, prompt: str, size: tuple[int, int], model: str
    ) -> tuple[bytes, str | None]:
        return self.inner._generate(prompt, size, model)

    def _edit(
        self,
        prompt: str,
        base_image: bytes,
        references: Sequence[ReferenceImage],
        size: tuple[int, int],
        model: str,
    ) -> tuple[bytes, str | None]:
        return self.inner._edit(prompt, base_image, references, size, model)

    def probe(self) -> dict[str, Any]:
        return self.inner.probe()


class ThrottledProvider(_Delegating):
    """给每次真实网络调用套上并发上限与最小间隔。

    限流放在 ``_generate`` / ``_edit`` 这一层（而不是 ``generate`` / ``edit``），
    是为了让**每次退避重试也各自占一个名额** —— 重试同样是真实请求，
    不算进限流的话，一批任务集体撞 429 时会同时重试，把限流打得更狠。
    """

    def __init__(self, inner: ImageProvider, throttle: Throttle) -> None:
        super().__init__(inner)
        self.throttle = throttle

    def _generate(
        self, prompt: str, size: tuple[int, int], model: str
    ) -> tuple[bytes, str | None]:
        with self.throttle.slot():
            return self.inner._generate(prompt, size, model)

    def _edit(
        self,
        prompt: str,
        base_image: bytes,
        references: Sequence[ReferenceImage],
        size: tuple[int, int],
        model: str,
    ) -> tuple[bytes, str | None]:
        with self.throttle.slot():
            return self.inner._edit(prompt, base_image, references, size, model)


class CachingProvider(_Delegating):
    """prompt hash + 输入图 hash 缓存。

    这个类兑现的是 SKILL.md 里那句承诺：
    **"重复请求会命中 prompt hash 缓存，所以重跑失败任务是安全的"**。
    它一旦失灵，用户每次调试都在重复付费。

    缓存是内容寻址的，因此不需要失效策略：prompt 改一个字哈希就变了，自然 miss。
    """

    def __init__(self, inner: ImageProvider, cache: GenerationCache) -> None:
        super().__init__(inner)
        self.cache = cache
        self.hits = 0
        self.misses = 0
        self._bypass = False

    @contextmanager
    def bypass(self) -> Iterator[None]:
        """临时跳过缓存**查找**（仍然写入）。

        这里区分了两种表面相同、语义相反的重跑：

        - **重试失败的调用** —— 上次根本没拿到图，缓存是朋友：命中即免费。
        - **因产出不合格而重生成** —— 上次拿到了图，只是画得不对。
          此时命中缓存会原样返回那张不合格的图，修复永远不可能成功。

        生成层不可复现（PLAN §2.7）正是重生成的**意义所在**：
        同一个 prompt 再摇一次，可能就落在合格的那一侧。缓存把这个机会掐掉了。

        仍然写入是刻意的：新图覆盖旧条目，后续普通调用拿到的是最新一次的产出。
        """
        previous, self._bypass = self._bypass, True
        try:
            yield
        finally:
            self._bypass = previous

    def _from_cache(
        self, key: str, *, requested_size: tuple[int, int], model: str, summary: dict[str, Any]
    ) -> GenerationResult | None:
        if self._bypass:
            logger.info("重生成：跳过缓存查找 %s", key[:12])
            return None
        entry = self.cache.get(key)
        if entry is None:
            return None
        data = entry.image_path.read_bytes()
        self.hits += 1
        logger.info("缓存命中 %s，跳过 API 调用", key[:12])
        return GenerationResult(
            image=data,
            provider=self.name,
            model=model,
            requested_size=requested_size,
            actual_size=measure_png(data),
            prompt_hash=key,
            request_id=entry.meta.get("request_id"),
            cached=True,
            request_summary=summary,
            attempts=0,
        )

    def _store(self, result: GenerationResult) -> None:
        self.cache.put(
            result.prompt_hash,
            result.image,
            {
                "request_id": result.request_id or "",
                "model": result.model,
                "provider": result.provider,
                "actual_size": f"{result.actual_size[0]}x{result.actual_size[1]}",
            },
        )

    def generate(
        self,
        prompt: str,
        *,
        size: tuple[int, int],
        model: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> GenerationResult:
        effective_model = model or self.model
        key = self.generate_key(prompt, size, effective_model)
        summary = {"operation": "generate", "size": list(size), "prompt_chars": len(prompt)}

        cached = self._from_cache(
            key, requested_size=size, model=effective_model, summary=summary
        )
        if cached is not None:
            return cached

        self.misses += 1
        result = super().generate(prompt, size=size, model=model, sleep=sleep)
        self._store(result)
        return result

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
        effective_model = model or self.model
        key = self.edit_key(prompt, base_image, references, size, effective_model)
        summary = {
            "operation": "edit",
            "size": list(size),
            "prompt_chars": len(prompt),
            "references": len(references),
        }

        cached = self._from_cache(
            key, requested_size=size, model=effective_model, summary=summary
        )
        if cached is not None:
            return cached

        self.misses += 1
        result = super().edit(
            prompt, base_image=base_image, size=size, references=references,
            model=model, sleep=sleep,
        )
        self._store(result)
        return result


def bypass_cache(provider: ImageProvider) -> AbstractContextManager[None]:
    """在 Provider 栈里找到 :class:`CachingProvider` 并跳过其缓存查找。

    栈里没有缓存层时返回一个空上下文 —— 调用方不必关心是不是开了缓存。
    """
    target: ImageProvider | None = provider
    while target is not None:
        if isinstance(target, CachingProvider):
            return target.bypass()
        target = getattr(target, "inner", None)
    return nullcontext()
