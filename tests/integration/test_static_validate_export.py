"""静态资产共享验证与导出阶段的状态语义。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
import yaml
from PIL import Image

from pixel_asset_forge.config import Config
from pixel_asset_forge.errors import PauseRequested, ProcessingError
from pixel_asset_forge.models import load_pack
from pixel_asset_forge.models.job import JobEvent, JobStatus
from pixel_asset_forge.pipelines.static_asset import (
    create_static_asset,
    validate_and_export_static_asset,
)
from pixel_asset_forge.pipelines.validation import run_validation
from pixel_asset_forge.providers import MockImageProvider
from pixel_asset_forge.storage import ArtifactStore


@pytest.fixture
def static_store(tmp_path: Path, examples_dir: Path) -> ArtifactStore:
    """用真实 mock 生成链准备一个停在 processed 的静态资产。"""
    config = Config(
        provider="mock",
        model="mock-image",
        output_dir=tmp_path / "outputs",
        cache_dir=tmp_path / "cache",
    )
    request = load_pack(examples_dir / "potion_pack.yaml").expand_requests()[0]
    request_path = tmp_path / "health_potion.yaml"
    request_path.write_text(
        yaml.safe_dump(
            request.model_dump(mode="json", exclude_none=True),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    create_static_asset(
        request_path,
        config,
        provider=MockImageProvider("mock-image"),
    )
    return ArtifactStore.for_asset(config.output_dir, request.asset_id)


def _set_job_status(store: ArtifactStore, status: JobStatus) -> None:
    table = store.load_job_table()
    assert table is not None
    next(iter(table)).status = status
    store.save_job_table(table)


def _break_static_frame(store: ArtifactStore) -> None:
    Image.new("RGBA", (32, 32), (0, 0, 0, 0)).save(store.frames / "static.png")


def _assert_no_export_artifacts(store: ArtifactStore) -> None:
    assert store.exports.exists()
    assert not any(store.exports.iterdir())


def test_validation_failed_job_is_revalidated_and_exported(
    static_store: ArtifactStore,
) -> None:
    _set_job_status(static_store, JobStatus.VALIDATION_FAILED)

    completion = validate_and_export_static_asset(
        static_store.root,
        targets=["generic-json"],
    )

    assert completion.validation is not None
    assert completion.validation.passed is True
    assert completion.export is not None
    table = static_store.load_job_table()
    assert table is not None
    job = next(iter(table))
    assert JobEvent.START_VALIDATION in [record.event for record in job.history]
    assert job.status is JobStatus.EXPORTED
    assert any(static_store.exports.rglob("*"))


def test_validation_failure_stops_before_export(static_store: ArtifactStore) -> None:
    _set_job_status(static_store, JobStatus.VALIDATION_FAILED)
    _break_static_frame(static_store)

    completion = validate_and_export_static_asset(
        static_store.root,
        targets=["generic-json"],
    )

    assert completion.passed is False
    assert completion.export is None
    table = static_store.load_job_table()
    assert table is not None
    assert next(iter(table)).status is JobStatus.VALIDATION_FAILED
    _assert_no_export_artifacts(static_store)


def test_validated_job_skips_validation_and_exports(static_store: ArtifactStore) -> None:
    assert run_validation(static_store.root).passed is True

    completion = validate_and_export_static_asset(
        static_store.root,
        targets=["generic-json"],
    )

    assert completion.validation is None
    assert completion.export is not None
    table = static_store.load_job_table()
    assert table is not None
    assert next(iter(table)).status is JobStatus.EXPORTED
    assert any(static_store.exports.rglob("*"))


def test_exported_job_can_reenter_export_idempotently(
    static_store: ArtifactStore,
) -> None:
    first = validate_and_export_static_asset(
        static_store.root,
        targets=["generic-json"],
    )
    assert first.export is not None

    second = validate_and_export_static_asset(
        static_store.root,
        targets=["generic-json"],
    )

    assert second.validation is None
    assert second.export is not None
    table = static_store.load_job_table()
    assert table is not None
    assert next(iter(table)).status is JobStatus.EXPORTED


def test_generated_job_cannot_enter_validation_or_export(
    static_store: ArtifactStore,
) -> None:
    _set_job_status(static_store, JobStatus.GENERATED)

    with pytest.raises(ProcessingError, match="不能进入静态验证与导出"):
        validate_and_export_static_asset(
            static_store.root,
            targets=["generic-json"],
        )

    table = static_store.load_job_table()
    assert table is not None
    assert next(iter(table)).status is JobStatus.GENERATED
    _assert_no_export_artifacts(static_store)


def test_missing_job_table_is_rejected(tmp_path: Path) -> None:
    asset_dir = tmp_path / "without-jobs"
    asset_dir.mkdir()

    with pytest.raises(ProcessingError, match="没有任务表"):
        validate_and_export_static_asset(
            asset_dir,
            targets=["generic-json"],
        )


def test_stop_request_pauses_after_validation_before_export(
    static_store: ArtifactStore,
) -> None:
    stop_requested = threading.Event()
    stop_requested.set()

    with pytest.raises(PauseRequested):
        validate_and_export_static_asset(
            static_store.root,
            targets=["generic-json"],
            stop_requested=stop_requested,
        )

    assert static_store.validation_report_path.exists()
    table = static_store.load_job_table()
    assert table is not None
    assert next(iter(table)).status is JobStatus.VALIDATED
    _assert_no_export_artifacts(static_store)
