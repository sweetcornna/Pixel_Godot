"""terrain 声明的 tile prompt 分支（PLAN §8.5）。"""

from __future__ import annotations

from typing import Any

from pixel_asset_forge.models.request import parse_request
from pixel_asset_forge.prompts.compiler import compile_tile_prompt


def request_data() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "asset_id": "terrain_demo",
        "asset_type": "tileset",
        "description": "A cohesive grass and dirt terrain family for a forest map.",
        "style": {
            "perspective": "top_down",
            "target_size": [32, 32],
            "max_colors": 16,
        },
        "tileset": {
            "tile_size": [32, 32],
            "tiles": [
                {"tile_id": "grass_base", "description": "Dense short green grass."},
                {"tile_id": "dirt_base", "description": "Packed brown forest dirt."},
            ],
        },
        "export": {"targets": ["generic-json"]},
    }


def test_homogeneous_terrain_keeps_the_old_prompt_byte_for_byte() -> None:
    old_request = parse_request(request_data())
    old = compile_tile_prompt(old_request, old_request.tile_list[0]).text

    data = request_data()
    data["tileset"]["tiles"][0]["terrain"] = "grass"
    data["tileset"]["tiles"][1]["terrain"] = "dirt"
    declared_request = parse_request(data)
    declared = compile_tile_prompt(declared_request, declared_request.tile_list[0]).text

    assert declared == old


def test_transition_prompt_names_every_corner_and_keeps_negative_constraints() -> None:
    data = request_data()
    data["tileset"]["tiles"][0]["terrain"] = "grass"
    data["tileset"]["tiles"][1]["terrain"] = "dirt"
    data["tileset"]["tiles"].append(
        {
            "tile_id": "grass_dirt_corner",
            "description": "Grass changing into dirt across one internal boundary.",
            "terrain": {"corners": ["grass", "grass", "dirt", "dirt"]},
        }
    )
    request = parse_request(data)
    prompt = compile_tile_prompt(request, request.tile_list[2]).text

    assert "top-left = grass; top-right = grass" in prompt
    assert "bottom-left = dirt; bottom-right = dirt" in prompt
    assert "must run inside the square" in prompt
    assert "homogeneous base tile" in prompt
    assert "Opposite edges are not required to match" in prompt
    for old_constraint in (
        "No light direction, no gradient, no vignette, no darkened corners or edges",
        "Exclude any border, frame, outline or margin",
        "any single large centred object",
        "hard pixel edges with no anti-aliasing",
    ):
        assert old_constraint in prompt


def test_transition_prompt_uses_the_same_deterministic_base_as_pixel_measurement() -> None:
    data = request_data()
    data["tileset"]["tiles"] = [
        {
            "tile_id": "grass_z_base",
            "description": "Later grass reference with broad yellow flowers.",
            "terrain": "grass",
        },
        {
            "tile_id": "grass_a_base",
            "description": "First grass reference with dense short blades.",
            "terrain": "grass",
        },
        {
            "tile_id": "dirt_base",
            "description": "Packed brown forest dirt.",
            "terrain": "dirt",
        },
        {
            "tile_id": "grass_dirt_corner",
            "description": "Grass changing into dirt across one internal boundary.",
            "terrain": {"corners": ["grass", "grass", "dirt", "dirt"]},
        },
    ]
    request = parse_request(data)

    prompt = compile_tile_prompt(request, request.tile_list[-1]).text

    assert "First grass reference with dense short blades." in prompt
    assert "Later grass reference with broad yellow flowers." not in prompt
