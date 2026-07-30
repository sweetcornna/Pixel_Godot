"""Provider 抽象与 Mock Provider（ADR-002）。

Mock 不是桩而是一等公民：合成图必须真的能驱动下游处理逻辑 ——
真的画出 N 个格子、真的有键控色背景、姿势不跨格线。
一张纯色图能通过的验证器等于没有验证器。

同时验证重试策略的边界：**只有瞬态错误才重试**。
反复重试一个非法请求只会烧配额。
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from pixel_asset_forge.errors import (
    InvalidRequestError,
    ModerationBlockedError,
    RetryLimitExceededError,
)
from pixel_asset_forge.planning import grid_for_frames, seed_layout
from pixel_asset_forge.providers import MockImageProvider, ReferenceImage, RetryPolicy

GRID_PROMPT = (
    "walk cycle for a knight, arranged in a 4x2 grid, exactly 8 distinct poses, "
    "frames ordered left to right, top to bottom, at least 8% margin around each pose, "
    "solid #FF00FF background"
)


def open_image(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def background_mask(image: Image.Image, color: tuple[int, int, int]) -> np.ndarray:
    arr = np.array(image.convert("RGB"))
    return np.all(arr == np.array(color, dtype=arr.dtype), axis=-1)


def test_generate_returns_requested_size() -> None:
    layout = grid_for_frames(8)
    result = MockImageProvider().generate(GRID_PROMPT, size=layout.size)
    assert result.requested_size == layout.size
    assert open_image(result.image).size == layout.size


def test_generation_is_deterministic() -> None:
    """离线迭代的前提：同一输入必然产出同一字节，改动才可判定有效性。"""
    layout = grid_for_frames(8)
    a = MockImageProvider().generate(GRID_PROMPT, size=layout.size)
    b = MockImageProvider().generate(GRID_PROMPT, size=layout.size)
    assert a.image == b.image
    assert a.prompt_hash == b.prompt_hash


def test_every_cell_actually_contains_a_pose() -> None:
    layout = grid_for_frames(8)
    image = open_image(MockImageProvider().generate(GRID_PROMPT, size=layout.size).image)
    bg = background_mask(image, (255, 0, 255))
    for index in range(layout.frames):
        left, top, right, bottom = layout.cell_box(index)
        assert not bg[top:bottom, left:right].all(), f"第 {index} 格是空的"


def test_poses_do_not_cross_cell_boundaries() -> None:
    """姿势跨格会被切帧直接切断肢体，且本地无法修复（PLAN §2.3.2）。"""
    layout = grid_for_frames(8)
    image = open_image(MockImageProvider().generate(GRID_PROMPT, size=layout.size).image)
    bg = background_mask(image, (255, 0, 255))

    for col in range(1, layout.cols):
        x = col * layout.cell[0]
        assert bg[:, x - 1 : x + 1].all(), f"第 {col} 条竖格线上有内容"
    for row in range(1, layout.rows):
        y = row * layout.cell[1]
        assert bg[y - 1 : y + 1, :].all(), f"第 {row} 条横格线上有内容"


def test_frames_are_all_distinct() -> None:
    """完全重复帧是中等严重度失败项 —— Mock 数据不该自带这个毛病。"""
    layout = grid_for_frames(8)
    image = open_image(MockImageProvider().generate(GRID_PROMPT, size=layout.size).image)
    crops = {image.crop(layout.cell_box(i)).tobytes() for i in range(layout.frames)}
    assert len(crops) == layout.frames


def test_key_color_follows_the_prompt() -> None:
    """键控色不是硬编码的 —— Slime 用例会把它降级到纯绿（ADR-004）。"""
    layout = grid_for_frames(4)
    prompt = GRID_PROMPT.replace("#FF00FF", "#00FF00").replace("4x2", "2x2").replace(
        "exactly 8", "exactly 4"
    )
    image = open_image(MockImageProvider().generate(prompt, size=layout.size).image)
    assert background_mask(image, (0, 255, 0)).any()
    assert not background_mask(image, (255, 0, 255)).any()


def test_seed_size_renders_a_single_figure_not_a_2x2_grid() -> None:
    """1024×1024 是种子图，不是 2×2 网格。

    判据取"中线上有没有内容"：单幅居中立绘必然跨过中线，
    而 2×2 网格的每个姿势都留了边距，中线必然干净。
    """
    layout = seed_layout()
    image = open_image(
        MockImageProvider().generate("canonical seed, solid #FF00FF background",
                                     size=layout.size).image
    )
    bg = background_mask(image, (255, 0, 255))
    assert not bg[:, 511:513].all(), "竖中线干净，说明被当成了 2×2 网格"
    assert not bg[511:513, :].all(), "横中线干净，说明被当成了 2×2 网格"


def test_edit_uses_references_in_the_hash() -> None:
    """换了 seed 就该得到不同的图，否则缓存语义是假的。"""
    layout = grid_for_frames(4)
    provider = MockImageProvider()
    base = b"blank canvas"
    a = provider.edit(GRID_PROMPT, base_image=base, size=layout.size,
                      references=[ReferenceImage("seed", b"seed-a")])
    b = provider.edit(GRID_PROMPT, base_image=base, size=layout.size,
                      references=[ReferenceImage("seed", b"seed-b")])
    assert a.prompt_hash != b.prompt_hash
    assert a.image != b.image


def test_result_carries_request_id_and_summary() -> None:
    """失败溯源的前提（ADR-002）。"""
    result = MockImageProvider().generate(GRID_PROMPT, size=grid_for_frames(8).size)
    assert result.request_id
    assert result.request_summary["operation"] == "generate"
    entry = result.log_entry()
    assert entry["prompt_hash"] == result.prompt_hash
    # 日志里不得出现 prompt 原文
    assert "walk cycle" not in str(entry)


def test_illegal_size_is_an_invalid_request_not_a_retryable_error() -> None:
    with pytest.raises(InvalidRequestError):
        MockImageProvider().generate(GRID_PROMPT, size=(1000, 1024))


# -- 重试策略 -------------------------------------------------------------


def test_transient_errors_are_retried(no_sleep) -> None:
    provider = MockImageProvider(retry_policy=RetryPolicy(max_retries=3, base_delay=0.01))
    provider.fail_times = 2
    result = provider.generate(GRID_PROMPT, size=grid_for_frames(4).size, sleep=no_sleep)
    assert result.attempts == 3
    assert len(no_sleep.calls) == 2


def test_backoff_is_exponential(no_sleep) -> None:
    provider = MockImageProvider(retry_policy=RetryPolicy(max_retries=4, base_delay=1.0))
    provider.fail_times = 3
    provider.generate(GRID_PROMPT, size=grid_for_frames(4).size, sleep=no_sleep)
    assert no_sleep.calls == [1.0, 2.0, 4.0]


def test_retry_limit_becomes_a_terminal_failure(no_sleep) -> None:
    provider = MockImageProvider(retry_policy=RetryPolicy(max_retries=2, base_delay=0.01))
    provider.fail_times = 99
    with pytest.raises(RetryLimitExceededError):
        provider.generate(GRID_PROMPT, size=grid_for_frames(4).size, sleep=no_sleep)


def test_moderation_block_is_never_retried(no_sleep) -> None:
    """永久错误重试多少次都还是永久错误 —— 只会白白烧配额。"""
    provider = MockImageProvider(retry_policy=RetryPolicy(max_retries=5, base_delay=0.01))
    with pytest.raises(ModerationBlockedError):
        provider.generate("a gore-filled battlefield", size=grid_for_frames(4).size,
                          sleep=no_sleep)
    assert no_sleep.calls == []


def test_retry_policy_delay_is_capped() -> None:
    policy = RetryPolicy(base_delay=1.0, multiplier=2.0, max_delay=10.0)
    assert policy.delay_for(1) == 1.0
    assert policy.delay_for(10) == 10.0
