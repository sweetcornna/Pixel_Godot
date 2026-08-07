"""MCP 适配层的五道闸门（PLAN §9.3 / ADR-005）。

**"服务能起来"不是判据。** 一个返回 `{"status": "ok"}` 的桩能通过任何
"工具存在"的检查，而 ADR-005 真正要守的是三件更容易悄悄失守的事：
工具数量必须收敛、验证失败必须原样传出、返回不能撑爆上下文。

`mcp` 是可选依赖，没装时整个文件跳过 —— 与 `openai` 同口径。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from pixel_asset_forge import mcp_server
from pixel_asset_forge.mcp_server import MAX_RESULT_CHARS, TOOL_NAMES

mcp = pytest.importorskip("mcp", reason="需要可选依赖 mcp")


@pytest.fixture
def project(tmp_path: Path, examples_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PIXEL_ASSET_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / "mock.yaml").write_text(
        "provider: mock\nmodel: mock-image\noutput_dir: outputs\n"
        "cache_dir: cache\nmax_concurrency: 1\n",
        encoding="utf-8",
    )
    (tmp_path / "grass_field.yaml").write_text(
        (examples_dir / "grass_field.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "knight.yaml").write_text(
        (examples_dir / "knight.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return tmp_path


def config_arg(project: Path) -> str:
    return str(project / "mock.yaml")


# -- 闸门一：恰好 6 个工具 ---------------------------------------------------
#
# ADR-005 的核心论点是"工具数量必须刻意收敛",而这个论点会被一次"顺手加个工具"
# 悄悄推翻 —— 推翻的那一刻没有任何东西会红。所以断言的是**相等**,不是包含。


def test_the_server_exposes_exactly_the_six_tools() -> None:
    exposed = mcp_server.tool_names_from_server()
    assert exposed == set(TOOL_NAMES), (
        f"MCP 工具集变了：多出 {sorted(exposed - set(TOOL_NAMES))}、"
        f"少了 {sorted(set(TOOL_NAMES) - exposed)}。"
        "改这条断言之前请重读 ADR-005：上下文开销、选错概率、顺序错误三份代价。"
    )
    assert len(exposed) == 6


@pytest.mark.parametrize("banned", ["init", "doctor", "plan", "process", "import"])
def test_developer_only_commands_are_not_exposed(banned: str) -> None:
    """这几个是开发者工作流入口，对模型没有语义价值（ADR-005 备选方案 C）。"""
    assert banned not in mcp_server.tool_names_from_server()


def test_every_tool_has_a_description_the_model_can_act_on() -> None:
    """没有描述的工具等于让模型猜 —— 而猜错正是收敛要避免的事。"""
    for name, description in mcp_server.tool_descriptions().items():
        assert description and len(description) > 20, f"{name} 的描述太薄，模型只能猜"


# -- 闸门二：工具必须真的跑流水线 --------------------------------------------


def test_create_character_actually_writes_artifacts(project: Path) -> None:
    """桩也能返回 asset_id —— 所以断言的是**盘上真的有产物**。"""
    result = mcp_server.create_character("knight.yaml", config_arg(project))
    assert result["asset_id"] == "knight_01"
    seed = Path(result["seed_image"])
    assert seed.is_file() and seed.stat().st_size > 0
    assert (project / "outputs" / "knight_01" / "asset-manifest.json").is_file()


def test_export_asset_actually_writes_files(project: Path) -> None:
    from pixel_asset_forge.config import load_config
    from pixel_asset_forge.pipelines.tileset import create_tileset

    config = load_config(project_config=project / "mock.yaml")
    create_tileset(project / "grass_field.yaml", config)
    mcp_server.validate_asset(str(project / "outputs" / "grass_field"), config_arg(project))

    result = mcp_server.export_asset(
        str(project / "outputs" / "grass_field"), ["generic-json"], config_arg(project)
    )
    assert result["files"] > 0
    assert (project / "outputs" / "grass_field" / "exports" / "generic-json").is_dir()


# -- 闸门三：validate_asset 必须如实报失败 -----------------------------------
#
# 全项目唯一不可退让的规则:验证失败时绝不把资产标记为成功(PLAN §9)。
# MCP 是新增的一条返回路径,它同样要守。


def test_validate_reports_failure_faithfully(project: Path) -> None:
    from pixel_asset_forge.config import load_config
    from pixel_asset_forge.pipelines.tileset import create_tileset

    config = load_config(project_config=project / "mock.yaml")
    create_tileset(project / "grass_field.yaml", config)
    asset_dir = project / "outputs" / "grass_field"

    healthy = mcp_server.validate_asset(str(asset_dir), config_arg(project))
    assert healthy["passed"] is True, "前提失效：干净的资产本来该通过"

    # 把一块 tile 换成 16×16 —— 尺寸不符是 fatal。
    manifest = json.loads((asset_dir / "asset-manifest.json").read_text(encoding="utf-8"))
    target = asset_dir / manifest["tileset"]["tiles"]["grass_base"]["image"]
    Image.new("RGBA", (16, 16), (10, 200, 10, 255)).save(target)

    broken = mcp_server.validate_asset(str(asset_dir), config_arg(project))
    assert broken["passed"] is False, "验证失败被吞掉了 —— 这是全项目唯一不可退让的规则"
    assert broken["blocking_total"] >= 1
    assert broken["blocking"], "报了失败却一条阻断项都不列，用户无从下手"


# -- 闸门四：返回体积有上界 --------------------------------------------------
#
# MCP 的返回直接进模型上下文。这与 §8.3 那次 OOM 是同一类错误:没有上界的输出。
# 那次吃的是内存,这次吃的是上下文。


def _size(payload: object) -> int:
    return len(json.dumps(payload, ensure_ascii=False))


def test_results_stay_under_the_context_budget(project: Path) -> None:
    """用**真实的大产物**验：带 24×16 地图的 tileset，Manifest 有几千个 tile id。"""
    from pixel_asset_forge.config import load_config
    from pixel_asset_forge.pipelines.tilemap import create_map
    from pixel_asset_forge.pipelines.tileset import create_tileset

    config = load_config(project_config=project / "mock.yaml")
    create_tileset(project / "grass_field.yaml", config)
    asset_dir = project / "outputs" / "grass_field"
    create_map(asset_dir, name="overworld", width=24, height=16, seed=7)

    validated = mcp_server.validate_asset(str(asset_dir), config_arg(project))
    exported = mcp_server.export_asset(str(asset_dir), ["generic-json"], config_arg(project))
    for name, payload in (("validate_asset", validated), ("export_asset", exported)):
        assert _size(payload) <= MAX_RESULT_CHARS, (
            f"{name} 的返回 {_size(payload)} 字符，超过上界 {MAX_RESULT_CHARS} —— "
            "有人开始往回传产物而不是摘要了"
        )


def test_a_report_full_of_failures_is_still_bounded() -> None:
    """反例：造一份有 200 条阻断项的报告，摘要必须仍然有界。

    只用健康资产验上界没有判别力 —— 健康资产本来就没什么可说的。
    """
    from pixel_asset_forge.models.validation import (
        Check,
        CheckResult,
        ValidationReport,
    )

    report = ValidationReport(asset_id="flooded", thresholds_calibrated=False)
    for index in range(200):
        report.checks.append(
            Check.make(
                "frame_size", f"walk_down#{index}", CheckResult.FAIL,
                message="期望 (96, 96)，实际 [(64, 64)] —— " + "细节" * 40,
            )
        )
    summary = mcp_server._report_summary(report)
    assert summary["passed"] is False
    assert summary["blocking_total"] == 200
    assert len(summary["blocking"]) == 10, "阻断项没有被截断，200 条会塞满上下文"
    assert _size(summary) <= MAX_RESULT_CHARS


# -- 闸门五：Key 不出现在返回里 ----------------------------------------------


def test_errors_do_not_leak_the_api_key(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP 多了一条错误冒泡路径，脱敏同样要覆盖它。"""
    from pixel_asset_forge.errors import PixelAssetError

    secret = "sk-" + "b" * 48
    monkeypatch.setenv("PIXEL_ASSET_API_KEY", secret)

    with pytest.raises(PixelAssetError) as excinfo:
        mcp_server.validate_asset(str(project / "outputs" / "nope"), config_arg(project))
    assert secret not in str(excinfo.value)
    assert secret not in repr(excinfo.value)


# -- 闸门二补全：六个工具都要真的跑过 ----------------------------------------
#
# 起初只覆盖了 create_character 与 export_asset —— 2/6。而恰恰是没覆盖的那个
# create_asset_pack 里藏着一处真 bug：run_asset_pack 是 async 的，同步调用会拿到
# coroutine 对象、然后在 .counts 上炸。**mypy 抓到了它，测试没有** —— 因为那条
# 路径根本没被跑过。闸门写了一半等于没写。


def test_create_asset_pack_actually_runs_the_batch(
    project: Path, examples_dir: Path
) -> None:
    """六个工具里唯一的协程 —— 同步调用会静默拿到 coroutine 对象。"""
    from typer.testing import CliRunner

    from pixel_asset_forge.cli import EXIT_OK, app

    pack = project / "potions.yaml"
    pack.write_text(
        (examples_dir / "potion_pack.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    # pack 执行前必须先 plan --save（指纹闸门），这条前置在 CLI 侧。
    planned = CliRunner().invoke(
        app, ["plan", str(pack), "--save", "--config", config_arg(project)]
    )
    assert planned.exit_code == EXIT_OK, planned.stdout

    result = mcp_server.create_asset_pack(str(pack), config_arg(project))
    assert isinstance(result, dict), "拿到的不是 dict —— 多半是没 await 的 coroutine"
    assert result["assets_total"] >= 1
    assert sum(result["counts"].values()) >= 1
    # 断言盘上真有产物，不是只看返回值
    assert any((project / "outputs").glob("*/asset-manifest.json"))


def test_create_animation_actually_runs(project: Path) -> None:
    mcp_server.create_character("knight.yaml", config_arg(project))
    asset_dir = str(project / "outputs" / "knight_01")

    result = mcp_server.create_animation(
        asset_dir, action="walk", direction="down",
        config_path=config_arg(project), approve_seed=True,
    )
    assert result["frames"] > 0
    frames = list((project / "outputs" / "knight_01" / "frames" / "walk_down").glob("*.png"))
    assert len(frames) == result["frames"]


def test_repair_asset_survives_an_actual_repair(project: Path) -> None:
    """真修一次 —— `repair_asset` 修完还要再验一遍，而那一步以前必崩。

    工具的返回里带着"修完之后过没过"，靠的是 `execute_plan` 后面那次
    `_run_validation`。本地修复把任务留在 `processing` 时，那次验证直接抛
    "没有可验证任务" —— 修是修了，工具却报错收场。
    下面那条只覆盖"没有可修的东西"，照不出这条路径。
    """
    import numpy as np

    mcp_server.create_character("knight.yaml", config_arg(project))
    asset_dir = str(project / "outputs" / "knight_01")
    mcp_server.create_animation(
        asset_dir, action="walk", direction="down",
        config_path=config_arg(project), approve_seed=True,
    )
    for path in sorted((Path(asset_dir) / "frames" / "walk_down").glob("*.png")):
        arr = np.array(Image.open(path).convert("RGBA"))
        arr[0, 0, :3] = (7, 7, 7)  # 透明却带 RGB —— 本地可修
        Image.fromarray(arr, "RGBA").save(path)
    assert mcp_server.validate_asset(asset_dir, config_arg(project))["passed"] is False

    result = mcp_server.repair_asset(asset_dir, config_arg(project))

    assert result["repaired"] is True
    assert result["passed"] is True, "修完那次验证要能跑得起来，并且真的修好了"


def test_repair_asset_runs_and_reports_honestly(project: Path) -> None:
    """没有可修的东西时要如实说'没有'，而不是假装修过。"""
    from pixel_asset_forge.config import load_config
    from pixel_asset_forge.pipelines.tileset import create_tileset

    config = load_config(project_config=project / "mock.yaml")
    create_tileset(project / "grass_field.yaml", config)
    asset_dir = str(project / "outputs" / "grass_field")

    result = mcp_server.repair_asset(asset_dir, config_arg(project))
    assert result["repaired"] is False
    assert "没有可自动修复" in result["reason"]
    assert result["passed"] is True


def test_all_six_tools_are_exercised_by_this_file() -> None:
    """防止本文件再退回"只覆盖两个工具"的状态。

    闸门二的价值全在覆盖率上：漏掉的那个工具正是藏 bug 的地方（实测如此）。
    """
    source = Path(__file__).read_text(encoding="utf-8")
    unexercised = [
        name for name in TOOL_NAMES if f"mcp_server.{name}(" not in source
    ]
    assert not unexercised, f"这些工具没有任何用例真的调用过：{unexercised}"
