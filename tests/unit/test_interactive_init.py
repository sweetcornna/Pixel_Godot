"""交互式 ``init`` 与项目级 ``.env`` 的命令行行为。"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import pixel_asset_forge.cli as cli_module
from pixel_asset_forge.cli import EXIT_OK, app

runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PIXEL_ASSET_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return tmp_path


def test_interactive_init_writes_all_three_values(project: Path) -> None:
    secret = "sk-interactive-secret"
    result = runner.invoke(
        app,
        ["init", "--interactive"],
        input=f"https://images.example.test/v1///\n{secret}\ncustom-image-model\nn\n",
    )

    assert result.exit_code == EXIT_OK
    config_text = (project / "pixel-asset.yaml").read_text(encoding="utf-8")
    config = yaml.safe_load(config_text)
    assert config["base_url"] == "https://images.example.test/v1"
    assert config["model"] == "custom-image-model"
    assert secret not in config_text
    assert (project / ".env").read_text(encoding="utf-8") == (
        f"PIXEL_ASSET_API_KEY={secret}\n"
    )
    assert stat.S_IMODE((project / ".env").stat().st_mode) == 0o600
    assert secret not in result.stdout


def test_interactive_init_accepts_all_defaults(project: Path) -> None:
    result = runner.invoke(app, ["init", "--interactive"], input="\n\n\n")

    assert result.exit_code == EXIT_OK
    config_text = (project / "pixel-asset.yaml").read_text(encoding="utf-8")
    config = yaml.safe_load(config_text)
    assert config["model"] == "gpt-image-2"
    assert "base_url" not in config
    assert "# base_url:" in config_text
    assert not (project / ".env").exists()
    assert "是否立即探测连通性?" not in result.stdout
    assert "pixel-asset doctor --probe" in result.stdout


def test_invalid_base_url_is_prompted_again(project: Path) -> None:
    result = runner.invoke(
        app,
        ["init", "--interactive"],
        input="ftp://invalid\nhttp://localhost:8080///\n\n\n",
    )

    assert result.exit_code == EXIT_OK
    assert result.stdout.count("端点服务器 base_url") == 2
    assert "必须以 http:// 或 https:// 开头" in result.stdout
    config = yaml.safe_load((project / "pixel-asset.yaml").read_text(encoding="utf-8"))
    assert config["base_url"] == "http://localhost:8080"


def test_interactive_init_uses_existing_yaml_values_as_defaults(project: Path) -> None:
    (project / "pixel-asset.yaml").write_text(
        "base_url: https://existing.test/v1/\nmodel: existing-model\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["init", "--interactive"],
        input="\n\nupdated-model\n",
    )

    assert result.exit_code == EXIT_OK
    config = yaml.safe_load((project / "pixel-asset.yaml").read_text(encoding="utf-8"))
    assert config["base_url"] == "https://existing.test/v1"
    assert config["model"] == "updated-model"


def test_existing_dotenv_only_updates_key_line(project: Path) -> None:
    env_path = project / ".env"
    env_path.write_text(
        "OTHER_SETTING=keep-me\n"
        "PIXEL_ASSET_API_KEY=old-key\n"
        "PIXEL_ASSET_MODEL=keep-this-too\n",
        encoding="utf-8",
    )
    env_path.chmod(0o644)

    result = runner.invoke(
        app,
        ["init", "--interactive"],
        input="\nnew-secret\n\nn\n",
    )

    assert result.exit_code == EXIT_OK
    assert env_path.read_text(encoding="utf-8") == (
        "OTHER_SETTING=keep-me\n"
        "PIXEL_ASSET_API_KEY=new-secret\n"
        "PIXEL_ASSET_MODEL=keep-this-too\n"
    )
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_interactive_init_preserves_custom_fields_in_existing_yaml(project: Path) -> None:
    (project / "pixel-asset.yaml").write_text(
        "# 用户自己的注释\n"
        "model: existing-model\n"
        "max_concurrency: 2\n"
        "output_dir: art\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["init", "--interactive"],
        input="https://ep.example.test\n\n\n",
    )

    assert result.exit_code == EXIT_OK
    config_text = (project / "pixel-asset.yaml").read_text(encoding="utf-8")
    config = yaml.safe_load(config_text)
    # 交互只更新 model / base_url，其余自定义字段与注释原样保留
    assert config["max_concurrency"] == 2
    assert config["output_dir"] == "art"
    assert config["base_url"] == "https://ep.example.test"
    assert config["model"] == "existing-model"
    assert "# 用户自己的注释" in config_text


def test_explicit_no_interactive_keeps_original_init_path(project: Path) -> None:
    result = runner.invoke(
        app,
        ["init", "--no-interactive"],
        input="https://should-not-be-read.test\nsk-unused\nunused-model\n",
    )

    assert result.exit_code == EXIT_OK
    config = yaml.safe_load((project / "pixel-asset.yaml").read_text(encoding="utf-8"))
    assert config["model"] == "gpt-image-2"
    assert "base_url" not in config
    assert not (project / ".env").exists()
    assert "端点服务器 base_url" not in result.stdout


def test_interactive_init_can_probe_with_shared_doctor_logic(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probed_models: list[str] = []

    def fake_probe(config: cli_module.Config) -> tuple[bool, str]:
        probed_models.append(config.model)
        assert config.require_api_key().get_secret_value() == "sk-probe-secret"
        return True, '{"reachable": true}'

    monkeypatch.setattr(cli_module, "_probe_provider", fake_probe)
    result = runner.invoke(
        app,
        ["init", "--interactive"],
        input="\nsk-probe-secret\nprobe-model\ny\n",
    )

    assert result.exit_code == EXIT_OK
    assert probed_models == ["probe-model"]
    assert "Provider 连通性" in result.stdout
    assert "reachable" in result.stdout


def test_doctor_reports_dotenv_key_source_without_printing_value(project: Path) -> None:
    secret = "sk-doctor-dotenv-secret"
    (project / ".env").write_text(f"OPENAI_API_KEY={secret}\n", encoding="utf-8")

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == EXIT_OK
    assert ".env 文件" in result.stdout
    assert secret not in result.stdout
