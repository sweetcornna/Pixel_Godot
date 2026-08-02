"""tileset 的请求契约与规划。

`tileset` 曾经是个**悬空类型**：它在 `AssetType` 与两份 schema 里各占一格，
却没有任何执行路径 —— `create-asset` 拒收它，而 `plan` 照常给它算预算并提示
"下一步 create-character"。这个文件把新契约的每一条都钉死。
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from pixel_asset_forge.errors import RequestValidationError
from pixel_asset_forge.models.job import JobKind, JobStatus
from pixel_asset_forge.models.request import parse_request
from pixel_asset_forge.planning.planner import plan_request


def _tileset_data() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "asset_id": "grass_field",
        "asset_type": "tileset",
        "description": "A cohesive top-down grass and dirt ground tile family.",
        "style": {
            "perspective": "top_down",
            "target_size": [32, 32],
            "max_colors": 16,
        },
        "tileset": {
            "tile_size": [32, 32],
            "tiles": [
                {
                    "tile_id": "grass_base",
                    "description": "Dense short grass with scattered small stones.",
                },
                {
                    "tile_id": "dirt_path",
                    "description": "Packed brown dirt with fine gravel speckles.",
                },
            ],
        },
        "export": {"targets": ["generic-json", "godot"]},
    }


def test_tileset_request_parses() -> None:
    request = parse_request(_tileset_data())
    assert request.asset_type == "tileset"
    assert request.tileset is not None
    assert request.tileset.tile_size == (32, 32)
    assert [tile.tile_id for tile in request.tile_list] == ["grass_base", "dirt_path"]


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        # tile 满幅不透明，去背景那一步根本不会执行；留着这个字段只会让人
        # 以为发生过。
        ("写了 background", lambda d: d.update(background={"mode": "chroma_key"})),
        (
            "写了 animations",
            lambda d: d.update(animations=[{"name": "walk", "frames": 8, "fps": 10}]),
        ),
        ("缺 tileset 块", lambda d: d.pop("tileset")),
        ("tiles 为空", lambda d: d["tileset"].update(tiles=[])),
        ("tile_size 不是逻辑档位", lambda d: d["tileset"].update(tile_size=[30, 30])),
    ],
)
def test_tileset_contract_rejections(label: str, mutate) -> None:
    data = _tileset_data()
    mutate(data)
    with pytest.raises(RequestValidationError):
        parse_request(data)


def test_duplicate_tile_id_is_rejected() -> None:
    data = _tileset_data()
    data["tileset"]["tiles"].append(copy.deepcopy(data["tileset"]["tiles"][0]))
    with pytest.raises(RequestValidationError):
        parse_request(data)


def test_non_tileset_asset_cannot_carry_a_tileset_block() -> None:
    data = _tileset_data()
    data["asset_type"] = "pickup"
    with pytest.raises(RequestValidationError):
        parse_request(data)


# -- 规划 -----------------------------------------------------------------


def test_plan_creates_one_billable_job_per_tile() -> None:
    result = plan_request(
        parse_request(_tileset_data()), provider="mock", model="mock-image"
    )
    jobs = list(result.jobs)

    assert {job.kind for job in jobs} == {JobKind.TILE}
    assert {job.action for job in jobs} == {"grass_base", "dirt_path"}
    assert all(job.status is JobStatus.PLANNED for job in jobs)
    # 每块 tile 各要一次调用 —— 报 0 次会让用户以为这条链不花钱。
    assert result.estimated_api_calls == 2


def test_tile_job_ids_are_deterministic_and_distinct() -> None:
    data = _tileset_data()
    first = plan_request(parse_request(data), provider="mock", model="mock-image")
    second = plan_request(parse_request(data), provider="mock", model="mock-image")

    ids = sorted(job.id for job in first.jobs)
    assert ids == sorted(job.id for job in second.jobs)
    assert len(set(ids)) == 2
    assert ids == ["grass_field:tile:dirt_path", "grass_field:tile:grass_base"]


def test_replanning_keeps_finished_tiles() -> None:
    """断点续跑的前提：完成过的 tile 不被打回 planned。"""
    request = parse_request(_tileset_data())
    table = plan_request(request, provider="mock", model="mock-image").jobs
    done = next(iter(table))
    done.status = JobStatus.EXPORTED

    merged = plan_request(request, existing=table, provider="mock", model="mock-image")
    statuses = {job.id: job.status for job in merged.jobs}
    assert statuses[done.id] is JobStatus.EXPORTED
    assert merged.estimated_api_calls == 1
