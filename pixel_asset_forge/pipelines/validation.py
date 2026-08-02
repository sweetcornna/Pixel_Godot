"""验证报告与状态的单一持久化入口。"""

from __future__ import annotations

from pathlib import Path

from ..errors import ProcessingError
from ..models.job import JobEvent, JobStatus
from ..models.manifest import AssetManifest
from ..models.validation import ValidationReport
from ..storage.artifacts import ArtifactStore
from ..validation.engine import validate_asset


def run_validation(asset_dir: str | Path) -> ValidationReport:
    """运行验证，并在报告落盘后推进 JobTable 与 Manifest。"""
    store = ArtifactStore(root=Path(asset_dir))
    table = store.load_job_table()
    if table is None:
        raise ProcessingError(f"{asset_dir} 下没有任务表 —— 先完成生成与处理")
    if not store.manifest_path.exists():
        raise ProcessingError(f"{asset_dir} 下没有 asset-manifest.json")

    candidates = [
        job
        for job in table
        if job.status
        in (
            JobStatus.PROCESSED,
            JobStatus.VALIDATING,
            JobStatus.VALIDATED,
            JobStatus.VALIDATION_FAILED,
        )
    ]
    if not candidates:
        states = ", ".join(sorted({job.status.value for job in table}))
        raise ProcessingError(f"没有可验证任务，当前状态：{states}")

    for job in candidates:
        if job.status is not JobStatus.VALIDATING:
            job.fire(JobEvent.START_VALIDATION)
    if candidates:
        store.save_job_table(table)

    report = validate_asset(store.root)
    report.save(store.validation_report_path)

    for job in candidates:
        job.fire(
            JobEvent.VALIDATION_PASSED if report.passed else JobEvent.VALIDATION_FAILED
        )
    if candidates:
        store.save_job_table(table)

    manifest = AssetManifest.load(store.manifest_path)
    manifest.status = "validated" if report.passed else "validation_failed"
    manifest.save(store.manifest_path)
    return report
