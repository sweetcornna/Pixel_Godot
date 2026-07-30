"""Marketplace 与 plugin 骨架自检（Sprint 10.3）。

清单里的路径、SKILL.md 里的相对链接都是**字符串**，改文件名不会有任何编译
或类型错误提醒 —— 只会在用户安装时才发现指到了空气。这套用例就是把这些
字符串变成会失败的断言。

内置了两个第三方项目（`plugins/godot-ai/`、`plugins/godot-prompter/`），
所以这里还要守住 vendor 的义务：许可、来源、改动记录一样都不能少。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"

#: 内置的第三方 plugin —— 它们比自有 plugin 多背几条义务。
VENDORED = ("godot-ai", "godot-prompter")


@pytest.fixture(scope="module")
def marketplace() -> dict:
    return json.loads(MARKETPLACE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def plugin_dirs(marketplace: dict) -> dict[str, Path]:
    return {
        entry["name"]: (ROOT / entry["source"]).resolve()
        for entry in marketplace["plugins"]
    }


# -- marketplace -----------------------------------------------------------


def test_the_marketplace_declares_the_three_pieces(marketplace: dict) -> None:
    """工作站是三块拼图：产资产、动编辑器、供领域知识。少一块就不是工作站。"""
    for field in ("name", "owner", "plugins"):
        assert field in marketplace, f"marketplace.json 缺少 {field}"
    assert {entry["name"] for entry in marketplace["plugins"]} == {
        "pixel-asset-forge", "godot-ai", "godot-prompter",
    }


def test_every_declared_source_exists(plugin_dirs: dict[str, Path]) -> None:
    for name, path in plugin_dirs.items():
        assert path.is_dir(), f"{name} 的 source 指向不存在的目录"
        assert (path / ".claude-plugin" / "plugin.json").is_file(), (
            f"{name} 下没有 plugin.json"
        )


def test_each_plugin_manifest_is_valid(plugin_dirs: dict[str, Path]) -> None:
    for name, path in plugin_dirs.items():
        manifest = json.loads(
            (path / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        assert manifest["name"] == name, f"{name} 的 plugin.json 名字对不上目录"
        assert manifest.get("description"), f"{name} 缺 description"
        assert re.fullmatch(r"\d+\.\d+\.\d+", manifest.get("version", "")), (
            f"{name} 的 version 不是三段式"
        )


def test_each_plugin_stands_alone(plugin_dirs: dict[str, Path]) -> None:
    """装其一不依赖其二 —— 退出门槛之一。

    判据是每个 plugin 目录自带它宣称的东西：skills / agents / addons 至少有一个，
    或者它本身就是个 MCP 声明。
    """
    for name, path in plugin_dirs.items():
        has_content = any(
            (path / sub).is_dir() for sub in ("skills", "agents", "addons")
        ) or (path / ".mcp.json").is_file()
        assert has_content, f"{name} 是个空壳，装了等于没装"


# -- MCP -------------------------------------------------------------------


def test_godot_ai_declares_an_http_mcp(plugin_dirs: dict[str, Path]) -> None:
    """godot-ai 的 MCP 是 **HTTP 服务**，由 Godot 编辑器里的 addon 拉起 ——
    不是能被 spawn 的 stdio 命令。写成 command/args 那种形式会连不上。
    """
    config = json.loads(
        (plugin_dirs["godot-ai"] / ".mcp.json").read_text(encoding="utf-8")
    )
    assert "godot-ai" in config
    entry = config["godot-ai"]
    assert entry.get("type") == "http"
    assert entry.get("url", "").endswith("/mcp")
    assert "command" not in entry, "它不是 stdio 命令"


def test_the_godot_addon_is_vendored(plugin_dirs: dict[str, Path]) -> None:
    """Python MCP server 从上游装，但 Godot addon 必须内置 —— 用户要把它
    复制进自己的 Godot 项目才能拉起 server。
    """
    addon = plugin_dirs["godot-ai"] / "addons" / "godot_ai"
    assert addon.is_dir()
    assert (addon / "plugin.cfg").is_file(), "Godot 认 plugin.cfg，没有它加载不了"


# -- vendor 的义务 ---------------------------------------------------------


@pytest.mark.parametrize("name", VENDORED)
def test_vendored_plugins_keep_their_licence(
    name: str, plugin_dirs: dict[str, Path]
) -> None:
    """MIT 允许复制，**前提是保留许可与版权声明**。这条是法律义务，不是风格。"""
    licence = plugin_dirs[name] / "LICENSE"
    assert licence.is_file(), f"{name} 没有保留 LICENSE"
    text = licence.read_text(encoding="utf-8")
    assert "MIT License" in text
    assert "Copyright" in text


@pytest.mark.parametrize("name", VENDORED)
def test_vendored_plugins_record_their_provenance(
    name: str, plugin_dirs: dict[str, Path]
) -> None:
    """内置就要说清楚"从哪个 commit 来的、改了什么"，否则同步上游时无从下手。"""
    upstream = plugin_dirs[name] / "UPSTREAM.md"
    assert upstream.is_file(), f"{name} 没有 UPSTREAM.md"
    text = upstream.read_text(encoding="utf-8")
    assert re.search(r"\b[0-9a-f]{40}\b", text), "没有记录 fork 的 commit SHA"
    assert "我们改了什么" in text
    assert "同步上游" in text


def test_the_notice_covers_every_vendored_project() -> None:
    notice = (ROOT / "NOTICE.md").read_text(encoding="utf-8")
    for name in VENDORED:
        assert f"plugins/{name}/" in notice, f"NOTICE.md 漏了 {name}"
    assert (ROOT / "LICENSE").is_file(), "NOTICE.md 指向的根 LICENSE 不存在"


# -- Skill 内容 ------------------------------------------------------------


def skill_files(path: Path) -> list[Path]:
    if not (path / "skills").is_dir():
        return []
    return sorted((path / "skills").rglob("SKILL.md"))


def test_every_skill_has_frontmatter(plugin_dirs: dict[str, Path]) -> None:
    """没有 frontmatter 的 SKILL.md 不会被识别成 skill。"""
    for name, path in plugin_dirs.items():
        for skill in skill_files(path):
            text = skill.read_text(encoding="utf-8")
            assert text.startswith("---\n"), f"{name}/{skill.parent.name} 缺 frontmatter"
            front = text.split("---", 2)[1]
            assert re.search(r"^name:\s*\S", front, re.M), f"{name}/{skill.parent.name}"
            assert re.search(r"^description:\s*\S", front, re.M), (
                f"{name}/{skill.parent.name}"
            )


def test_our_skill_references_resolve(plugin_dirs: dict[str, Path]) -> None:
    """SKILL.md 的参考表指向不存在的文件时，Agent 会去读一个空气文件。

    只查自有 plugin —— 上游的内部链接由上游负责，我们一个字都没改。
    """
    skill_dir = plugin_dirs["pixel-asset-forge"] / "skills" / "pixel-asset-forge"
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    for target in re.findall(r"`(references/[\w./-]+)`", text):
        assert (skill_dir / target).is_file(), f"SKILL.md 指向不存在的 {target}"
    for target in re.findall(r"`(docs/[\w./-]+)`", text):
        assert (ROOT / target).exists(), f"SKILL.md 指向不存在的 {target}"

    refs = skill_dir / "references"
    for doc in refs.glob("*.md"):
        for target in re.findall(
            r"\]\(([\w./-]+\.md)\)", doc.read_text(encoding="utf-8")
        ):
            assert (doc.parent / target).is_file(), f"{doc.name} 指向不存在的 {target}"


#: 长得像 OpenAI 风格 API Key 的东西。
#:
#: 不能只搜 ``sk-``：内置的 GodotPrompter 文档里有 "Task-Based"，
#: 里面就含 "sk-B"。这条检查一旦开始误报，下次真泄漏时就没人当回事了。
API_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")


def test_no_api_key_leaked_into_any_plugin(plugin_dirs: dict[str, Path]) -> None:
    """Key 只从环境变量读 —— plugin 是要分发出去的，泄漏后果不可逆。"""
    for target in [MARKETPLACE, *plugin_dirs.values()]:
        files = [target] if target.is_file() else list(target.rglob("*"))
        for path in files:
            if path.is_file() and path.suffix in {".md", ".json", ".yaml", ".yml"}:
                text = path.read_text(encoding="utf-8", errors="ignore")
                found = API_KEY_PATTERN.search(text)
                assert found is None, f"{path} 疑似含 API Key：{found.group()[:12]}…"


def test_the_key_check_actually_catches_a_key(tmp_path: Path) -> None:
    """一个永远通过的泄漏检查没有价值 —— 这里证明它抓得住。"""
    assert API_KEY_PATTERN.search("sk-" + "a" * 32) is not None
    assert API_KEY_PATTERN.search("### Task-Based Patterns") is None
    assert API_KEY_PATTERN.search("risk-free") is None
