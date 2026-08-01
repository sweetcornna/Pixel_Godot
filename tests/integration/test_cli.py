"""CLI 冒烟测试（PLAN §6.1）。

Sprint 1 交付 ``init`` / ``plan`` / ``doctor``。其余命令必须给出
"排期在哪个 Sprint"的明确出口，而不是一个看起来像 bug 的堆栈。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from PIL import Image
from typer.testing import CliRunner

from pixel_asset_forge.cli import (
    EXIT_ERROR,
    EXIT_INVALID_REQUEST,
    EXIT_NOT_IMPLEMENTED,
    EXIT_OK,
    app,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """把 CLI 关进临时目录，且不让宿主机的 Key 泄漏进测试。"""
    monkeypatch.delenv("PIXEL_ASSET_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_doctor_succeeds_without_an_api_key() -> None:
    """离线命令不该因为没有 Key 就失败 —— 5/9 命令根本不碰 API。"""
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == EXIT_OK
    assert "API Key" in result.stdout


def test_doctor_never_prints_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret-value")
    result = runner.invoke(app, ["doctor"])
    assert "sk-super-secret-value" not in result.stdout
    assert "OPENAI_API_KEY" in result.stdout


def test_init_writes_config_and_directories(isolated_env: Path) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == EXIT_OK
    config = isolated_env / "pixel-asset.yaml"
    assert config.exists()
    assert (isolated_env / "outputs").is_dir()
    # 配置模板绝不能引导用户把 Key 写进文件
    assert "绝不要在这里写 API Key" in config.read_text(encoding="utf-8")
    assert "outputs/" in (isolated_env / ".gitignore").read_text(encoding="utf-8")


def test_init_is_idempotent(isolated_env: Path) -> None:
    runner.invoke(app, ["init"])
    (isolated_env / "pixel-asset.yaml").write_text("model: custom\n", encoding="utf-8")
    result = runner.invoke(app, ["init"])
    assert result.exit_code == EXIT_OK
    assert (isolated_env / "pixel-asset.yaml").read_text(encoding="utf-8") == "model: custom\n"


def test_init_force_overwrites(isolated_env: Path) -> None:
    runner.invoke(app, ["init"])
    (isolated_env / "pixel-asset.yaml").write_text("model: custom\n", encoding="utf-8")
    runner.invoke(app, ["init", "--force"])
    assert "provider: openai" in (isolated_env / "pixel-asset.yaml").read_text(encoding="utf-8")


def test_plan_reports_cost_before_anything_is_generated(examples_dir: Path) -> None:
    """大批量生成之前先跑 plan —— 用户有权在花钱之前看清楚要生成什么。"""
    result = runner.invoke(app, ["plan", str(examples_dir / "knight.yaml")])
    assert result.exit_code == EXIT_OK
    assert "预计 API 调用" in result.stdout
    assert "knight_01:seed" in result.stdout


def test_plan_json_output_is_machine_readable(examples_dir: Path) -> None:
    result = runner.invoke(app, ["plan", str(examples_dir / "slime.yaml"), "--json"])
    assert result.exit_code == EXIT_OK
    payload = json.loads(result.stdout)
    assert payload["estimated_api_calls"] == 7
    assert payload["background"]["color_used"] == "#00FF00"


def test_plan_surfaces_the_background_downgrade(examples_dir: Path) -> None:
    result = runner.invoke(app, ["plan", str(examples_dir / "slime.yaml")])
    assert "#00FF00" in result.stdout


def test_plan_recognizes_potion_pack_and_reports_effective_model(
    examples_dir: Path,
) -> None:
    result = runner.invoke(app, ["plan", str(examples_dir / "potion_pack.yaml"), "--json"])
    assert result.exit_code == EXIT_OK
    payload = json.loads(result.stdout)
    assert payload["pack_type"] == "potion_pack"
    assert payload["estimated_api_calls"] == 3
    assert len(payload["assets"]) == 3


def test_create_asset_pack_requires_saved_plan(
    examples_dir: Path, isolated_env: Path
) -> None:
    config = isolated_env / "mock.yaml"
    config.write_text(
        "provider: mock\nmodel: mock-image\noutput_dir: outputs\ncache_dir: cache\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["create-asset-pack", str(examples_dir / "potion_pack.yaml"), "--config", str(config)],
    )

    assert result.exit_code == EXIT_ERROR
    assert "批量执行前必须先完成离线规划" in result.stderr
    assert "pixel-asset plan" in result.stderr
    assert "--save" in result.stderr
    assert not (isolated_env / "outputs" / "_packs").exists()


def test_create_asset_pack_rejects_stale_saved_plan(
    examples_dir: Path, isolated_env: Path
) -> None:
    config = isolated_env / "mock.yaml"
    config.write_text(
        "provider: mock\nmodel: mock-image\noutput_dir: outputs\ncache_dir: cache\n",
        encoding="utf-8",
    )
    pack_file = isolated_env / "potion_pack.yaml"
    pack_data = yaml.safe_load(
        (examples_dir / "potion_pack.yaml").read_text(encoding="utf-8")
    )
    pack_file.write_text(yaml.safe_dump(pack_data, sort_keys=False), encoding="utf-8")
    planned = runner.invoke(
        app,
        ["plan", str(pack_file), "--config", str(config), "--save"],
    )
    assert planned.exit_code == EXIT_OK
    pack_data["assets"][0]["description"] += " with a gold label"
    pack_file.write_text(yaml.safe_dump(pack_data, sort_keys=False), encoding="utf-8")

    result = runner.invoke(
        app,
        ["create-asset-pack", str(pack_file), "--config", str(config)],
    )

    assert result.exit_code == EXIT_ERROR
    assert "规划指纹与当前请求不一致：health_potion" in result.stderr
    assert "pixel-asset plan" in result.stderr
    assert not (isolated_env / "outputs" / "_packs").exists()


def test_create_pack_and_export_by_asset_id(
    examples_dir: Path, isolated_env: Path
) -> None:
    config = isolated_env / "mock.yaml"
    config.write_text(
        "provider: mock\n"
        "model: mock-image\n"
        "output_dir: outputs\n"
        "cache_dir: cache\n"
        "max_concurrency: 2\n",
        encoding="utf-8",
    )
    planned = runner.invoke(
        app,
        ["plan", str(examples_dir / "potion_pack.yaml"), "--config", str(config), "--save"],
    )
    assert planned.exit_code == EXIT_OK
    created = runner.invoke(
        app,
        [
            "create-asset-pack",
            str(examples_dir / "potion_pack.yaml"),
            "--config",
            str(config),
            "--json",
        ],
    )
    assert created.exit_code == EXIT_OK
    payload = json.loads(created.stdout)
    assert payload["model"] == "mock-image"
    assert payload["counts"]["exported"] == 3

    exported = runner.invoke(
        app,
        ["export", "health_potion", "--config", str(config), "--no-contact-sheet"],
    )
    assert exported.exit_code == EXIT_OK
    assert (isolated_env / "outputs" / "health_potion" / "exports").exists()


def test_export_by_asset_id_includes_default_contact_sheet(
    examples_dir: Path, isolated_env: Path
) -> None:
    config = isolated_env / "mock.yaml"
    config.write_text(
        "provider: mock\n"
        "model: mock-image\n"
        "output_dir: outputs\n"
        "cache_dir: cache\n"
        "max_concurrency: 2\n",
        encoding="utf-8",
    )
    planned = runner.invoke(
        app,
        ["plan", str(examples_dir / "potion_pack.yaml"), "--config", str(config), "--save"],
    )
    assert planned.exit_code == EXIT_OK
    created = runner.invoke(
        app,
        ["create-asset-pack", str(examples_dir / "potion_pack.yaml"), "--config", str(config)],
    )
    assert created.exit_code == EXIT_OK

    exported = runner.invoke(
        app,
        ["export", "health_potion", "--config", str(config), "--target", "godot"],
    )

    assert exported.exit_code == EXIT_OK
    assert "contact-sheet.png" in exported.stdout
    assert (
        isolated_env / "outputs" / "health_potion" / "previews" / "contact-sheet.png"
    ).exists()


def test_plan_save_persists_the_job_table(examples_dir: Path, isolated_env: Path) -> None:
    result = runner.invoke(app, ["plan", str(examples_dir / "knight.yaml"), "--save"])
    assert result.exit_code == EXIT_OK
    table = isolated_env / "outputs" / "knight_01" / "jobs" / "job-table.json"
    assert table.exists()
    # 产物必须自带它的输入
    assert (isolated_env / "outputs" / "knight_01" / "request.yaml").exists()


def test_replanning_after_save_is_idempotent(examples_dir: Path, isolated_env: Path) -> None:
    runner.invoke(app, ["plan", str(examples_dir / "knight.yaml"), "--save"])
    table = isolated_env / "outputs" / "knight_01" / "jobs" / "job-table.json"
    first = json.loads(table.read_text(encoding="utf-8"))
    runner.invoke(app, ["plan", str(examples_dir / "knight.yaml"), "--save"])
    assert json.loads(table.read_text(encoding="utf-8")) == first


def test_replanning_with_changed_model_reports_readable_fingerprint_conflict(
    examples_dir: Path, isolated_env: Path
) -> None:
    config = isolated_env / "mock.yaml"
    config.write_text(
        "provider: mock\nmodel: model-a\noutput_dir: outputs\ncache_dir: cache\n",
        encoding="utf-8",
    )
    first = runner.invoke(
        app,
        [
            "plan",
            str(examples_dir / "potion_pack.yaml"),
            "--config",
            str(config),
            "--save",
        ],
    )
    assert first.exit_code == EXIT_OK
    config.write_text(
        "provider: mock\nmodel: model-b\noutput_dir: outputs\ncache_dir: cache\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "plan",
            str(examples_dir / "potion_pack.yaml"),
            "--config",
            str(config),
            "--save",
        ],
    )

    assert result.exit_code == EXIT_ERROR
    assert "input_fingerprint 冲突" in result.stderr
    assert not isinstance(result.exception, ValueError)


def test_invalid_request_exits_with_field_paths(isolated_env: Path) -> None:
    bad = isolated_env / "bad.yaml"
    bad.write_text(
        "asset_id: bad_01\nasset_type: character\n"
        "description: something short enough\n"
        "style: {perspective: top_down_3_4, target_size: [32, 32], max_colors: 24}\n"
        "animations: [{name: walk, frames: 7, fps: 10}]\n"
        "export: {targets: [godot]}\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["plan", str(bad)])
    assert result.exit_code == EXIT_INVALID_REQUEST
    # 错误走 stderr，正常输出走 stdout —— 管道里 `plan --json | jq` 才不会被污染
    assert "animations.0.frames" in result.stderr


def test_all_nine_commands_are_implemented() -> None:
    """Sprint 6 之后 9 个命令全部落地，不再有 NotImplemented 出口。"""

    for argv in (
        ["export", "outputs/nope"],
        ["validate", "outputs/nope"],
        ["repair", "outputs/nope"],
        ["process", "outputs/nope"],
    ):
        result = runner.invoke(app, argv)
        assert result.exit_code != EXIT_NOT_IMPLEMENTED, argv


def test_generation_commands_fail_without_an_asset(isolated_env: Path) -> None:
    """已实现但资产不存在时，要给出可操作的错误，而不是 NotImplemented。"""
    result = runner.invoke(
        app, ["create-animation", "--asset", "nope_01", "--action", "walk"]
    )
    assert result.exit_code == EXIT_ERROR
    assert "create-character" in result.stderr


def test_help_lists_all_nine_commands() -> None:
    result = runner.invoke(app, ["--help"])
    for command in (
        "init", "doctor", "plan", "create-character", "create-animation",
        "process", "validate", "repair", "export",
    ):
        assert command in result.stdout


# -- 导入入口消歧（Sprint 6.8.0）-------------------------------------------


def test_import_refuses_to_guess_the_intent(
    tmp_path: Path, examples_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一批文件对应两种意图，光看文件数量分不出来，猜错代价还不对称。

    这与 pose_sequence 对未知动作抛错而不是退回泛泛描述是同一条原则 ——
    静默选一条路等于把已知失败模式请回来。
    """
    monkeypatch.chdir(tmp_path)
    request = tmp_path / "knight.yaml"
    request.write_text((examples_dir / "knight.yaml").read_text(encoding="utf-8"),
                       encoding="utf-8")
    art = tmp_path / "art.png"
    Image.new("RGBA", (64, 64), (139, 90, 43, 255)).save(art)

    result = runner.invoke(app, ["import", str(request), str(art)])
    assert result.exit_code == EXIT_ERROR
    # 报错必须把两条路各自的产出说清楚，否则用户不知道该选哪个
    assert "--as seed" in result.stdout
    assert "--as keyframes" in result.stdout
    assert "生成整套动作" in result.stdout
    assert "补出中间帧" in result.stdout


def test_import_rejects_an_unknown_intent(
    tmp_path: Path, examples_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    request = tmp_path / "knight.yaml"
    request.write_text((examples_dir / "knight.yaml").read_text(encoding="utf-8"),
                       encoding="utf-8")
    art = tmp_path / "art.png"
    Image.new("RGBA", (64, 64), (139, 90, 43, 255)).save(art)

    result = runner.invoke(app, ["import", str(request), str(art), "--as", "guess"])
    assert result.exit_code == EXIT_ERROR
