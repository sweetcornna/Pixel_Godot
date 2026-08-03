"""MCP 适配层 —— §6.2 的 6 个高层工具（ADR-005）。

**这里没有业务逻辑。** 每个工具调的都是 `pipelines/` 里那几个函数，与 CLI 同源。
MCP 协议会变，Python 函数不会 —— 把核心押在演进中的协议上是不必要的风险。

## 为什么恰好 6 个

ADR-005 的核心论点不是"要有 MCP"，是**工具数量必须刻意收敛**。三个代价里最关键
的是**顺序错误**：像素处理有严格的顺序依赖（despill 必须在量化前、越界检测必须在
切帧前），让模型编排这个顺序，等于把确定性流程交给不确定性组件 —— 而整个项目的
立论就是"AI 只生成视觉原料，本地程序负责所有需要精确性的操作"。

**这个论点会被一次"顺手加个工具"悄悄推翻，而推翻的那一刻没有任何东西会红。**
所以 :data:`TOOL_NAMES` 与它的测试是硬约束：加第 7 个工具必须先去改那条断言，
那一刻人会被迫重读 ADR-005 的理由。

## 为什么返回摘要而不是 Manifest

MCP 的返回**直接进模型上下文**。一个装着 24×16 地图的 tileset Manifest 有几千个
tile id，整个 dump 回去一次调用就能吃掉几万 token。

这与 §8.3 那次 OOM 是同一类错误：**没有上界的输出**。那次吃的是内存，这次吃的是
上下文。所以工具返回的是摘要 —— asset_id、状态、产物路径、检查项统计。
要看细节的路径是明确的：摘要里给出 Manifest 与报告的路径，让人或工具自己去读。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .errors import PixelAssetError
from .models.validation import ValidationReport
from .pipelines import create_animation as _create_animation
from .pipelines import create_character as _create_character
from .pipelines import run_export as _run_export
from .pipelines.asset_pack import run_asset_pack as _run_asset_pack
from .pipelines.validation import run_validation as _run_validation
from .repair import execute_plan, plan_repairs, rounds_used

#: 暴露给模型的工具名，**恰好这 6 个**（ADR-005）。
#:
#: 这不是"至少这些"。加第 7 个工具要先来改这里，并解释为什么它值得那三份代价
#: （上下文开销 / 选错概率 / 顺序错误）。
TOOL_NAMES: tuple[str, ...] = (
    "create_character",
    "create_animation",
    "create_asset_pack",
    "validate_asset",
    "repair_asset",
    "export_asset",
)

#: 单次返回的字符上界。超过就说明有人开始往回传大块产物了。
#:
#: 定这个数不是为了省流量，是为了守住"返回是摘要不是产物"这条线 —— 一旦某个
#: 工具开始 dump Manifest，它会先撞上这堵墙，而不是在用户的上下文里悄悄膨胀。
MAX_RESULT_CHARS = 4000


def _config(config_path: str | None) -> Config:
    return load_config(project_config=Path(config_path) if config_path else None)


def _report_summary(report: ValidationReport) -> dict[str, Any]:
    """验证报告 → 摘要。

    **失败必须原样传出去。** 全项目唯一不可退让的规则是"验证失败时绝不把资产
    标记为成功"（PLAN §9），MCP 是新增的一条返回路径，它同样要守 ——
    所以这里既给 ``passed``，也把阻断项逐条列出来，不做任何"大体上没问题"的加工。
    """
    blocking = report.blocking_checks
    return {
        "passed": report.passed,
        "summary": report.summary(),
        # 只列阻断项，且截断 —— 一个坏掉的资产可能有上百条失败，全列回去等于
        # 把报告塞进上下文。要全文就去读 validation-report.json。
        "blocking": [
            {"id": check.id, "target": check.target, "severity": str(check.severity),
             "message": check.message}
            for check in blocking[:10]
        ],
        "blocking_total": len(blocking),
    }


# -- 6 个工具的实现 ----------------------------------------------------------
#
# 每个都是薄适配：解析入参 → 调 pipelines → 收成摘要。异常统一由 build_server
# 那层转成结构化错误，不在这里各写一遍。


def create_character(request_file: str, config_path: str | None = None) -> dict[str, Any]:
    """从 Asset Request 生成 canonical seed。**调用 API。**

    产出停在人工闸门：seed 是所有动画的身份基准，它不对则后续动画全部作废重来。
    """
    result = _create_character(request_file, _config(config_path))
    return {
        "asset_id": result.asset_id,
        "seed_image": str(result.pixel_path),
        "approved": False,
        "next": "看过 seed 图后用 create_animation 并带 approve_seed 放行",
    }


def create_animation(
    asset_dir: str,
    action: str,
    direction: str | None = None,
    config_path: str | None = None,
    approve_seed: bool = False,
) -> dict[str, Any]:
    """给一个已有 canonical seed 的角色生成一个动作网格。**调用 API。**"""
    from .pipelines import approve_seed as _approve_seed

    config = _config(config_path)
    if approve_seed:
        _approve_seed(asset_dir)
    result = _create_animation(
        asset_dir, action=action, direction=direction, config=config  # type: ignore[arg-type]
    )
    return {
        "asset_id": result.asset_id,
        "key": result.key,
        "frames": result.frames,
        "next": "validate_asset 查产物；API 返回成功 ≠ 资产合格",
    }


def create_asset_pack(
    pack_file: str, config_path: str | None = None, retry_failed: bool = False
) -> dict[str, Any]:
    """批量生成一组共享约束的资产。**调用 API。**

    单资产失败不取消其余资产；重跑同一份 pack 即断点续跑。
    """
    # run_asset_pack 是 async 的（批次调度要并发跑 worker）。这里同步跑完它 ——
    # 六个工具里只有它是协程，被 mypy 抓到过一次：同步调用会拿到 coroutine 对象，
    # 然后在 .counts() 上炸，而那时错误信息与真正的原因已经隔了一层。
    summary = asyncio.run(
        _run_asset_pack(pack_file, _config(config_path), retry_failed=retry_failed)
    )
    return {
        "pack_id": summary.pack_id,
        "counts": dict(summary.counts),
        # 只列前 20 个 —— 一个 50 资产的 pack 全列回去就把上下文占满了。
        "assets": [
            {"asset_id": entry.asset_id, "outcome": str(entry.outcome)}
            for entry in summary.assets[:20]
        ],
        "assets_total": len(summary.assets),
    }


def validate_asset(asset_dir: str, config_path: str | None = None) -> dict[str, Any]:
    """跑验证引擎。**不调用 API。**

    失败原样传出去 —— 见 :func:`_report_summary`。
    """
    _config(config_path)  # 校验配置可加载；validate 本身不需要它
    report = _run_validation(Path(asset_dir))
    return {"asset_dir": str(asset_dir), **_report_summary(report)}


def repair_asset(asset_dir: str, config_path: str | None = None) -> dict[str, Any]:
    """按验证报告执行修复计划。本地修复不调用 API，重生成会。"""
    config = _config(config_path)
    report = _run_validation(Path(asset_dir))
    plan = plan_repairs(
        report,
        rounds_used=rounds_used(Path(asset_dir)),
        max_rounds=config.max_repair_rounds,
    )
    if not plan.steps:
        return {"asset_dir": str(asset_dir), "repaired": False,
                "reason": "没有可自动修复的失败项", **_report_summary(report)}
    outcomes = execute_plan(Path(asset_dir), plan, config)
    after = _run_validation(Path(asset_dir))
    return {
        "asset_dir": str(asset_dir),
        "repaired": True,
        "steps": len(plan.steps),
        "outcomes": len(outcomes),
        **_report_summary(after),
    }


def export_asset(
    asset_dir: str, targets: list[str] | None = None, config_path: str | None = None
) -> dict[str, Any]:
    """导出到引擎格式。**不调用 API。**

    未通过验证的资产会被拒绝导出 —— 那道闸门在流水线里，不在这里。
    """
    summary = _run_export(asset_dir, targets=targets or ["generic-json", "godot"])
    return {
        "asset_id": summary.asset_id,
        "targets": list(summary.targets),
        "files": len(summary.files),
        "contact_sheet": str(summary.contact_sheet) if summary.contact_sheet else None,
    }


_IMPLEMENTATIONS = {
    "create_character": create_character,
    "create_animation": create_animation,
    "create_asset_pack": create_asset_pack,
    "validate_asset": validate_asset,
    "repair_asset": repair_asset,
    "export_asset": export_asset,
}


def tool_names_from_server() -> set[str]:
    """从**组装好的 server** 上把工具名读回来，而不是直接信 :data:`TOOL_NAMES`。

    直接断言常量等于常量是同义反复：真正要防的是"常量写着 6 个、server 上挂了
    7 个"。所以这里走一遍 server 自己的注册表。
    """
    server = build_server()
    return {tool.name for tool in asyncio.run(server.list_tools())}


def tool_descriptions() -> dict[str, str]:
    server = build_server()
    return {tool.name: (tool.description or "") for tool in asyncio.run(server.list_tools())}


def build_server() -> Any:
    """组装 MCP server。``mcp`` 是**可选依赖**，没装就在这里明确报错。"""
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError as exc:  # pragma: no cover - 取决于安装形态
        raise PixelAssetError(
            "未安装 MCP SDK。装：`uv sync --extra mcp` 或 `pip install 'pixel-asset-forge[mcp]'`"
        ) from exc

    server = MCPServer(
        name="pixel-asset-forge",
        instructions=(
            "像素资产编译器。6 个高层工具各对应一个完整业务动作，"
            "内部流程写死 —— 不要试图组合底层步骤，像素处理有严格顺序依赖。"
        ),
    )
    for name in TOOL_NAMES:
        server.add_tool(_IMPLEMENTATIONS[name], name=name)  # type: ignore[arg-type]
    return server


def main() -> None:  # pragma: no cover - 进程入口
    # 缺 [mcp] extra 是发布后最常见的失败形态 —— 给一行干净的报错，
    # 而不是把 traceback 甩给刚 pip install 完的用户。
    import sys

    try:
        server = build_server()
    except PixelAssetError as exc:
        print(f"pixel-asset-mcp: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    server.run()
