"""Skill 文档与 CLI 的防漂移闸门（PLAN §9.2）。

文档漂移是典型的**静默失败**：没人会因为文档过时而看到红叉，
要等用户照着敲一条不存在的命令才发现。实测过 —— `SKILL.md` 与三份 references
是 Sprint 7 时代写的，Sprint 8 落地的三个纵切一个都没进去，
`create-map` 这条整命令在文档里完全不存在。

补完一次不解决问题，Sprint 10 再落几切它会再漂一次。所以有了这个文件。

**只保证"文档提到的东西真的存在"，不保证"存在的东西文档都提到"。** 反向也强制
的话，`init` / `process` / `repair` 这些**刻意不进 Skill** 的命令会被迫写进去，
而 §6.2 的判断正是"不要向模型暴露几十个工具"——收敛是 Skill 层的职责，
覆盖率不是它的指标。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import typer

from pixel_asset_forge.cli import app
from pixel_asset_forge.models.validation import ALL_CHECK_IDS
from pixel_asset_forge.schema_registry import SCHEMA_FILES, schema_dir

SKILL_DIR = Path(__file__).parent.parent.parent / "plugins" / "pixel-asset-forge" / (
    "skills"
) / "pixel-asset-forge"
SKILL_MD = SKILL_DIR / "SKILL.md"
REFERENCES = SKILL_DIR / "references"


def skill_docs() -> list[Path]:
    return sorted(SKILL_DIR.rglob("*.md"))


def doc_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in skill_docs())


# -- CLI 真实形态 -----------------------------------------------------------


def cli_commands() -> dict[str, set[str]]:
    """命令名 → 它接受的长选项集合。直接问 typer，不是抄一份。"""
    out: dict[str, set[str]] = {}
    for command in app.registered_commands:
        name = command.name or (command.callback.__name__.replace("_", "-"))
        click_command = typer.main.get_command_from_info(
            command, pretty_exceptions_short=False, rich_markup_mode="rich"
        )
        # secondary_opts 不能漏：布尔开关声明成 `--loop/--one-shot` 时，
        # 反面那半存在 secondary_opts 里。只读 opts 会把 `--one-shot` 误报成
        # "不存在的选项" —— 实测被这条咬过一次。
        out[name] = {
            option
            for param in click_command.params
            for option in (*param.opts, *getattr(param, "secondary_opts", ()))
            if option.startswith("--")
        }
    return out


def test_the_skill_directory_is_where_we_think_it_is() -> None:
    """路径写死了，搬家时要当场发现，而不是让下面每条检查静默变成空集。"""
    assert SKILL_MD.is_file()
    assert REFERENCES.is_dir()
    assert skill_docs(), "一份 Skill 文档都没扫到 —— 检查全成了空转"


# -- 1. 文档提到的命令必须真的存在 -------------------------------------------


def test_every_command_named_in_the_docs_exists() -> None:
    named = set(re.findall(r"pixel-asset\s+([a-z][a-z0-9-]*)", doc_text()))
    unknown = sorted(named - set(cli_commands()))
    assert not unknown, (
        f"Skill 文档里这些命令 CLI 没有：{unknown}。"
        "用户会照着敲，然后拿到 'No such command'。"
    )


# -- 2. 文档提到的 flag 必须真的是那条命令的选项 ------------------------------


def _flags_by_command() -> list[tuple[str, str, str]]:
    """逐行归属：同一行里既有命令又有 flag 时，把 flag 记在那条命令上。

    归属不到命令的 flag（单独成行、散文里提到的）退回"必须是某条命令的选项"——
    弱一些，但仍抓得住拼错和已删除的选项。这条限制如实写在这里，不假装它更强。
    """
    pairs: list[tuple[str, str, str]] = []
    for path in skill_docs():
        for line in path.read_text(encoding="utf-8").splitlines():
            flags = re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", line)
            if not flags:
                continue
            match = re.search(r"pixel-asset\s+([a-z][a-z0-9-]*)", line)
            for flag in flags:
                pairs.append((path.name, match.group(1) if match else "", flag))
    return pairs


def test_every_flag_named_in_the_docs_belongs_to_a_real_command() -> None:
    commands = cli_commands()
    every_flag = {flag for options in commands.values() for flag in options}
    problems: list[str] = []
    for doc, command, flag in _flags_by_command():
        if command and command in commands:
            if flag not in commands[command]:
                problems.append(f"{doc}: `{command}` 没有 {flag} 这个选项")
        elif flag not in every_flag:
            problems.append(f"{doc}: 没有任何命令接受 {flag}")
    assert not problems, "Skill 文档里的选项对不上 CLI：\n" + "\n".join(sorted(set(problems)))


# -- 3. 文档点名的验证检查项必须存在 -----------------------------------------
#
# 直接"所有下划线词都得是 CheckId"会误伤一片：Godot API 名（sprite_frames）、
# pack 类型（potion_pack）、函数名（layout_for_frames）都长这样。
#
# 所以用**快照式白名单**把非检查项的词固定下来：某个 CheckId 一旦在代码里被改名，
# 文档里那个词就会从"是 CheckId"掉进"两者都不是"，这条检查当场失败。
# 新增一个非检查项的词要在这里加一行 —— 那点成本换的是这条检查的判别力。

NOT_CHECK_IDS = frozenset(
    {
        # Godot / 引擎侧的名字
        "animation_finished", "ext_resource", "load_steps", "sprite_frames",
        # 请求与产物里的枚举值
        "awaiting_approval", "environment_object", "fixed_top_left", "top_down_3_4",
        # pack 类型
        "combat_bundle", "environment_pack", "potion_pack", "spell_bundle",
        "weapon_pack",
        # 代码里的函数名
        "compile_animation_prompt", "compile_seed_prompt", "layout_for_frames",
        "resolve_key_color",
    }
)


def _schema_fields() -> set[str]:
    names: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    names.update(value)
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for filename in SCHEMA_FILES.values():
        walk(json.loads((schema_dir() / filename).read_text(encoding="utf-8")))
    return names


def test_every_snake_case_token_is_accounted_for() -> None:
    tokens = set(re.findall(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`", doc_text()))
    unexplained = sorted(
        tokens - set(ALL_CHECK_IDS) - _schema_fields() - NOT_CHECK_IDS
    )
    assert not unexplained, (
        f"这些词既不是检查项、也不是 schema 字段、也不在白名单里：{unexplained}。"
        "如果是检查项被改名了，改文档；如果是新的非检查项词，加进 NOT_CHECK_IDS。"
    )


def test_the_allowlist_has_not_rotted() -> None:
    """白名单里躺着文档已经不再提的词，说明它该被清掉。

    留着不会报错，但会**削弱上一条检查**：一个词一旦进了白名单就永远不被追问，
    而它可能正是某个被改名的检查项的旧名字。
    """
    tokens = set(re.findall(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`", doc_text()))
    stale = sorted(NOT_CHECK_IDS - tokens)
    assert not stale, f"NOT_CHECK_IDS 里这些词文档已经不提了，删掉：{stale}"


# -- 4. references 双向对齐 --------------------------------------------------


def test_every_referenced_file_exists() -> None:
    named = set(re.findall(r"references/([a-z0-9-]+\.md)", doc_text()))
    missing = sorted(name for name in named if not (REFERENCES / name).is_file())
    assert not missing, f"SKILL.md 指向了不存在的参考文档：{missing}"


def test_every_reference_file_is_pointed_at() -> None:
    """没人指向的参考文档等于不存在，而它还会让后来者以为那是现行契约。"""
    named = set(re.findall(r"references/([a-z0-9-]+\.md)", doc_text()))
    orphans = sorted(
        path.name for path in REFERENCES.glob("*.md") if path.name not in named
    )
    assert not orphans, f"references/ 下没人引用的文档：{orphans}"


# -- Sprint 8 的三切必须在文档里有位置 ---------------------------------------


@pytest.mark.parametrize(
    ("what", "needle"),
    [
        ("8.2 邻接表", "adjacency"),
        ("8.3 地图生成", "create-map"),
        ("8.4 Tiled 导出", "tiled"),
    ],
)
def test_sprint_8_features_reached_the_skill(what: str, needle: str) -> None:
    """这三条是 9.2 的由来：文档停在 8.1，而 CLI 已经走到 8.4。

    点名而不是泛泛地要求"文档要更新"—— 泛泛的要求没法失败。
    """
    assert needle in doc_text(), f"{what} 没有进 Skill 文档（找不到 {needle!r}）"
