"""Potion pack 输入契约与展开。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from pixel_asset_forge.errors import RequestValidationError
from pixel_asset_forge.models import input_fingerprint, load_pack, parse_pack
from pixel_asset_forge.schema_registry import load_schema


def pack_data() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "pack_type": "potion_pack",
        "pack_id": "starter_potions",
        "shared": {
            "style": {
                "perspective": "top_down_3_4",
                "target_size": [32, 32],
                "max_colors": 4,
            },
            "background": {"mode": "chroma_key", "color": "#FF00FF"},
            "export": {"targets": ["generic-json"]},
            "palette": {
                "name": "potions",
                "colors": ["#211A2C", "#EEE8D5", "#D94B4B", "#3978C5"],
            },
        },
        "assets": [
            {"asset_id": "health_potion", "description": "A small red health potion bottle."},
            {"asset_id": "mana_potion", "description": "A small blue mana potion bottle."},
        ],
    }


def test_example_expands_to_static_pickup_requests(examples_dir: Path) -> None:
    pack = load_pack(examples_dir / "potion_pack.yaml")
    requests = pack.expand_requests()

    assert pack.pack_type == "potion_pack"
    assert [request.asset_id for request in requests] == [
        "health_potion",
        "mana_potion",
        "stamina_potion",
    ]
    assert all(request.asset_type == "pickup" for request in requests)
    assert all(request.animation_list() == () for request in requests)
    assert all(request.schema_version == "1.1" for request in requests)
    assert all(request.style.palette_colors == pack.shared.palette.colors for request in requests)
    assert all(request.style.palette_preset == "starter_potions" for request in requests)


def test_duplicate_asset_id_is_rejected() -> None:
    data = pack_data()
    data["assets"].append(copy.deepcopy(data["assets"][0]))
    with pytest.raises(RequestValidationError, match="asset_id 重复"):
        parse_pack(data)


@pytest.mark.parametrize(
    "colors",
    [
        [],
        ["not-a-color"],
        ["#000000", "#111111", "#222222", "#333333", "#444444"],
    ],
)
def test_palette_must_be_nonempty_hex_and_within_max_colors(colors: list[str]) -> None:
    data = pack_data()
    data["shared"]["palette"]["colors"] = colors
    with pytest.raises(RequestValidationError):
        parse_pack(data)


def test_all_key_colors_conflicting_with_palette_is_rejected_during_pack_parse() -> None:
    data = pack_data()
    data["shared"]["background"] = {
        "mode": "chroma_key",
        "color": "#211A2C",
        "fallback_colors": ["#EEE8D5", "#D94B4B"],
    }
    with pytest.raises(RequestValidationError, match="全部候选键控色"):
        parse_pack(data)


def test_asset_entries_reject_extra_fields() -> None:
    data = pack_data()
    data["assets"][0]["provider"] = "openai"
    with pytest.raises(RequestValidationError) as exc:
        parse_pack(data)
    assert any(error["path"] == "assets.0" for error in exc.value.errors)


def test_pack_schema_has_no_provider_or_model_contract() -> None:
    schema_text = json.dumps(load_schema("asset-pack"), sort_keys=True)
    assert '"provider"' not in schema_text
    assert '"model"' not in schema_text


def test_input_fingerprint_is_stable_and_backend_sensitive() -> None:
    request = parse_pack(pack_data()).expand_requests()[0]
    first = input_fingerprint(request, "openai", "gpt-image-2")
    second = input_fingerprint(request, "openai", "gpt-image-2")

    assert first == second
    assert len(first) == 64
    assert input_fingerprint(request, "openai", "other-model") != first
