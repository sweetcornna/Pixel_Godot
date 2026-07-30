"""跨动作缩放一致性（Sprint 6）。

没有这个基准，每个动作各自填满画布 —— 同一个角色在 idle 与 death 之间会变大变小。
实测：把源图里的角色整体缩到 60% 模拟蹲伏，输出后的内容高度与全高走路**完全一样**。
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from pixel_asset_forge.planning import grid_for_frames
from pixel_asset_forge.processing import ProcessOptions, derive_profile, process_grid
from pixel_asset_forge.processing.scale_profile import (
    ScaleProfile,
    clamp_warning,
    scale_for,
)
from pixel_asset_forge.validation.metrics import content_box

KEY_BG = (242, 4, 234)


def grid_with_subject(shrink: float = 1.0) -> np.ndarray:
    """8 帧网格；``shrink`` 控制角色在格子里占多大。"""
    layout = grid_for_frames(8)
    size = (1774, 887)
    width, height = size
    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[:, :] = KEY_BG

    for index in range(8):
        x0, y0, x1, y1 = layout.cell_box(index, size)
        cw, ch = x1 - x0, y1 - y0
        base_w, base_h = int(cw * 0.5), int(ch * 0.7)
        w, h = max(4, int(base_w * shrink)), max(4, int(base_h * shrink))
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        img[cy - h // 2 : cy + h // 2, cx - w // 2 : cx + w // 2] = (139, 90, 43)
        # 一点帧间变化，避免触发"完全重复帧"
        img[cy - h // 2 : cy - h // 2 + 4, cx - w // 2 + index : cx - w // 2 + index + 4] = (
            222, 176, 132
        )
    return img


def output_height(result) -> int:  # type: ignore[no-untyped-def]
    box = content_box(result.frames[0])
    return box[3] - box[1]


# -- 缺陷本身 --------------------------------------------------------------


def test_without_a_profile_every_action_fills_the_canvas() -> None:
    """这是被修复的缺陷：源图里差 40% 体型，输出后一样高。"""
    layout = grid_for_frames(8)
    full = process_grid(grid_with_subject(1.0), layout, ProcessOptions(target_size=(32, 32)))
    small = process_grid(grid_with_subject(0.6), layout, ProcessOptions(target_size=(32, 32)))
    assert abs(output_height(full) - output_height(small)) <= 1


def test_a_shared_profile_preserves_real_size_differences() -> None:
    """蹲伏该是真实的姿势变化，不该被归一化回参考高度。"""
    layout = grid_for_frames(8)
    reference = process_grid(
        grid_with_subject(1.0), layout, ProcessOptions(target_size=(32, 32))
    )
    profile = derive_profile(
        "walk_down",
        content_height=reference.content_source_height,
        cell_height=reference.source_cell_height,
        canvas_height=32,
        output_height=reference.output_content_height,
    )

    crouch = process_grid(
        grid_with_subject(0.6),
        layout,
        ProcessOptions(target_size=(32, 32), scale_profile=profile),
    )
    assert output_height(crouch) < output_height(reference) * 0.8


def test_the_reference_action_still_fills_the_canvas() -> None:
    result = process_grid(
        grid_with_subject(1.0), grid_for_frames(8), ProcessOptions(target_size=(32, 32))
    )
    assert output_height(result) >= 28


# -- 基准本身 --------------------------------------------------------------


def test_denominator_is_the_source_cell_not_the_viewport() -> None:
    """分母必须与 sprite 画多大无关。

    用抽帧后的视口做分母时比值恒等于 1，基准退化成"各自填满画布"，等于没做 ——
    这个 bug 真实发生过。
    """
    layout = grid_for_frames(8)
    big = process_grid(grid_with_subject(1.0), layout)
    small = process_grid(grid_with_subject(0.6), layout)

    big_ratio = big.content_source_height / big.source_cell_height
    small_ratio = small.content_source_height / small.source_cell_height
    assert small_ratio < big_ratio * 0.8, "subject_ratio 没有反映体型差异"


def test_profile_round_trips() -> None:
    profile = ScaleProfile(reference="walk_down", subject_ratio=0.8, canvas_fraction=0.95)
    assert ScaleProfile.from_dict(profile.to_dict()) == profile  # type: ignore[arg-type]


def test_scale_without_profile_fits_the_canvas() -> None:
    scale = scale_for(None, content_size=(100, 200), cell_height=400, canvas=(32, 32))
    assert scale == pytest.approx(32 / 200)


def test_scale_with_profile_follows_the_baseline() -> None:
    profile = ScaleProfile(reference="ref", subject_ratio=0.8, canvas_fraction=1.0)
    # 本动作只有参考动作一半高 → 输出也该只有一半
    scale = scale_for(profile, content_size=(50, 100), cell_height=250, canvas=(32, 32))
    assert scale * 100 == pytest.approx(32 * 0.5, abs=0.5)


def test_oversized_action_is_clamped_and_flagged() -> None:
    """比参考动作大时缩回画布内并告警 —— 残缺帧比略小的帧严重得多。"""
    profile = ScaleProfile(reference="idle_down", subject_ratio=0.4, canvas_fraction=1.0)
    # 本动作在格子里占 0.8，是参考的两倍 → 按基准该放大到画布外
    scale = scale_for(profile, content_size=(20, 40), cell_height=50, canvas=(32, 32))
    assert scale * 40 <= 32, "缩放没有被钳制在画布内"
    assert clamp_warning("attack_down", 1.6, 0.8) is not None
    assert clamp_warning("walk_down", 0.8, 0.8) is None


def test_derive_rejects_degenerate_input() -> None:
    with pytest.raises(ValueError):
        derive_profile("x", content_height=10, cell_height=0, canvas_height=32, output_height=10)


# -- 端到端 ---------------------------------------------------------------


def test_profile_survives_the_manifest(tmp_path) -> None:
    from pixel_asset_forge.models.manifest import (
        AssetManifest,
        BackgroundInfo,
        CanvasInfo,
        PaletteInfo,
        ProviderInfo,
        ScaleProfileInfo,
    )

    manifest = AssetManifest(
        asset_id="knight_01",
        asset_type="character",
        provider=ProviderInfo(name="mock", model="mock-image"),
        canvas=CanvasInfo(width=32, height=32),
        background=BackgroundInfo(
            mode="chroma_key", color_requested="#FF00FF",
            color_used="#FF00FF", fallback_stage="tolerant_key",
        ),
        palette=PaletteInfo(max_colors=24, colors=[]),
        scale_profile=ScaleProfileInfo(
            reference="walk_down", subject_ratio=0.795, canvas_fraction=0.969
        ),
        status="processed",
    )
    loaded = AssetManifest.load(manifest.save(tmp_path / "m.json"))
    assert loaded.scale_profile is not None
    assert loaded.scale_profile.reference == "walk_down"


def test_synthetic_grid_renders(tmp_path) -> None:
    """夹具自检：合成图确实是可键控的近洋红背景。"""
    Image.fromarray(grid_with_subject()).save(tmp_path / "g.png")
    assert (tmp_path / "g.png").exists()


# -- 放大方向：只允许整数倍 -------------------------------------------------


def test_uneven_upscale_is_reported_but_not_corrected() -> None:
    """非整数倍放大会让一部分块占 1px、一部分占 2px，等宽被打回参差 ——
    值得告警，但**不能自动改掉**。

    曾经在这里向下取整到整数倍，结果是只有需要放大的动作被砍、需要缩小的
    不受影响，跨动作缩放基准当场失效：实测同一个角色 hurt 占画布 49%、
    attack 占 78%，连参考动作自己都够不到自己的目标（要 1.745× 被砍成 1.0）。
    """
    from pixel_asset_forge.processing.scale_profile import uneven_upscale

    assert uneven_upscale(1.17)
    assert uneven_upscale(2.6)
    assert not uneven_upscale(2.0), "整数倍放大是干净的"
    assert not uneven_upscale(0.6), "缩小不在这条规则的范围里"


def test_a_canvas_larger_than_native_still_fills_it() -> None:
    """跨动作一致性是硬要求，像素等宽是加分项 —— 冲突时前者赢。"""
    scale = scale_for(None, content_size=(55, 82), cell_height=100, canvas=(96, 96))
    assert scale == pytest.approx(96 / 82)


def test_the_reference_action_can_always_reach_its_own_target() -> None:
    """参考动作按定义就该占到 canvas_fraction —— 够不到就说明基准形同虚设。"""
    profile = ScaleProfile(reference="hurt_down", subject_ratio=0.653, canvas_fraction=0.854)
    scale = scale_for(profile, content_size=(42, 47), cell_height=72, canvas=(96, 96))
    assert scale * 47 == pytest.approx(0.854 * 96, abs=1.0)


# -- 基准不能循环推导 -------------------------------------------------------


def test_deriving_from_an_already_scaled_output_shrinks_everything() -> None:
    """这是被修复的缺陷。

    增量生成时基准边走边顶替，后来的参考动作在写入时已经被前一任基准缩过一道。
    拿那个缩过的输出去推 canvas_fraction，记下的就不是"参考填满画布"而是
    "参考只占画布 43%"（实测值），于是全部动作都照这个比例再缩一遍。
    """
    tall = derive_profile(
        "hurt_down", content_height=47, cell_height=72,
        canvas_height=96, output_height=94,       # 未被缩过：接近填满
    )
    circular = derive_profile(
        "hurt_down", content_height=47, cell_height=72,
        canvas_height=96, output_height=41,       # 被前一任基准缩过
    )
    assert tall.canvas_fraction > 0.95
    assert circular.canvas_fraction < 0.5

    # 同一个动作在两套基准下的输出高度差了一倍以上
    args = dict(content_size=(52, 82), cell_height=145, canvas=(96, 96))
    assert scale_for(tall, **args) > scale_for(circular, **args) * 1.9
