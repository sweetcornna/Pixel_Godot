from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"


@pytest.fixture
def examples_dir() -> Path:
    return EXAMPLES


@pytest.fixture
def minimal_request() -> dict[str, Any]:
    """最小合法请求。测试改坏某个字段时以它为基线。"""
    return {
        "schema_version": "1.0",
        "asset_id": "test_01",
        "asset_type": "character",
        "description": "A simple test character with a plain tunic.",
        "style": {
            "perspective": "top_down_3_4",
            "target_size": [32, 32],
            "max_colors": 24,
        },
        "animations": [
            {"name": "walk", "directions": ["down", "left", "right", "up"],
             "frames": 8, "fps": 10, "loop": True}
        ],
        "export": {"targets": ["generic-json", "godot"]},
    }


@pytest.fixture
def no_sleep():
    """把退避 sleep 换成空操作 —— 测试不该真的睡 30 秒。"""
    calls: list[float] = []

    def _sleep(seconds: float) -> None:
        calls.append(seconds)

    _sleep.calls = calls  # type: ignore[attr-defined]
    return _sleep
