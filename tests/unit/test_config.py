"""配置加载与密钥保护。

两条硬要求：
- 优先级 **环境变量 > 项目级 > 用户级 > 默认值**
- **API Key 只能来自环境变量。** 配置文件里出现 Key 一律报错，
  让"Key 被 commit 进仓库"在结构上不可能发生。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pixel_asset_forge.config import Config, load_config
from pixel_asset_forge.errors import ConfigError, MissingApiKeyError


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_when_nothing_is_configured(tmp_path: Path) -> None:
    config = load_config(user_config=tmp_path / "none.yaml", project_config=None, env={})
    assert config.provider == "openai"
    assert config.model == "gpt-image-2"
    assert config.max_concurrency == 3


def test_project_config_overrides_user_config(tmp_path: Path) -> None:
    user = write(tmp_path / "user.yaml", "model: user-model\nmax_retries: 1\n")
    project = write(tmp_path / "project.yaml", "model: project-model\n")
    config = load_config(user_config=user, project_config=project, env={})
    assert config.model == "project-model"
    assert config.max_retries == 1  # 项目配置没覆盖的字段沿用用户配置


def test_env_overrides_everything(tmp_path: Path) -> None:
    project = write(tmp_path / "project.yaml", "model: project-model\nmax_retries: 1\n")
    config = load_config(
        user_config=tmp_path / "none.yaml",
        project_config=project,
        env={"PIXEL_ASSET_MODEL": "env-model", "PIXEL_ASSET_MAX_RETRIES": "7"},
    )
    assert config.model == "env-model"
    assert config.max_retries == 7


def test_cli_overrides_beat_env(tmp_path: Path) -> None:
    config = load_config(
        user_config=tmp_path / "none.yaml",
        project_config=None,
        env={"PIXEL_ASSET_PROVIDER": "openai"},
        overrides={"provider": "mock"},
    )
    assert config.provider == "mock"


def test_api_key_in_config_file_is_rejected(tmp_path: Path) -> None:
    project = write(tmp_path / "project.yaml", "api_key: sk-should-never-be-here\n")
    with pytest.raises(ConfigError) as exc:
        load_config(user_config=tmp_path / "none.yaml", project_config=project, env={})
    assert "环境变量" in exc.value.message
    # 报错本身也不能把值漏出去
    assert "sk-should-never-be-here" not in exc.value.message


def test_openai_api_key_field_in_config_file_is_rejected(tmp_path: Path) -> None:
    project = write(tmp_path / "project.yaml", "OPENAI_API_KEY: sk-nope\n")
    with pytest.raises(ConfigError):
        load_config(user_config=tmp_path / "none.yaml", project_config=project, env={})


def test_unknown_config_field_lists_valid_options(tmp_path: Path) -> None:
    project = write(tmp_path / "project.yaml", "maxx_retries: 3\n")
    with pytest.raises(ConfigError) as exc:
        load_config(user_config=tmp_path / "none.yaml", project_config=project, env={})
    assert "max_retries" in exc.value.message


def test_non_integer_env_value_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(
            user_config=tmp_path / "none.yaml",
            project_config=None,
            env={"PIXEL_ASSET_MAX_RETRIES": "many"},
        )


def test_api_key_comes_from_env_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PIXEL_ASSET_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert Config.api_key() is None
    assert Config.api_key_env_var() is None

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-value")
    assert Config.api_key_env_var() == "OPENAI_API_KEY"
    assert Config.api_key().get_secret_value() == "sk-test-value"


def test_project_key_var_wins_over_openai_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("PIXEL_ASSET_API_KEY", "sk-project")
    assert Config.api_key_env_var() == "PIXEL_ASSET_API_KEY"


def test_missing_key_error_names_the_env_vars_not_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PIXEL_ASSET_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(MissingApiKeyError) as exc:
        Config().require_api_key()
    assert "OPENAI_API_KEY" in exc.value.message


def test_secret_str_does_not_leak_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret-value")
    key = Config.api_key()
    assert "sk-super-secret-value" not in repr(key)
    assert "sk-super-secret-value" not in str(key)


def test_config_model_has_no_api_key_field() -> None:
    """结构性保证：Config 里根本没有放 Key 的地方，就不可能被序列化出去。"""
    assert "api_key" not in Config.model_fields
    assert "sk-" not in repr(Config())
