"""验证报告与状态的单一持久化入口。"""

from __future__ import annotations

from pathlib import Path

from ..errors import ProcessingError
from ..models.job import Job, JobEvent, JobKind, JobStatus
from ..models.manifest import AssetManifest
from ..models.validation import ValidationReport
from ..storage.artifacts import ArtifactStore
from ..storage.hashes import hash_file
from ..validation.engine import validate_asset


def static_validation_binding(
    store: ArtifactStore,
    job: Job,
    manifest: AssetManifest | None = None,
) -> tuple[bool, str]:
    """判断静态任务的成功状态是否仍绑定到当前磁盘成品。"""
    if job.kind is not JobKind.STATIC:
        return False, f"{job.id} 不是 static 任务"
    if job.status not in (JobStatus.VALIDATED, JobStatus.EXPORTED):
        return False, f"任务状态为 {job.status.value}"

    current_manifest = manifest or AssetManifest.load(store.manifest_path)
    if current_manifest.status not in ("validated", "exported"):
        return False, f"Manifest 状态为 {current_manifest.status}"
    entry = current_manifest.static_image
    if entry is None:
        return False, "Manifest 缺少 static_image"

    image_path = store.root / entry.image
    if not image_path.is_file():
        return False, f"静态成品缺失：{image_path}"
    disk_hash = hash_file(image_path)
    if disk_hash != entry.processed_hash:
        return False, "磁盘成品哈希与 Manifest processed_hash 不一致"
    if job.validated_processed_hash != entry.processed_hash:
        return False, "当前 processed_hash 未由该任务状态验证"
    return True, "静态成品与最近一次验证哈希一致"


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
        or (job.kind is JobKind.STATIC and job.status is JobStatus.EXPORTED)
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

    manifest = AssetManifest.load(store.manifest_path)
    for job in candidates:
        job.fire(
            JobEvent.VALIDATION_PASSED if report.passed else JobEvent.VALIDATION_FAILED
        )
        if job.kind is JobKind.STATIC:
            job.validated_processed_hash = (
                manifest.static_image.processed_hash
                if report.passed and manifest.static_image is not None
                else None
            )
    if candidates:
        store.save_job_table(table)

    manifest.status = "validated" if report.passed else "validation_failed"
    manifest.save(store.manifest_path)
    return report
