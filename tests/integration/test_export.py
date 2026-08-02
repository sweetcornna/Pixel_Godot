"""导出器与 MVP 端到端（Sprint 6）。

MVP 退出门槛：一个 YAML → 四方向 × idle/walk，Manifest 能完整重建导出文件，
Contact Sheet 可供一次性人工审核。

**「Godot 能直接加载」这一条本套件覆盖不了**：这里只验证 `.tres` 的结构
符合 Godot 4 资源语法。真机验证在 `tools/godot-gate/`，需要下载 Godot 才能跑 ——
2026-07-29 已用 Godot 4.3 跑通（4 条动画、帧数/fps/loop/纹理尺寸全对，
并真的挂到 AnimatedSprite2D 上播放）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pixel_asset_forge.cli import EXIT_ERROR, EXIT_OK, app
from pixel_asset_forge.config import Config
from pixel_asset_forge.errors import ExportError
from pixel_asset_forge.exporters import get_exporter
from pixel_asset_forge.models.manifest import AssetManifest
from pixel_asset_forge.pipelines import (
    approve_seed,
    create_animation,
    create_character,
    run_export,
)
from pixel_asset_forge.storage import ArtifactStore

runner = CliRunner()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        provider="mock", model="mock-image",
        output_dir=tmp_path / "outputs", cache_dir=tmp_path / "cache",
    )


@pytest.fixture
def mvp_asset(config: Config, tmp_path: Path, examples_dir: Path) -> ArtifactStore:
    """MVP 范围的资产：slime（可镜像）× idle/walk × 四方向。"""
    request = tmp_path / "slime.yaml"
    request.write_text((examples_dir / "slime.yaml").read_text(encoding="utf-8"),
                       encoding="utf-8")
    create_character(request, config)
    store = ArtifactStore.for_asset(config.output_dir, "slime_01")
    approve_seed(store.root)

    for action in ("idle", "walk"):
        for direction in ("down", "left", "up", "right"):  # right 由 left 派生
            create_animation(
                store.root, action=action, direction=direction, config=config
            )
    from pixel_asset_forge.pipelines.validation import run_validation

    run_validation(store.root)
    return store


# -- MVP 覆盖面 ------------------------------------------------------------


def test_mvp_produces_all_eight_animations(mvp_asset: ArtifactStore) -> None:
    """一个 YAML → 四方向 × idle/walk。"""
    manifest = AssetManifest.load(mvp_asset.manifest_path)
    assert set(manifest.animations) == {
        f"{a}_{d}" for a in ("idle", "walk") for d in ("down", "left", "right", "up")
    }


def test_mirrored_directions_cost_no_api_call(mvp_asset: ArtifactStore) -> None:
    manifest = AssetManifest.load(mvp_asset.manifest_path)
    derived = manifest.derived_animations()
    assert set(derived) == {"idle_right", "walk_right"}
    assert not mvp_asset.source_path("walk_right").exists()


def test_all_animations_share_one_scale_baseline(mvp_asset: ArtifactStore) -> None:
    """没有共享基准的话，角色会在 idle 与 walk 之间变大变小。

    基准必须来自**某个动作**，且是幅度最大的那个。seed 当不了参考 ——
    它是 1×1 整幅画布、prompt 还要求四周留 10% 边距，与动作格子的构图约定
    根本不可比（实测 subject_ratio 0.57 对 0.80），拿它当基准会把所有动作
    都推到画布外再钳回来，等于没做。
    """
    manifest = AssetManifest.load(mvp_asset.manifest_path)
    assert manifest.scale_profile is not None
    assert manifest.scale_profile.reference in manifest.animations
    assert manifest.scale_profile.subject_ratio > 0


def test_the_baseline_is_the_largest_action_not_the_first(
    mvp_asset: ArtifactStore,
) -> None:
    """基准动作在自己格子里必须占得最满 —— 否则别的动作会被推出画布。"""
    from pixel_asset_forge.pipelines.process import run_process

    summaries = {s["key"]: s for s in run_process(mvp_asset.root)}
    manifest = AssetManifest.load(mvp_asset.manifest_path)
    reference = manifest.scale_profile.reference  # type: ignore[union-attr]

    clamped = [k for k, s in summaries.items()
               if any("画布之外" in w for w in s["warnings"])]
    assert not clamped, f"这些动作被钳制了，说明基准 {reference} 选小了：{clamped}"


# -- Generic JSON ----------------------------------------------------------


def test_generic_json_is_self_contained(mvp_asset: ArtifactStore) -> None:
    """读取方只需图 + JSON，不需要理解本项目的目录结构。"""
    manifest = AssetManifest.load(mvp_asset.manifest_path)
    out = mvp_asset.exports / "generic-json"
    get_exporter("generic-json").export(manifest, mvp_asset.root, out)

    payload = json.loads((out / "slime_01.json").read_text(encoding="utf-8"))
    assert payload["schema"] == "pixel-asset-forge.generic.v1"
    assert (out / payload["atlas"]["image"]).exists()
    assert set(payload["animations"]) == set(manifest.animations)

    # 区域用绝对像素坐标，且必须落在图内
    width, height = payload["atlas"]["width"], payload["atlas"]["height"]
    for anim in payload["animations"].values():
        for frame in anim["frames"]:
            assert frame["x"] + frame["width"] <= width
            assert frame["y"] + frame["height"] <= height


def test_generic_json_records_anchor_and_palette(mvp_asset: ArtifactStore) -> None:
    manifest = AssetManifest.load(mvp_asset.manifest_path)
    out = mvp_asset.exports / "generic-json"
    get_exporter("generic-json").export(manifest, mvp_asset.root, out)
    payload = json.loads((out / "slime_01.json").read_text(encoding="utf-8"))

    assert payload["anchor"]["type"] == "bottom_center"
    assert payload["palette"] == manifest.palette.colors
    assert payload["scale_profile"]["reference"]


# -- Godot -----------------------------------------------------------------


def tres_of(store: ArtifactStore) -> str:
    manifest = AssetManifest.load(store.manifest_path)
    out = store.exports / "godot"
    get_exporter("godot").export(manifest, store.root, out)
    return (out / "slime_01_frames.tres").read_text(encoding="utf-8")


def test_godot_header_declares_the_right_load_steps(mvp_asset: ArtifactStore) -> None:
    """``load_steps`` 必须等于 ext + sub + 1。

    **实测更正**：把它改成 3（正确值 22）之后 Godot 4.3 照样加载成功 ——
    它是给加载进度条用的提示，不是硬校验。这里原本写着"数不对会让 Godot
    加载失败"，那句话是错的，靠真机跑 tools/godot-gate 才发现。

    仍然保留这条断言：写对是零成本的，而写错会让 Godot 的加载进度显示失真，
    未来版本也不保证一直宽容。但别再把它当成"加载会失败"的护栏。
    """
    text = tres_of(mvp_asset)
    declared = int(re.search(r"load_steps=(\d+)", text).group(1))
    ext = len(re.findall(r"^\[ext_resource ", text, re.M))
    sub = len(re.findall(r"^\[sub_resource ", text, re.M))
    assert declared == ext + sub + 1


def test_godot_animation_names_are_stringnames(mvp_asset: ArtifactStore) -> None:
    """Godot 4 用 StringName；写成普通字符串读不到动画。"""
    text = tres_of(mvp_asset)
    for key in ("walk_down", "idle_up", "walk_right"):
        assert f'"name": &"{key}"' in text


def test_godot_speed_is_fps_not_duration(mvp_asset: ArtifactStore) -> None:
    """``speed`` 是每秒帧数；每帧 ``duration`` 是相对倍率而非秒。"""
    text = tres_of(mvp_asset)
    manifest = AssetManifest.load(mvp_asset.manifest_path)
    walk = manifest.animations["walk_down"]
    assert f'"speed": {float(walk.fps)}' in text
    assert '"duration": 1.0' in text


def test_godot_regions_match_the_atlas(mvp_asset: ArtifactStore) -> None:
    text = tres_of(mvp_asset)
    manifest = AssetManifest.load(mvp_asset.manifest_path)
    size = manifest.canvas.width
    regions = re.findall(r"region = Rect2\((\d+), (\d+), (\d+), (\d+)\)", text)

    total_frames = sum(
        len(manifest.resolve_frames(key)) for key in manifest.animations
    )
    assert len(regions) == total_frames
    for _x, _y, w, h in regions:
        assert (int(w), int(h)) == (size, size)


def test_godot_texture_path_is_relative_so_the_directory_can_go_anywhere(
    mvp_asset: ArtifactStore,
) -> None:
    """``ext_resource`` 必须是相对 ``.tres`` 自身的路径，不能是 ``res://`` 绝对路径。

    绝对形式写死了"png 在项目根"，与我们"整目录复制进项目"的交付说明冲突 ——
    照说明放进 ``res://assets/`` 就会 Parse Error。相对路径两种放法都成立。
    """
    text = tres_of(mvp_asset)
    assert 'path="slime_01.png"' in text
    assert "res://" not in text, "回退到绝对路径会让整目录复制到子目录时崩掉"

    # 相对路径成立的前提：引用的纹理确实与 .tres 同目录。
    godot_dir = mvp_asset.exports / "godot"
    assert (godot_dir / "slime_01.png").is_file()


def test_godot_handoff_doc_ships_with_the_directory(mvp_asset: ArtifactStore) -> None:
    """四条必设项必须随目录落盘。

    只打在终端的知识传不到下游 —— ``create-asset`` 与 ``create-asset-pack``
    都不打印 exporter 的 notes，批量生产的人一次也看不到。
    """
    manifest = AssetManifest.load(mvp_asset.manifest_path)
    out_dir = mvp_asset.exports / "godot"
    result = get_exporter("godot").export(manifest, mvp_asset.root, out_dir)

    doc = out_dir / "GODOT-README.md"
    assert doc.is_file()
    assert doc in result.files
    text = doc.read_text(encoding="utf-8")
    assert "Nearest" in text, "线性过滤会把像素糊掉"
    assert "offset" in text, "不设 offset 脚底对不上节点原点"
    assert "animation_finished" in text, "一次性动作要连信号，循环动作不要"
    assert manifest.asset_id in text


def test_godot_export_says_where_the_real_gate_lives(mvp_asset: ArtifactStore) -> None:
    """这套用例只验证 .tres 的结构语法，证明不了 Godot 能加载。

    真机判据在 tools/godot-gate/ —— 导出提示里必须指向它，
    否则用户会以为结构断言全绿就等于引擎里能用。
    """
    manifest = AssetManifest.load(mvp_asset.manifest_path)
    result = get_exporter("godot").export(
        manifest, mvp_asset.root, mvp_asset.exports / "godot"
    )
    assert any("tools/godot-gate" in n for n in result.notes)
    assert any("Nearest" in n for n in result.notes), "线性过滤会把像素糊掉"


# -- Manifest 可重建 -------------------------------------------------------


def test_export_needs_only_manifest_and_frames(mvp_asset: ArtifactStore) -> None:
    """ADR-001：所有导出文件必须能仅凭 Manifest + frames/ 重建。

    删掉 source/、jobs/、request.yaml 之后导出仍应成功。
    """
    import shutil

    shutil.rmtree(mvp_asset.source)
    shutil.rmtree(mvp_asset.root / "jobs")
    mvp_asset.request_path.unlink()

    summary = run_export(mvp_asset.root, targets=["generic-json", "godot"])
    # generic-json: json + png / godot: png + .tres + GODOT-README.md
    assert len(summary.files) == 5
    assert {p.name for p in summary.files} >= {"GODOT-README.md", "slime_01_frames.tres"}
    assert summary.contact_sheet is not None


def test_missing_derived_frames_fail_loudly(mvp_asset: ArtifactStore) -> None:
    """镜像方向没有自己的帧时，绝不能悄悄导出源方向的帧（朝向会反）。"""
    import shutil

    shutil.rmtree(mvp_asset.frames_of("walk_right"))
    with pytest.raises(ExportError, match="朝向相反"):
        run_export(mvp_asset.root, targets=["generic-json"])


# -- Contact sheet ---------------------------------------------------------


def test_contact_sheet_covers_every_animation(mvp_asset: ArtifactStore) -> None:
    """帧序被打乱无法自动检测 —— 这张图是唯一的防线。"""
    from PIL import Image

    summary = run_export(mvp_asset.root, targets=["generic-json"])
    assert summary.contact_sheet is not None

    manifest = AssetManifest.load(mvp_asset.manifest_path)
    sheet = Image.open(summary.contact_sheet)
    assert sheet.height == len(manifest.animations) * manifest.canvas.height * 4


def test_contact_sheet_background_cannot_hide_a_whole_color(
    mvp_asset: ArtifactStore,
) -> None:
    """贴片区必须是棋盘格，任何单色都不能与整片背景撞色。

    实测过的真实故障：旧的纯色背景 ``#22222C`` 与本仓库两个示例包的标准描边色
    ``#211A2C`` 距离只有 8.06，wooden_barrel 42% 的像素在人工审核图上直接隐形 ——
    而 contact sheet 是帧序/朝向错误的唯一防线。
    """
    import math

    from pixel_asset_forge.pipelines.export import CHECKER_DARK, CHECKER_LIGHT

    summary = run_export(mvp_asset.root, targets=["generic-json"])
    assert summary.contact_sheet is not None

    outline = (0x21, 0x1A, 0x2C)  # 示例包的描边色
    worst = min(math.dist(outline, CHECKER_LIGHT), math.dist(outline, CHECKER_DARK))
    assert worst > 60, f"描边色与棋盘格最近的一格只差 {worst:.1f}，仍会隐形"

    # 两格必须真的不同色，否则棋盘格退化成纯色，上面的保证就没了。
    assert math.dist(CHECKER_LIGHT, CHECKER_DARK) > 100


def test_export_can_skip_the_contact_sheet(mvp_asset: ArtifactStore) -> None:
    summary = run_export(
        mvp_asset.root, targets=["generic-json"], contact_sheet=False
    )
    assert summary.contact_sheet is None


# -- CLI -------------------------------------------------------------------


def test_cli_export_defaults_to_both_targets(
    mvp_asset: ArtifactStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["export", str(mvp_asset.root)])
    assert result.exit_code == EXIT_OK
    assert (mvp_asset.exports / "godot").exists()
    assert (mvp_asset.exports / "generic-json").exists()
    assert "Godot 4.3 真机验证" in result.stdout


def test_cli_export_accepts_one_target(
    mvp_asset: ArtifactStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["export", str(mvp_asset.root), "-t", "godot"])
    assert result.exit_code == EXIT_OK
    assert not (mvp_asset.exports / "generic-json").exists()


def test_cli_export_rejects_unknown_target(
    mvp_asset: ArtifactStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["export", str(mvp_asset.root), "-t", "unity"])
    assert result.exit_code == EXIT_ERROR
    assert "Sprint 7" in result.stderr or "可选" in result.stderr


def test_cli_export_without_manifest_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    empty = tmp_path / "nothing"
    empty.mkdir()
    assert runner.invoke(app, ["export", str(empty)]).exit_code == EXIT_ERROR
