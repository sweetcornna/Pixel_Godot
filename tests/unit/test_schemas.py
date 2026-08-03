"""`schemas/*.json` 自身的健康检查（PLAN §9.1）。

`schemas/` 是**对外契约** —— 别的工具、别的语言也要读它，所以它必须自己先是
一份合法的 JSON Schema。这件事此前没有任何检查在管：一个写坏的 schema
（`type` 拼成 `typo`、`$ref` 指向不存在的 `$defs`）只会在**恰好有数据去撞它**时
才暴露，而那可能是几个月以后，在用户那边。

8.2 与 8.3 都实测过这层的价值：Python 那侧的 `Literal` 改齐了、单测也过了，
落盘 JSON 仍然被 `validation-report.schema.json` 的枚举拦下来。
但那是拿数据去撞它 —— 撞不到的那些角落，只能靠这里。
"""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from pixel_asset_forge.schema_registry import SCHEMA_FILES, schema_dir

SCHEMA_NAMES = sorted(SCHEMA_FILES)


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_the_schema_file_is_valid_json(name: str) -> None:
    payload = (schema_dir() / SCHEMA_FILES[name]).read_text(encoding="utf-8")
    json.loads(payload)


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_the_schema_is_itself_a_valid_json_schema(name: str) -> None:
    """`check_schema` 会抓出 `type` 拼错、约束值类型不对这类问题。"""
    schema = json.loads((schema_dir() / SCHEMA_FILES[name]).read_text(encoding="utf-8"))
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:  # pragma: no cover - 出错时要看得见是哪一条
        pytest.fail(f"{name} 不是合法的 JSON Schema：{exc}")


def _refs(node: object) -> list[str]:
    if isinstance(node, dict):
        found = [node["$ref"]] if isinstance(node.get("$ref"), str) else []
        for value in node.values():
            found += _refs(value)
        return found
    if isinstance(node, list):
        return [ref for item in node for ref in _refs(item)]
    return []


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_every_internal_ref_resolves(name: str) -> None:
    """`$ref: "#/$defs/foo"` 指向不存在的 `foo` 时，jsonschema 只在**用到那条分支**
    时才炸 —— 而没有数据走到那条分支的话，它能一直躺在那里。

    8.3 加 `tile_map_entry` 时就是手写 `$ref` 再手写 `$defs`，两处对不上不会有
    任何东西提醒。
    """
    schema = json.loads((schema_dir() / SCHEMA_FILES[name]).read_text(encoding="utf-8"))
    defs = set(schema.get("$defs", {}))
    dangling = sorted(
        {
            ref
            for ref in _refs(schema)
            if ref.startswith("#/$defs/") and ref.removeprefix("#/$defs/") not in defs
        }
    )
    assert not dangling, f"{name} 里这些 $ref 指向不存在的 $defs：{dangling}"


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_every_definition_is_reachable(name: str) -> None:
    """反过来：`$defs` 里躺着没人引用的定义，多半是改名后忘了删的旧版本。

    留着它比删掉更糟 —— 后来者会以为那是现行契约的一部分。
    """
    schema = json.loads((schema_dir() / SCHEMA_FILES[name]).read_text(encoding="utf-8"))
    used = {
        ref.removeprefix("#/$defs/") for ref in _refs(schema) if ref.startswith("#/$defs/")
    }
    orphans = sorted(set(schema.get("$defs", {})) - used)
    assert not orphans, f"{name} 的 $defs 里没人引用：{orphans}"


def test_the_registry_lists_every_schema_file_on_disk() -> None:
    """盘上多一份 schema 而注册表不知道，等于那份契约没人校验。"""
    on_disk = {path.name for path in schema_dir().glob("*.schema.json")}
    assert on_disk == set(SCHEMA_FILES.values())
