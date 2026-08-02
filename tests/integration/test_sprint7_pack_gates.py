"""Sprint 7 总退出门槛：五条要对**全部五种 pack 类型**成立。

每一切自己的纵切测试守的是那一种 pack 的语义（seed 闸门、基准收敛、
静态导出硬闸……）。这个文件只干一件事：把总门槛按 `pack_type` 逐个跑一遍。

**刻意不靠「共用代码路径」推断。** 静态三种确实共用 ``_run_one_static``、
动画两种共用 ``_run_one_animated``，但总门槛的口径是「对全部 pack 类型成立」，
而映射表、schema 条件约束、动作缺省值这些恰恰是逐类型分叉的 —— 推断会把
分叉处的洞盖住。为此宁可多跑几遍 mock。

用例里的 pack 都被裁过（静态留 2 个资产、动画留 1 个资产 1 个动作 1 个方向）：
门槛验的是机制，不是资产条数，而 96px × 12 个动作跑三遍纯属浪费。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from pixel_asset_forge.cli import EXIT_OK, app
from pixel_asset_forge.models import AssetManifest
from pixel_asset_forge.pipelines.process import run_process
from pixel_asset_forge.storage import ArtifactStore

runner = CliRunner()

#: 模块级夹具拿不到函数作用域的 ``examples_dir``，这里按 conftest 同样的方式算。
EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

PACK_TYPES = (
    "potion_pack",
    "weapon_pack",
    "environment_pack",
    "spell_bundle",
    "combat_bundle",
)
ANIMATED_PACK_TYPES = ("spell_bundle", "combat_bundle")

#: mock provider 的模拟 moderation 拦截词，用来制造**永久失败**的兄弟资产。
BLOCKED_DESCRIPTION = "A mutilated gore trophy used to trigger mock moderation."


def _write_config(path: Path) -> Path:
    path.write_text(
        "provider: mock\n"
        "model: mock-image\n"
        "output_dir: outputs\n"
        "cache_dir: cache\n"
        "max_concurrency: 2\n",
        encoding="utf-8",
    )
    return path


def _trim(data: dict[str, Any], pack_type: str) -> dict[str, Any]:
    """裁到够验门槛的最小规模。"""
    if pack_type in ANIMATED_PACK_TYPES:
        data["assets"] = data["assets"][:1]
        data["shared"]["animations"] = data["shared"]["animations"][:1]
        data["shared"]["animations"][0]["directions"] = ["down"]
    else:
        data["assets"] = data["assets"][:2]
    return data


def _pack_file(
    examples_dir: Path, tmp_path: Path, pack_type: str, *, pack_id: str
) -> tuple[Path, dict[str, Any]]:
    data = yaml.safe_load(
        (examples_dir / f"{pack_type}.yaml").read_text(encoding="utf-8")
    )
    data = _trim(data, pack_type)
    data["pack_id"] = pack_id
    # asset_id 也要错开：输出目录按 asset_id 存，同名会跨用例串味。
    for asset in data["assets"]:
        asset["asset_id"] = f"{pack_id}_{asset['asset_id']}"
    path = tmp_path / f"{pack_id}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path, data


def _run_to_exported(
    pack_path: Path, config_path: Path, data: dict[str, Any]
) -> dict[str, Any]:
    """跑到全部 exported。动画 pack 会在 seed 闸门停一次，批准后续跑。"""
    planned = runner.invoke(
        app, ["plan", str(pack_path), "--config", str(config_path), "--save"]
    )
    assert planned.exit_code == EXIT_OK, planned.stdout

    result = runner.invoke(
        app,
        ["create-asset-pack", str(pack_path), "--config", str(config_path), "--json"],
    )
    assert result.exit_code == EXIT_OK, result.stdout
    payload = json.loads(result.stdout)

    if payload["counts"]["awaiting_approval"]:
        action = data["shared"]["animations"][0]["name"]
        for asset in data["assets"]:
            approved = runner.invoke(
                app,
                [
                    "create-animation", "--asset", asset["asset_id"],
                    "--action", action, "--direction", "down",
                    "--approve-seed", "--config", str(config_path),
                ],
            )
            assert approved.exit_code == EXIT_OK, approved.stdout
        result = runner.invoke(
            app,
            [
                "create-asset-pack", str(pack_path),
                "--config", str(config_path), "--json",
            ],
        )
        assert result.exit_code == EXIT_OK, result.stdout
        payload = json.loads(result.stdout)

    assert payload["counts"]["exported"] == len(data["assets"]), payload["counts"]
    return payload


@pytest.fixture(scope="module")
def _built_packs(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, tuple[Path, Path, dict[str, Any]]]:
    """五种 pack 各跑一遍到 exported，模块内共用（重跑一次要好几分钟）。"""
    root = tmp_path_factory.mktemp("sprint7_gates")
    built: dict[str, tuple[Path, Path, dict[str, Any]]] = {}
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(root)
        patch.delenv("PIXEL_ASSET_API_KEY", raising=False)
        patch.delenv("OPENAI_API_KEY", raising=False)

        async def inline_to_thread(function, /, *args, **kwargs):
            return function(*args, **kwargs)

        patch.setattr(asyncio, "to_thread", inline_to_thread)
        config_path = _write_config(root / "mock.yaml")
        for pack_type in PACK_TYPES:
            pack_path, data = _pack_file(
                EXAMPLES, root, pack_type, pack_id=f"gate_{pack_type}"
            )
            _run_to_exported(pack_path, config_path, data)
            built[pack_type] = (pack_path, config_path, data)
        built["__root__"] = (root, config_path, {})  # type: ignore[assignment]
    return built


@contextmanager
def _inside_pack_root(
    built: dict[str, tuple[Path, Path, dict[str, Any]]],
) -> Iterator[None]:
    """CLI 的 ``output_dir: outputs`` 是相对 cwd 的，模块夹具建在别处。"""
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(built["__root__"][0])

        async def inline_to_thread(function, /, *args, **kwargs):
            return function(*args, **kwargs)

        patch.setattr(asyncio, "to_thread", inline_to_thread)
        yield


def _stores(
    built: dict[str, tuple[Path, Path, dict[str, Any]]], pack_type: str
) -> list[ArtifactStore]:
    root = built["__root__"][0]
    _pack_path, _config_path, data = built[pack_type]
    return [
        ArtifactStore.for_asset(root / "outputs", asset["asset_id"])
        for asset in data["assets"]
    ]


@pytest.mark.parametrize("pack_type", PACK_TYPES)
def test_pack_assets_share_the_declared_style_and_palette(
    pack_type: str,
    _built_packs: dict[str, tuple[Path, Path, dict[str, Any]]],
) -> None:
    """总门槛 5：同批资产共享风格与调色板定义。

    断言的是**声明值**落到了每个资产的 Manifest，不是「三个资产恰好一样」——
    后者在只有一个资产的 pack 上会退化成永真。
    """
    _pack_path, _config_path, data = _built_packs[pack_type]
    shared = data["shared"]
    declared_palette = list(shared["palette"]["colors"])
    width, height = shared["style"]["target_size"]

    for store in _stores(_built_packs, pack_type):
        manifest = AssetManifest.load(store.manifest_path)
        assert manifest.palette.colors == declared_palette
        assert manifest.palette.max_colors == shared["style"]["max_colors"]
        assert (manifest.canvas.width, manifest.canvas.height) == (width, height)
        assert manifest.background.color_requested == shared["background"]["color"]


@pytest.mark.parametrize("pack_type", PACK_TYPES)
def test_pack_assets_reprocess_without_regenerating(
    pack_type: str,
    tmp_path: Path,
    _built_packs: dict[str, tuple[Path, Path, dict[str, Any]]],
) -> None:
    """总门槛 3：可重新处理而不重新生成。

    判据取 ``generation-log`` 的**字节**与原图字节：处理链可以随便重跑，
    但一条 API 调用记录都不该多出来，原始生成图也永不被覆盖。

    在**副本**上跑：重跑会把 Manifest 打回 ``processed``，留在原地会串到同一
    夹具上的其它门槛用例去（第一版就是这么挂的，续跑那条门槛读到了被本用例
    改过的状态）。
    """
    for original in _stores(_built_packs, pack_type):
        root = tmp_path / original.root.name
        shutil.copytree(original.root, root)
        store = ArtifactStore(root=root)
        log_before = store.generation_log_path.read_bytes()
        sources_before = {
            path.name: path.read_bytes() for path in sorted(store.source.glob("*.png"))
        }
        assert sources_before, f"{root} 下没有原图，判据会退化成永真"

        summaries = run_process(root)

        assert summaries, f"{root} 重跑没有产出任何处理摘要"
        assert store.generation_log_path.read_bytes() == log_before
        assert {
            path.name: path.read_bytes() for path in sorted(store.source.glob("*.png"))
        } == sources_before


@pytest.mark.parametrize("pack_type", PACK_TYPES)
def test_pack_asset_exports_by_asset_id(
    pack_type: str,
    _built_packs: dict[str, tuple[Path, Path, dict[str, Any]]],
) -> None:
    """总门槛 4：可按 ``asset_id`` 单独导出。"""
    _pack_path, config_path, _data = _built_packs[pack_type]
    with _inside_pack_root(_built_packs):
        for store in _stores(_built_packs, pack_type):
            asset_id = store.root.name
            exported = runner.invoke(
                app,
                ["export", asset_id, "--config", str(config_path), "--no-contact-sheet"],
            )
            assert exported.exit_code == EXIT_OK, exported.stdout
            assert (store.exports / "generic-json" / f"{asset_id}.json").exists()
            assert (store.exports / "godot").is_dir()


@pytest.mark.parametrize("pack_type", PACK_TYPES)
def test_completed_pack_reruns_are_skipped_not_regenerated(
    pack_type: str,
    _built_packs: dict[str, tuple[Path, Path, dict[str, Any]]],
) -> None:
    """总门槛 2 的一半：跑完的资产在续跑里被跳过，不重复烧 API。

    真正的「中途断点续跑」由各纵切自己的用例守（potion 的 stop/resume、
    spell 与 combat 的 seed 闸门续跑）；这里补的是「对每种 pack 类型都成立」。
    """
    pack_path, config_path, data = _built_packs[pack_type]
    logs_before = {
        store.root.name: store.generation_log_path.read_bytes()
        for store in _stores(_built_packs, pack_type)
    }

    with _inside_pack_root(_built_packs):
        again = runner.invoke(
            app,
            [
                "create-asset-pack", str(pack_path),
                "--config", str(config_path), "--json",
            ],
        )
    assert again.exit_code == EXIT_OK, again.stdout
    counts = json.loads(again.stdout)["counts"]
    assert counts["skipped"] == len(data["assets"]), counts
    assert counts["exported"] == 0

    for store in _stores(_built_packs, pack_type):
        assert store.generation_log_path.read_bytes() == logs_before[store.root.name]


@pytest.mark.parametrize("pack_type", PACK_TYPES)
def test_one_failed_asset_does_not_cancel_its_siblings(
    pack_type: str,
    examples_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """总门槛 1：单个失败不导致整包失败。

    毒的是**第二个**资产：第一个已经跑完时才失败，能同时验到「先完成的不被
    回滚」与「失败之后的兄弟仍被派发」。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PIXEL_ASSET_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    async def inline_to_thread(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)
    config_path = _write_config(tmp_path / "mock.yaml")
    data = yaml.safe_load(
        (examples_dir / f"{pack_type}.yaml").read_text(encoding="utf-8")
    )
    data = _trim(data, pack_type)
    data["pack_id"] = f"partial_{pack_type}"
    # 动画 pack 的示例被裁成了单资产，这里补一个兄弟出来 —— 隔离性本来就要
    # 至少两个资产才谈得上。
    if len(data["assets"]) < 2:
        data["assets"].append(dict(data["assets"][0]))
    for index, asset in enumerate(data["assets"][:2]):
        asset["asset_id"] = f"partial_{pack_type}_{index}"
    data["assets"] = data["assets"][:2]
    data["assets"][1]["description"] = BLOCKED_DESCRIPTION
    pack_path = tmp_path / f"partial-{pack_type}.yaml"
    pack_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    planned = runner.invoke(
        app, ["plan", str(pack_path), "--config", str(config_path), "--save"]
    )
    assert planned.exit_code == EXIT_OK, planned.stdout
    result = runner.invoke(
        app,
        ["create-asset-pack", str(pack_path), "--config", str(config_path), "--json"],
    )
    payload = json.loads(result.stdout)
    counts = payload["counts"]
    outcomes = {asset["asset_id"]: asset for asset in payload["assets"]}

    assert counts["provider_failed"] == 1, counts
    failed = outcomes[f"partial_{pack_type}_1"]
    assert failed["error_code"] == "provider_moderation_blocked"

    # 兄弟资产照常推进到它这条 pack 类型该到的位置：静态 pack 一遍到底，
    # 动画 pack 停在 seed 闸门 —— 都不是失败。
    survivor = outcomes[f"partial_{pack_type}_0"]
    if pack_type in ANIMATED_PACK_TYPES:
        assert survivor["outcome"] == "awaiting_approval"
        assert counts["awaiting_approval"] == 1
    else:
        assert survivor["outcome"] == "exported"
        assert counts["exported"] == 1
    assert counts["outcome_unknown"] == 0
    assert counts["outcome_missing"] == 0
    summary_path = (
        tmp_path / "outputs" / "_packs" / data["pack_id"] / "pack-summary.json"
    )
    assert json.loads(summary_path.read_text(encoding="utf-8"))["counts"] == counts
