"""静态 pickup 从生成到验证、导出的完整闭环。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pixel_asset_forge.config import Config
from pixel_asset_forge.errors import ExportError, ProcessingError
from pixel_asset_forge.models import AssetManifest, load_pack, load_request
from pixel_asset_forge.models.job import JobStatus
from pixel_asset_forge.pipelines.export import run_export
from pixel_asset_forge.pipelines.process import run_process
from pixel_asset_forge.pipelines.static_asset import create_static_asset
from pixel_asset_forge.pipelines.validation import run_validation
from pixel_asset_forge.planning import plan_request
from pixel_asset_forge.providers import MockImageProvider
from pixel_asset_forge.storage import ArtifactStore
from pixel_asset_forge.storage.hashes import hash_file


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        provider="mock",
        model="mock-image",
        output_dir=tmp_path / "outputs",
        cache_dir=tmp_path / "cache",
    )


@pytest.fixture
def request_file(tmp_path: Path, examples_dir: Path) -> Path:
    request = load_pack(examples_dir / "potion_pack.yaml").expand_requests()[0]
    path = tmp_path / "health_potion.yaml"
    path.write_text(
        "schema_version: \"1.1\"\n"
        "asset_id: health_potion\n"
        "asset_type: pickup\n"
        f"description: {request.description}\n"
        "style:\n"
        "  perspective: top_down_3_4\n"
        "  target_size: [32, 32]\n"
        "  max_colors: 12\n"
        "  palette_colors:\n"
        + "".join(f'    - \"{color}\"\n' for color in request.style.palette_colors or ())
        + "background:\n"
        "  mode: chroma_key\n"
        '  color: "#FF00FF"\n'
        "export:\n"
        "  targets: [generic-json, godot]\n",
        encoding="utf-8",
    )
    return path


def test_static_pickup_runs_to_export(request_file: Path, config: Config) -> None:
    result = create_static_asset(request_file, config)
    store = ArtifactStore.for_asset(config.output_dir, "health_potion")

    assert result.source_path.exists()
    assert result.image_path == store.frames / "static.png"
    assert result.image_path.exists()

    manifest = AssetManifest.load(store.manifest_path)
    assert manifest.provider.model == "mock-image"
    assert manifest.anchor.type == "center"
    assert manifest.static_image is not None
    assert manifest.static_image.image == "frames/static.png"
    assert manifest.palette.colors == load_pack_data(request_file)["style"]["palette_colors"]

    log = json.loads(store.generation_log_path.read_text(encoding="utf-8"))
    assert log[-1]["model"] == "mock-image"

    with pytest.raises(ExportError, match="validated"):
        run_export(store.root, targets=["generic-json", "godot"])

    report = run_validation(store.root)
    assert report.checks
    assert report.passed
    assert {check.id for check in report.checks} >= {
        "artifact_exists",
        "artifact_hash",
        "frame_size",
        "blank_frame",
        "content_bounds",
        "palette_membership",
        "transparent_rgb_residue",
    }

    summary = run_export(store.root, targets=["generic-json", "godot"])
    assert len(summary.files) == 3
    assert summary.contact_sheet is not None and summary.contact_sheet.exists()
    generic = json.loads(
        (store.exports / "generic-json" / "health_potion.json").read_text(encoding="utf-8")
    )
    assert generic["image"]["file"] == "health_potion.png"
    assert (store.exports / "godot" / "health_potion.png").exists()

    table = store.load_job_table()
    assert table is not None
    assert next(iter(table)).status is JobStatus.EXPORTED


def test_static_pipeline_accepts_weapon(request_file: Path, config: Config) -> None:
    weapon_file = request_file.with_name("iron_sword.yaml")
    weapon_file.write_text(
        request_file.read_text(encoding="utf-8")
        .replace("asset_id: health_potion", "asset_id: iron_sword")
        .replace("asset_type: pickup", "asset_type: weapon"),
        encoding="utf-8",
    )

    result = create_static_asset(weapon_file, config)
    store = ArtifactStore.for_asset(config.output_dir, "iron_sword")

    assert result.image_path.exists()
    manifest = AssetManifest.load(store.manifest_path)
    assert manifest.asset_type == "weapon"
    assert manifest.static_image is not None
    assert run_validation(store.root).passed


@pytest.mark.parametrize("asset_type", ["prop", "ui_icon", "environment_object"])
def test_static_pipeline_accepts_remaining_static_asset_types(
    request_file: Path, config: Config, asset_type: str
) -> None:
    asset_id = f"test_{asset_type}"
    static_file = request_file.with_name(f"{asset_id}.yaml")
    static_file.write_text(
        request_file.read_text(encoding="utf-8")
        .replace("asset_id: health_potion", f"asset_id: {asset_id}")
        .replace("asset_type: pickup", f"asset_type: {asset_type}"),
        encoding="utf-8",
    )

    result = create_static_asset(static_file, config)
    store = ArtifactStore.for_asset(config.output_dir, asset_id)

    assert result.image_path.exists()
    manifest = AssetManifest.load(store.manifest_path)
    assert manifest.asset_type == asset_type
    assert manifest.static_image is not None
    assert run_validation(store.root).passed


def test_static_pipeline_rejects_unsupported_asset_type(
    request_file: Path, config: Config
) -> None:
    spell_file = request_file.with_name("healing_spell.yaml")
    spell_file.write_text(
        request_file.read_text(encoding="utf-8")
        .replace("asset_id: health_potion", "asset_id: healing_spell")
        .replace("asset_type: pickup", "asset_type: spell"),
        encoding="utf-8",
    )

    with pytest.raises(
        ProcessingError,
        match=(
            r"无 animations 的资产类型：environment_object, pickup, prop, ui_icon, weapon"
        ),
    ):
        create_static_asset(spell_file, config)


def test_resume_from_processed_does_not_call_provider_again(
    request_file: Path, config: Config
) -> None:
    first = create_static_asset(request_file, config)
    second = create_static_asset(request_file, config)
    assert first.source_path.read_bytes() == second.source_path.read_bytes()
    assert second.cached is False
    store = ArtifactStore.for_asset(config.output_dir, "health_potion")
    log = json.loads(store.generation_log_path.read_text(encoding="utf-8"))
    assert len(log) == 1


def test_process_rebuilds_completed_static_asset_without_provider_call(
    request_file: Path, config: Config
) -> None:
    created = create_static_asset(request_file, config)
    store = ArtifactStore.for_asset(config.output_dir, "health_potion")
    assert run_validation(store.root).passed
    run_export(store.root, targets=["generic-json"])
    table = store.load_job_table()
    assert table is not None
    assert next(iter(table)).status is JobStatus.EXPORTED
    original_product = created.image_path.read_bytes()
    generation_log = store.generation_log_path.read_bytes()
    created.image_path.write_bytes(b"not a png")

    summaries = run_process(store.root)

    assert summaries[0]["key"] == "static"
    assert created.image_path.read_bytes() == original_product
    assert store.generation_log_path.read_bytes() == generation_log
    manifest = AssetManifest.load(store.manifest_path)
    assert manifest.static_image is not None
    assert manifest.static_image.image == "frames/static.png"
    assert manifest.static_image.processed_hash == hash_file(created.image_path)


def test_generating_resume_rejects_uncached_provider_result(
    request_file: Path, config: Config
) -> None:
    request = load_request(request_file)
    store = ArtifactStore.for_asset(config.output_dir, request.asset_id).ensure()
    planned = plan_request(
        request, provider=config.provider, model=config.model
    )
    job = next(iter(planned.jobs))
    job.status = JobStatus.GENERATING
    store.save_job_table(planned.jobs)
    provider = MockImageProvider(config.model)

    with pytest.raises(ProcessingError, match=r"恢复调用未命中 cache.*拒绝提交"):
        create_static_asset(
            request_file,
            config,
            provider=provider,
            allow_cached_resume=True,
        )

    assert len(provider.calls) == 1
    assert not store.source_path("static").exists()
    persisted = store.load_job_table()
    assert persisted is not None
    assert next(iter(persisted)).status is JobStatus.GENERATING


def load_pack_data(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))
