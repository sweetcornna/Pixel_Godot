"""静态资产共享验证与导出阶段的状态语义。"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import pytest
import yaml
from PIL import Image

from pixel_asset_forge.config import Config
from pixel_asset_forge.errors import ExportError, PauseRequested, ProcessingError
from pixel_asset_forge.models import AssetManifest, load_pack
from pixel_asset_forge.models.job import JobEvent, JobStatus
from pixel_asset_forge.models.validation import (
    ALL_CHECK_IDS,
    Check,
    CheckId,
    CheckResult,
    ValidationReport,
)
from pixel_asset_forge.pipelines.export import run_export
from pixel_asset_forge.pipelines.static_asset import (
    create_static_asset,
    validate_and_export_static_asset,
)
from pixel_asset_forge.pipelines.validation import run_validation
from pixel_asset_forge.providers import MockImageProvider
from pixel_asset_forge.storage import ArtifactStore
from pixel_asset_forge.storage.hashes import hash_file
from pixel_asset_forge.validation.engine import validate_asset


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


def _check(report: ValidationReport, check_id: CheckId) -> Check:
    matches = [check for check in report.checks if check.id == check_id]
    assert len(matches) == 1, check_id
    return matches[0]


def _refresh_static_hashes(store: ArtifactStore) -> AssetManifest:
    manifest = AssetManifest.load(store.manifest_path)
    assert manifest.static_image is not None
    entry = manifest.static_image
    entry.source_hash = hash_file(store.root / entry.source_image)
    entry.processed_hash = hash_file(store.root / entry.image)
    manifest.save(store.manifest_path)
    return manifest


def _load_static_frame(store: ArtifactStore) -> np.ndarray:
    return np.array(Image.open(store.frames / "static.png").convert("RGBA"))


def _save_static_frame(store: ArtifactStore, frame: np.ndarray) -> None:
    Image.fromarray(frame, mode="RGBA").save(store.frames / "static.png")
    _refresh_static_hashes(store)


def _hex_rgb(color: str) -> tuple[int, int, int]:
    return (
        int(color[1:3], 16),
        int(color[3:5], 16),
        int(color[5:7], 16),
    )


@pytest.mark.parametrize(
    "check_id",
    [
        "partial_alpha",
        "isolated_pixel",
        "key_color_residue",
        "palette_overflow",
        "cell_overflow",
    ],
)
def test_clean_static_product_passes_discriminating_check(
    static_store: ArtifactStore,
    check_id: CheckId,
) -> None:
    report = validate_asset(static_store.root)
    assert _check(report, check_id).result is CheckResult.PASS


def test_static_partial_alpha_defect_fails_when_antialiasing_is_disabled(
    static_store: ArtifactStore,
) -> None:
    frame = _load_static_frame(static_store)
    y, x = np.argwhere(frame[:, :, 3] == 255)[0]
    frame[y, x, 3] = 128
    _save_static_frame(static_store, frame)

    report = validate_asset(static_store.root)

    assert _check(report, "artifact_hash").result is CheckResult.PASS
    assert _check(report, "partial_alpha").result is CheckResult.FAIL


def test_static_partial_alpha_is_allowed_when_requested(
    static_store: ArtifactStore,
) -> None:
    frame = _load_static_frame(static_store)
    y, x = np.argwhere(frame[:, :, 3] == 255)[0]
    frame[y, x, 3] = 128
    _save_static_frame(static_store, frame)

    request_path = static_store.root / "request.yaml"
    request = yaml.safe_load(request_path.read_text(encoding="utf-8"))
    request["style"]["antialiasing"] = True
    request_path.write_text(
        yaml.safe_dump(request, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    report = validate_asset(static_store.root)

    assert _check(report, "partial_alpha").result is CheckResult.PASS


def test_static_isolated_pixel_defect_fails(static_store: ArtifactStore) -> None:
    frame = _load_static_frame(static_store)
    alpha = frame[:, :, 3]
    slot: tuple[int, int] | None = None
    for y in range(1, frame.shape[0] - 1):
        for x in range(1, frame.shape[1] - 1):
            if not alpha[y - 1 : y + 2, x - 1 : x + 2].any():
                slot = (y, x)
                break
        if slot is not None:
            break
    assert slot is not None

    manifest = AssetManifest.load(static_store.manifest_path)
    frame[slot] = (*_hex_rgb(manifest.palette.colors[0]), 255)
    _save_static_frame(static_store, frame)

    report = validate_asset(static_store.root)

    assert _check(report, "artifact_hash").result is CheckResult.PASS
    assert _check(report, "isolated_pixel").result is CheckResult.FAIL


def test_static_key_color_residue_defect_fails(static_store: ArtifactStore) -> None:
    frame = _load_static_frame(static_store)
    manifest = AssetManifest.load(static_store.manifest_path)
    key = _hex_rgb(manifest.background.color_used)
    frame[12:20, 12:20] = (*key, 255)
    _save_static_frame(static_store, frame)

    report = validate_asset(static_store.root)

    assert _check(report, "artifact_hash").result is CheckResult.PASS
    assert _check(report, "key_color_residue").result is CheckResult.FAIL


def test_static_prequant_key_residue_warns_but_does_not_block(
    static_store: ArtifactStore,
) -> None:
    """量化前的残留只告警，不阻断 —— 判 FAIL 会误伤任何带孔洞的资产。

    这个比例的大头是**被前景围住的封闭背景区**（钥匙的圆环孔、两腿之间、
    弓的弯里）：色键的漫水填充只清与画布外缘连通的部分，这些区域本来就该在
    ``strip_key_residue`` 那一步删掉，而那一步在量化**之前**，成品是干净的。

    真实生成实测（gpt-image-2，4 个资产）：唯一超过 5% 的样本是一个
    **完全合格**的金钥匙图标（6.4%，成品洋红像素为 0，视觉无瑕疵），
    判 FAIL 会把它挡在导出之外。另外三个资产都是 0.0%。
    "一个天天误报的验证器最终一定会被开发者关掉"（PLAN §9.1/§9.2）。

    真正该拦的是**成品**里还剩键控色 —— 见上一条测试。
    """
    manifest = AssetManifest.load(static_store.manifest_path)
    assert manifest.static_image is not None
    source_path = static_store.root / manifest.static_image.source_image
    source = np.array(Image.open(source_path).convert("RGB"))
    height, width = source.shape[:2]
    source[height * 45 // 100 : height * 60 // 100,
           width * 42 // 100 : width * 58 // 100] = _hex_rgb(
               manifest.background.color_used
           )
    Image.fromarray(source, mode="RGB").save(source_path)
    _refresh_static_hashes(static_store)

    report = validate_asset(static_store.root)

    assert _check(report, "artifact_hash").result is CheckResult.PASS
    assert _check(report, "palette_membership").result is CheckResult.PASS
    assert _check(report, "key_color_residue").result is CheckResult.WARN
    assert report.passed, "告警不能阻断导出"


def test_static_palette_overflow_defect_fails_at_max_colors_six(
    static_store: ArtifactStore,
) -> None:
    frame = np.zeros_like(_load_static_frame(static_store))
    colors = np.array(
        [(value, value * 37 % 256, value * 73 % 256) for value in range(256)],
        dtype=np.uint8,
    ).reshape(16, 16, 3)
    frame[8:24, 8:24, :3] = colors
    frame[8:24, 8:24, 3] = 255
    Image.fromarray(frame, mode="RGBA").save(static_store.frames / "static.png")

    manifest = _refresh_static_hashes(static_store)
    manifest.palette.max_colors = 6
    manifest.palette.colors = manifest.palette.colors[:6]
    manifest.save(static_store.manifest_path)

    report = validate_asset(static_store.root)

    assert _check(report, "artifact_hash").result is CheckResult.PASS
    assert _check(report, "palette_overflow").result is CheckResult.FAIL


def test_static_source_edge_overflow_defect_fails(static_store: ArtifactStore) -> None:
    manifest = AssetManifest.load(static_store.manifest_path)
    assert manifest.static_image is not None
    source_path = static_store.root / manifest.static_image.source_image
    source = np.array(Image.open(source_path).convert("RGB"))
    height, width = source.shape[:2]
    source[height // 3 : height * 2 // 3, : max(2, width // 8)] = _hex_rgb(
        manifest.palette.colors[0]
    )
    Image.fromarray(source, mode="RGB").save(source_path)
    _refresh_static_hashes(static_store)

    report = validate_asset(static_store.root)

    assert _check(report, "artifact_hash").result is CheckResult.PASS
    assert _check(report, "cell_overflow").result is CheckResult.FAIL


def test_static_report_explicitly_skips_every_animation_only_check(
    static_store: ArtifactStore,
) -> None:
    report = validate_asset(static_store.root)
    ids = [check.id for check in report.checks]
    skipped = [check for check in report.checks if check.result is CheckResult.SKIP]

    assert len(ids) == len(set(ids)) == len(ALL_CHECK_IDS)
    assert set(ids) == set(ALL_CHECK_IDS)
    assert {check.id for check in skipped} == {
        "frame_count",
        "frame_order_continuity",
        "beat_signature",
        "mirror_flip",
        "anchor_drift",
        "height_variation",
        "silhouette_variation",
        "duplicate_frame_exact",
        "duplicate_frame_approx",
        "static_animation",
        # tileset 专属的四条判据对静态资产同样不适用 —— 按本文件的既定口径
        # 也要显式记一笔，而不是从报告里消失。
        "tile_seam",
        "tile_border",
        "tile_adjacency",
        "map_adjacency",
    }
    assert all(check.skip_reason == "static_asset" for check in skipped)
    assert report.summary()["skipped"] == len(skipped)


def test_static_manifest_without_image_records_dependency_skips(
    static_store: ArtifactStore,
) -> None:
    manifest = AssetManifest.load(static_store.manifest_path)
    manifest.static_image = None
    manifest.save(static_store.manifest_path)

    report = validate_asset(static_store.root)
    checks = {check.id: check for check in report.checks}

    assert set(checks) == set(ALL_CHECK_IDS)
    assert checks["artifact_exists"].result is CheckResult.FAIL
    assert checks["artifact_hash"].skip_reason == "dependency_failed"
    assert checks["partial_alpha"].skip_reason == "dependency_failed"
    assert checks["frame_order_continuity"].skip_reason == "static_asset"


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


def test_exported_changed_product_with_refreshed_manifest_hash_is_revalidated(
    static_store: ArtifactStore,
) -> None:
    first = validate_and_export_static_asset(
        static_store.root,
        targets=["generic-json"],
    )
    assert first.export is not None

    table = static_store.load_job_table()
    assert table is not None
    job = next(iter(table))
    old_validated_hash = job.validated_processed_hash
    assert old_validated_hash is not None

    frame = np.fliplr(_load_static_frame(static_store)).copy()
    _save_static_frame(static_store, frame)
    manifest = AssetManifest.load(static_store.manifest_path)
    assert manifest.static_image is not None
    assert manifest.static_image.processed_hash != old_validated_hash

    completion = validate_and_export_static_asset(
        static_store.root,
        targets=["generic-json"],
    )

    assert completion.validation is not None
    assert completion.validation.passed is True
    assert completion.export is not None
    persisted = static_store.load_job_table()
    assert persisted is not None
    persisted_job = next(iter(persisted))
    assert persisted_job.status is JobStatus.EXPORTED
    assert persisted_job.validated_processed_hash == manifest.static_image.processed_hash
    assert [record.event for record in persisted_job.history].count(
        JobEvent.START_VALIDATION
    ) == 2


def test_direct_export_revalidates_changed_static_product(
    static_store: ArtifactStore,
) -> None:
    assert run_validation(static_store.root).passed is True
    table = static_store.load_job_table()
    assert table is not None
    old_validated_hash = next(iter(table)).validated_processed_hash
    assert old_validated_hash is not None

    frame = np.fliplr(_load_static_frame(static_store)).copy()
    _save_static_frame(static_store, frame)

    summary = run_export(static_store.root, targets=["generic-json"])

    assert any("已重新验证通过后导出" in note for note in summary.notes)
    persisted = static_store.load_job_table()
    assert persisted is not None
    job = next(iter(persisted))
    assert job.status is JobStatus.EXPORTED
    assert job.validated_processed_hash != old_validated_hash


def test_static_export_rejects_manifest_missing_static_image(
    static_store: ArtifactStore,
) -> None:
    assert run_validation(static_store.root).passed is True
    manifest = AssetManifest.load(static_store.manifest_path)
    manifest.static_image = None
    manifest.save(static_store.manifest_path)

    with pytest.raises(ExportError, match=r"静态资产.*缺少 static_image"):
        run_export(static_store.root, targets=["generic-json"])

    table = static_store.load_job_table()
    assert table is not None
    assert next(iter(table)).status is JobStatus.VALIDATED
    _assert_no_export_artifacts(static_store)


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
