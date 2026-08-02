"""create-asset 单静态资产完整链与请求边界。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from pixel_asset_forge.cli import (
    EXIT_INVALID_REQUEST,
    EXIT_OK,
    EXIT_VALIDATION_FAILED,
    app,
)
from pixel_asset_forge.models import AssetManifest
from pixel_asset_forge.models.job import JobKind, JobStatus
from pixel_asset_forge.storage import ArtifactStore

runner = CliRunner()


def _write_config(path: Path) -> Path:
    path.write_text(
        "provider: mock\n"
        "model: configured-model\n"
        "output_dir: outputs\n"
        "cache_dir: cache\n",
        encoding="utf-8",
    )
    return path


def _static_request(asset_id: str, asset_type: str) -> dict:
    return {
        "schema_version": "1.1",
        "asset_id": asset_id,
        "asset_type": asset_type,
        "description": f"A clearly readable isolated {asset_type} for a fantasy game inventory.",
        "style": {
            "perspective": "top_down_3_4",
            "target_size": [32, 32],
            "max_colors": 12,
            "outline": "single_pixel_dark",
            "shading": "two_tone",
            "antialiasing": False,
            "lighting": "fixed_top_left",
        },
        "background": {
            "mode": "chroma_key",
            "color": "#FF00FF",
            "fallback_colors": ["#00FF00", "#00FFFF"],
        },
        "export": {"targets": ["generic-json", "godot"]},
    }


@pytest.mark.parametrize(
    ("asset_type", "asset_id", "model_override"),
    [
        ("prop", "wooden_crate", "prop-model"),
        ("ui_icon", "quest_marker", None),
    ],
)
def test_create_asset_runs_static_request_to_export_without_saved_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    asset_type: str,
    asset_id: str,
    model_override: str | None,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PIXEL_ASSET_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = _write_config(tmp_path / "mock.yaml")
    request_path = tmp_path / f"{asset_id}.yaml"
    request_path.write_text(
        yaml.safe_dump(_static_request(asset_id, asset_type), sort_keys=False),
        encoding="utf-8",
    )
    store = ArtifactStore.for_asset(tmp_path / "outputs", asset_id)
    assert not store.job_table_path.exists()

    argv = ["create-asset", str(request_path), "--config", str(config)]
    if model_override is not None:
        argv.extend(["--model", model_override])
    created = runner.invoke(app, argv)

    assert created.exit_code == EXIT_OK
    assert "验证" in created.stdout
    assert "通过" in created.stdout
    manifest = AssetManifest.load(store.manifest_path)
    table = store.load_job_table()
    assert manifest.asset_type == asset_type
    assert manifest.status == "exported"
    assert manifest.provider.model == (model_override or "configured-model")
    assert manifest.static_image is not None
    assert (store.frames / "static.png").exists()
    assert store.validation_report_path.exists()
    assert (store.exports / "generic-json" / f"{asset_id}.json").exists()
    assert (store.exports / "godot" / f"{asset_id}.png").exists()
    assert table is not None
    jobs = tuple(table)
    assert len(jobs) == 1
    assert jobs[0].kind is JobKind.STATIC
    assert jobs[0].status is JobStatus.EXPORTED
    generation_log = json.loads(store.generation_log_path.read_text(encoding="utf-8"))
    assert len(generation_log) == 1


@pytest.mark.parametrize("request_kind", ["character", "static_with_animations"])
def test_create_asset_rejects_animation_requests_and_points_to_create_character(
    tmp_path: Path,
    examples_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_kind: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    config = _write_config(tmp_path / "mock.yaml")
    request_path = tmp_path / f"{request_kind}.yaml"
    if request_kind == "character":
        request_path.write_text(
            (examples_dir / "knight.yaml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    else:
        request = _static_request("animated_prop", "prop")
        request["animations"] = [
            {"name": "loop", "frames": 4, "fps": 8, "loop": True}
        ]
        request_path.write_text(yaml.safe_dump(request, sort_keys=False), encoding="utf-8")

    created = runner.invoke(
        app,
        ["create-asset", str(request_path), "--config", str(config)],
    )

    assert created.exit_code == EXIT_INVALID_REQUEST
    assert "create-character" in created.stderr
    assert not (tmp_path / "outputs").exists()


def test_create_asset_returns_validation_failed_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    config = _write_config(tmp_path / "mock.yaml")
    request_path = tmp_path / "invalid_prop.yaml"
    request_path.write_text(
        yaml.safe_dump(_static_request("invalid_prop", "prop"), sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "pixel_asset_forge.cli.run_create_static_asset",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "pixel_asset_forge.cli.validate_and_export_static_asset",
        lambda *_args, **_kwargs: SimpleNamespace(passed=False, export=None),
    )

    created = runner.invoke(
        app,
        ["create-asset", str(request_path), "--config", str(config)],
    )

    assert created.exit_code == EXIT_VALIDATION_FAILED
    assert "验证未通过" in created.stderr
