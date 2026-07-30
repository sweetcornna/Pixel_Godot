"""内容哈希。

生成层不可复现（PLAN §2.7：无 seed 参数，同一 prompt 每次结果不同），
所以 **prompt hash + 输入图 hash 是唯一的"生成层复现"手段**。

因此哈希必须覆盖一切会改变产出的输入 —— 漏掉任何一个（比如尺寸、模型名），
缓存就会把不同请求的结果当成同一个返回。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prompt_hash(
    prompt: str,
    *,
    model: str,
    size: tuple[int, int],
    operation: str = "generate",
    reference_hashes: Sequence[str] = (),
    extra: dict[str, Any] | None = None,
) -> str:
    """一次生成调用的指纹。

    ``reference_hashes`` 会先排序 —— 参考图的传入顺序不影响语义，
    但如果不排序，同一组参考图换个顺序就会 miss 掉缓存。
    """
    payload = {
        "prompt": prompt,
        "model": model,
        "size": list(size),
        "operation": operation,
        "references": sorted(reference_hashes),
        "extra": extra or {},
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
