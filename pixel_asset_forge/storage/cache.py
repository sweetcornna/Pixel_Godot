"""生成结果缓存（prompt hash + 输入图 hash）。

存在意义直白得很：**重跑失败任务不应该重复计费。**

SKILL.md 里"重复请求会命中 prompt hash 缓存，所以重跑失败任务是安全的"这句承诺
就落在这个模块上。它一旦失灵，用户每次调试都在烧钱。

缓存是内容寻址的：文件名即哈希，命中即字节级相同，因此不需要失效策略 ——
prompt 改一个字，哈希就变了，自然 miss。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..errors import ProcessingError
from .atomic import atomic_write_bytes, atomic_write_json
from .hashes import hash_bytes


@dataclass(frozen=True, slots=True)
class CacheEntry:
    key: str
    image_path: Path
    meta: dict[str, str]

    @property
    def content_hash(self) -> str:
        return self.meta.get("content_hash", "")


class GenerationCache:
    """磁盘上的内容寻址缓存。"""

    def __init__(self, root: str | Path, *, enabled: bool = True) -> None:
        self.root = Path(root)
        self.enabled = enabled
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    @contextmanager
    def key_lock(self, key: str) -> Iterator[None]:
        """同进程内同一生成键只允许一个调用者读写缓存。"""
        with self._locks_guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            yield

    def _entry_dir(self, key: str) -> Path:
        # 两级分片：单目录几万个文件在某些文件系统上会明显变慢。
        return self.root / key[:2] / key

    def get(self, key: str) -> CacheEntry | None:
        if not self.enabled:
            return None
        entry_dir = self._entry_dir(key)
        image = entry_dir / "image.png"
        meta_path = entry_dir / "meta.json"
        if not (image.exists() and meta_path.exists()):
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # 元数据损坏就当没命中 —— 缓存永远不应该让主流程失败。
            return None
        return CacheEntry(key=key, image_path=image, meta=meta)

    def put(self, key: str, data: bytes, meta: dict[str, str] | None = None) -> CacheEntry:
        if not self.enabled:
            return CacheEntry(key=key, image_path=Path(), meta={})
        entry_dir = self._entry_dir(key)
        entry_dir.mkdir(parents=True, exist_ok=True)
        image = entry_dir / "image.png"
        atomic_write_bytes(image, data)

        full_meta = dict(meta or {})
        full_meta["content_hash"] = hash_bytes(data)
        full_meta["size_bytes"] = str(len(data))
        atomic_write_json(entry_dir / "meta.json", full_meta)
        return CacheEntry(key=key, image_path=image, meta=full_meta)

    def read(self, key: str) -> bytes:
        entry = self.get(key)
        if entry is None:
            raise ProcessingError(f"缓存未命中：{key}")
        return entry.image_path.read_bytes()

    def stats(self) -> dict[str, int]:
        if not self.root.exists():
            return {"entries": 0, "bytes": 0}
        images = list(self.root.rglob("image.png"))
        return {
            "entries": len(images),
            "bytes": sum(p.stat().st_size for p in images),
        }
