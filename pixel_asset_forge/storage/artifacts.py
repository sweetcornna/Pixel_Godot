"""Artifact Store —— 一个资产的磁盘布局（PLAN §11）。

最重要的一条不变量：**``source/`` 下的原始生成图永不覆盖。**
这是 ``pixel-asset process`` 能离线重跑的前提 —— 一旦原图被覆盖，
所有"不重新生成就能修"的路径就全断了，每次调参都要重新花钱。

所以 :meth:`write_source` 在目标已存在且内容不同的时候是**报错**，而不是静默覆盖。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..constants import (
    GENERATION_LOG_FILE,
    MANIFEST_FILE,
    OUTPUT_SUBDIRS,
    REPAIR_PLAN_FILE,
    REQUEST_FILE,
    VALIDATION_REPORT_FILE,
)
from ..errors import ProcessingError
from ..models.job import JobTable
from .hashes import hash_bytes, hash_file

JOB_TABLE_FILE = "jobs/job-table.json"


@dataclass(frozen=True, slots=True)
class ArtifactStore:
    """``outputs/<asset_id>/`` 下的全部路径。"""

    root: Path

    @classmethod
    def for_asset(cls, output_dir: str | Path, asset_id: str) -> ArtifactStore:
        return cls(root=Path(output_dir) / asset_id)

    # -- 目录 -------------------------------------------------------------

    def ensure(self) -> ArtifactStore:
        self.root.mkdir(parents=True, exist_ok=True)
        for sub in OUTPUT_SUBDIRS:
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        return self

    @property
    def source(self) -> Path:
        return self.root / "source"

    @property
    def frames(self) -> Path:
        return self.root / "frames"

    @property
    def sheets(self) -> Path:
        return self.root / "sheets"

    @property
    def previews(self) -> Path:
        return self.root / "previews"

    @property
    def exports(self) -> Path:
        return self.root / "exports"

    def intermediate(self, stage: str) -> Path:
        return self.root / "intermediate" / stage

    def frames_of(self, key: str) -> Path:
        return self.frames / key

    # -- 顶层文件 ---------------------------------------------------------

    @property
    def request_path(self) -> Path:
        return self.root / REQUEST_FILE

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_FILE

    @property
    def validation_report_path(self) -> Path:
        return self.root / VALIDATION_REPORT_FILE

    @property
    def repair_plan_path(self) -> Path:
        return self.root / REPAIR_PLAN_FILE

    @property
    def generation_log_path(self) -> Path:
        return self.root / GENERATION_LOG_FILE

    @property
    def job_table_path(self) -> Path:
        return self.root / JOB_TABLE_FILE

    # -- 原始生成图 -------------------------------------------------------

    def source_path(self, key: str, *, suffix: str = ".png") -> Path:
        """``walk_down`` → ``source/walk-down-original.png``。"""
        return self.source / f"{key.replace('_', '-')}-original{suffix}"

    def write_source(self, key: str, data: bytes, *, suffix: str = ".png") -> Path:
        """写入原始生成图。

        已存在同名文件时：内容相同则原样复用（重跑缓存命中的正常情形），
        内容不同则**报错** —— 覆盖原图会摧毁离线重跑能力。
        重生成应当先调用 :meth:`archive_source`。
        """
        path = self.source_path(key, suffix=suffix)
        path.parent.mkdir(parents=True, exist_ok=True)

        if path.exists():
            if hash_file(path) == hash_bytes(data):
                return path
            raise ProcessingError(
                f"{path} 已存在且内容不同。原始生成图永不覆盖 —— "
                f"重生成前请先调用 archive_source({key!r})。"
            )

        path.write_bytes(data)
        return path

    def archive_source(self, key: str, *, suffix: str = ".png") -> Path | None:
        """把既有原图改名归档为 ``<key>-original.rN.png``，返回归档路径。

        重生成时调用。归档而非删除：失败模式的样本对调 prompt 很有价值。
        """
        path = self.source_path(key, suffix=suffix)
        if not path.exists():
            return None
        revision = 1
        while True:
            archived = path.with_suffix(f".r{revision}{suffix}")
            if not archived.exists():
                path.rename(archived)
                return archived
            revision += 1

    def iter_sources(self) -> Iterator[Path]:
        if not self.source.exists():
            return iter(())
        return iter(sorted(self.source.glob("*")))

    # -- JSON 读写 --------------------------------------------------------

    @staticmethod
    def _write_json(path: Path, payload: Any) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return path

    def save_job_table(self, table: JobTable) -> Path:
        return self._write_json(self.job_table_path, table.model_dump(mode="json"))

    def load_job_table(self) -> JobTable | None:
        """读取既有任务表；不存在返回 None（首次 plan 的正常情形）。"""
        if not self.job_table_path.exists():
            return None
        try:
            data = json.loads(self.job_table_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProcessingError(
                f"{self.job_table_path}：任务表不是合法 JSON —— {exc}"
            ) from exc
        return JobTable.model_validate(data)

    def append_generation_log(self, entry: dict[str, Any]) -> Path:
        """追加一条生成日志。条目里**不得包含 API Key 或完整响应体**。"""
        log: list[dict[str, Any]] = []
        if self.generation_log_path.exists():
            try:
                log = json.loads(self.generation_log_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log = []
        log.append(entry)
        return self._write_json(self.generation_log_path, log)

    def save_request_copy(self, source: str | Path) -> Path:
        """把请求原文复制进资产目录 —— 产物必须自带它的输入。"""
        text = Path(source).read_text(encoding="utf-8")
        self.root.mkdir(parents=True, exist_ok=True)
        self.request_path.write_text(text, encoding="utf-8")
        return self.request_path
