"""``pixel-asset`` 命令行。

9 个命令中有 5 个完全不调用 API（``init`` / ``plan`` / ``process`` / ``validate`` / ``export``）
—— 这是刻意设计：调试与迭代应尽量在离线侧完成（PLAN §6.1）。

Sprint 1 交付 ``init`` / ``plan`` / ``doctor``；其余命令给出明确的"排期在哪个 Sprint"出口，
而不是一个看起来像 bug 的堆栈。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from . import (
    MANIFEST_SCHEMA_VERSION,
    PIPELINE_VERSION,
    REPORT_SCHEMA_VERSION,
    REQUEST_SCHEMA_VERSION,
)
from .config import (
    API_KEY_ENV_VARS,
    DEFAULT_CONFIG_TEMPLATE,
    PROJECT_CONFIG_NAME,
    Config,
    find_project_config,
    load_config,
)
from .constants import (
    ALLOWED_FRAME_COUNTS,
    REPAIR_PLAN_FILE,
    THRESHOLDS_CALIBRATED,
    VALIDATION_REPORT_FILE,
)
from .errors import (
    NotImplementedYetError,
    PixelAssetError,
    RequestValidationError,
)
from .logging_utils import configure_logging
from .models.job import JobKind
from .models.request import load_request
from .pipelines import approve_seed as run_approve_seed
from .pipelines import create_animation as run_create_animation
from .pipelines import create_character as run_create_character
from .pipelines import import_keyframes as run_import_keyframes
from .pipelines import import_seed as run_import_seed
from .pipelines import next_pending, run_export, run_interpolate, run_process
from .planning import layout_for_frames, plan_request, seed_layout
from .repair import execute_plan as execute_repairs
from .repair import plan_repairs
from .repair import rounds_used as repair_rounds_used
from .schema_registry import SCHEMA_FILES, schema_dir
from .storage import ArtifactStore
from .validation import validate_asset as run_validation

app = typer.Typer(
    name="pixel-asset",
    help="像素游戏资产编译器 —— 自然语言 → 可直接导入引擎的像素资产。",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
err_console = Console(stderr=True)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INVALID_REQUEST = 2
EXIT_VALIDATION_FAILED = 3
EXIT_NOT_IMPLEMENTED = 4


def _fail(exc: PixelAssetError) -> None:
    """统一的错误出口。错误信息已在 :class:`PixelAssetError` 里脱敏过。"""
    if isinstance(exc, RequestValidationError):
        err_console.print(f"[red]✗[/red] {exc.message}")
        for item in exc.errors:
            err_console.print(f"  [yellow]{item['path'] or '<root>'}[/yellow]: {item['message']}")
        raise typer.Exit(EXIT_INVALID_REQUEST)
    if isinstance(exc, NotImplementedYetError):
        err_console.print(f"[yellow]![/yellow] {exc.message}")
        raise typer.Exit(EXIT_NOT_IMPLEMENTED)
    err_console.print(f"[red]✗[/red] [{exc.code}] {exc.message}")
    raise typer.Exit(EXIT_ERROR)


def _load_config(config_path: Path | None = None) -> Config:
    try:
        return load_config(project_config=config_path)
    except PixelAssetError as exc:
        _fail(exc)
        raise  # pragma: no cover - _fail 永远抛出


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@app.command()
def init(
    directory: Annotated[Path, typer.Argument(help="项目目录，默认当前目录")] = Path(),
    force: Annotated[bool, typer.Option("--force", help="覆盖已存在的配置文件")] = False,
) -> None:
    """初始化项目配置与输出目录。不调用 API。"""
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)

    config_path = directory / PROJECT_CONFIG_NAME
    if config_path.exists() and not force:
        console.print(f"[yellow]![/yellow] {config_path} 已存在，未改动（加 --force 覆盖）")
    else:
        config_path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
        console.print(f"[green]✓[/green] 写入 {config_path}")

    for sub in ("outputs", "requests"):
        path = directory / sub
        path.mkdir(exist_ok=True)
        console.print(f"[green]✓[/green] 目录 {path}")

    gitignore = directory / ".gitignore"
    entries = ["outputs/", ".pixel-asset-cache/", ".env"]
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    missing = [e for e in entries if e not in existing]
    if missing:
        with gitignore.open("a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write("\n".join(missing) + "\n")
        console.print(f"[green]✓[/green] 追加 .gitignore 条目：{', '.join(missing)}")

    console.print()
    console.print("下一步：")
    console.print(
        f"  1. 设置 API Key 环境变量：[cyan]{API_KEY_ENV_VARS[0]}[/cyan]（切勿写进配置文件）"
    )
    console.print("  2. [cyan]pixel-asset doctor[/cyan] 检查环境")
    console.print("  3. [cyan]pixel-asset plan <request.yaml>[/cyan] 看清楚要生成什么")


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@app.command()
def doctor(
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", help="指定项目配置文件")
    ] = None,
    probe: Annotated[bool, typer.Option("--probe", help="探测 Provider 连通性")] = False,
) -> None:
    """检测配置、依赖与 API 连通性。永远不打印 Key 本身。"""
    ok = True
    table = Table(title="pixel-asset doctor", show_header=True, header_style="bold")
    table.add_column("检查项")
    table.add_column("状态", justify="center")
    table.add_column("详情")

    def row(name: str, good: bool, detail: str, *, warn: bool = False) -> None:
        nonlocal ok
        if warn:
            mark = "[yellow]![/yellow]"
        else:
            mark = "[green]✓[/green]" if good else "[red]✗[/red]"
            ok = ok and good
        table.add_row(name, mark, detail)

    row(
        "版本",
        True,
        f"pipeline {PIPELINE_VERSION} · schema: request {REQUEST_SCHEMA_VERSION}"
        f" · manifest {MANIFEST_SCHEMA_VERSION} · report {REPORT_SCHEMA_VERSION}",
    )
    row("Python", True, sys.version.split()[0])

    # 依赖
    for module, label in (
        ("PIL", "Pillow"), ("numpy", "NumPy"), ("yaml", "PyYAML"),
        ("jsonschema", "jsonschema"), ("pydantic", "Pydantic"), ("imagehash", "imagehash"),
    ):
        try:
            __import__(module)
            row(f"依赖 · {label}", True, "已安装")
        except ImportError:
            row(f"依赖 · {label}", False, "缺失 —— 运行 `uv sync`")

    try:
        __import__("openai")
        row("依赖 · openai", True, "已安装（真实 Provider 可用）")
    except ImportError:
        row("依赖 · openai", True, "未安装 —— 仅影响真实生成，离线命令不受影响", warn=True)

    # Schema
    try:
        directory = schema_dir()
        missing = [f for f in SCHEMA_FILES.values() if not (directory / f).exists()]
        row("JSON Schema", not missing, f"{directory}" + (f"（缺少 {missing}）" if missing else ""))
    except PixelAssetError as exc:
        row("JSON Schema", False, exc.message)

    # 配置
    try:
        config = load_config(project_config=config_path)
        found = config_path or find_project_config()
        row("配置来源", True, " → ".join(config.sources))
        row("项目配置", True, str(found) if found else "未找到（使用默认值）", warn=found is None)
        row("provider / model", True, f"{config.provider} / {config.model}")
        row("output_dir", True, str(config.output_dir.resolve()))
        row(
            "并发 / 重试 / 修复轮次",
            True,
            f"{config.max_concurrency} / {config.max_retries} / {config.max_repair_rounds}",
        )
    except PixelAssetError as exc:
        row("配置", False, exc.message)
        config = Config()

    # API Key —— 只报告来自哪个环境变量，绝不回显值
    key_var = Config.api_key_env_var()
    if key_var:
        row("API Key", True, f"已从环境变量 {key_var} 读取（值不显示）")
    else:
        row(
            "API Key",
            True,
            "未设置。离线命令（plan/process/validate/export）不受影响；"
            f"生成命令需要 {' 或 '.join(API_KEY_ENV_VARS)}",
            warn=True,
        )

    # 网格表自检 —— 常量被改坏时应当在这里就被抓住，而不是等 API 返回 400
    try:
        # 报的必须是**实际会用的**布局，不是多行表 —— 默认走单行条带，
        # doctor 却显示 3×2 的话，排障时会照着一个根本没发生的切分去找问题。
        details = []
        for f in ALLOWED_FRAME_COUNTS:
            lay = layout_for_frames(f)
            mark = "单行" if lay.rows == 1 else ""
            details.append(f"{f}帧→{lay.cols}×{lay.rows}{mark}")
        seed = seed_layout()
        row("网格档位", True, " · ".join(details) + f" · seed {seed.width}×{seed.height}")
    except PixelAssetError as exc:
        row("网格档位", False, exc.message)

    row(
        "验证阈值",
        True,
        "已校准" if THRESHOLDS_CALIBRATED else "未校准 —— 中低严重度告警可能误报，以人眼为准",
        warn=not THRESHOLDS_CALIBRATED,
    )

    if probe:
        try:
            from .providers import get_provider

            info = get_provider(config).probe()
            row(
                "Provider 连通性",
                bool(info.get("reachable")),
                json.dumps(info, ensure_ascii=False),
            )
        except PixelAssetError as exc:
            row("Provider 连通性", False, exc.message)

    console.print(table)
    if not ok:
        raise typer.Exit(EXIT_ERROR)


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


@app.command()
def plan(
    request_file: Annotated[Path, typer.Argument(help="Asset Request YAML")],
    as_json: Annotated[bool, typer.Option("--json", help="输出 JSON 而非表格")] = False,
    save: Annotated[bool, typer.Option("--save", help="把任务表写入 outputs/<asset_id>/")] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", help="指定项目配置文件")
    ] = None,
) -> None:
    """解析请求并输出任务 DAG 与预计 API 调用次数。不调用 API、不生成任何图。

    大批量生成之前先跑它 —— 不要在用户不知情的情况下发起批量生成。
    """
    config = _load_config(config_path)
    try:
        request = load_request(request_file)
        store = ArtifactStore.for_asset(config.output_dir, request.asset_id)
        existing = store.load_job_table() if save or store.job_table_path.exists() else None
        result = plan_request(request, existing=existing)
    except PixelAssetError as exc:
        _fail(exc)
        raise  # pragma: no cover

    if as_json:
        console.print_json(json.dumps(result.to_dict(), ensure_ascii=False))
    else:
        _render_plan(result, resumed=existing is not None)

    if save:
        store.ensure()
        store.save_request_copy(request_file)
        path = store.save_job_table(result.jobs)
        console.print(f"\n[green]✓[/green] 任务表已写入 {path}")


def _split_label(item: dict[str, Any]) -> str:
    """抽帧方式。components 是正常路径；退回 slots 说明连通域分不开。"""
    method = item["split_method"]
    if method == "components":
        extra = f" +{item['fragments']}碎片" if item["fragments"] else ""
        return f"[green]连通域{extra}[/green]"
    return f"[yellow]退回 {method}[/yellow]"


def _render_plan(result: Any, *, resumed: bool) -> None:
    request = result.request

    header = Table.grid(padding=(0, 2))
    header.add_column(style="bold")
    header.add_column()
    header.add_row("资产", f"{request.asset_id}（{request.asset_type}）")
    header.add_row("逻辑尺寸", f"{request.style.target_size[0]}×{request.style.target_size[1]}")
    header.add_row("调色板上限", str(request.style.max_colors))
    header.add_row("键控色", result.background.explain())
    mirror_target = "right" if request.mirror_source == "left" else "left"
    header.add_row(
        "镜像",
        f"启用（{request.mirror_source} → {mirror_target}）"
        if request.mirroring_enabled
        else "禁用（四方向独立生成）",
    )
    header.add_row("任务签名", result.jobs.signature())
    console.print(header)
    console.print()

    if request.animation_list():
        anim = Table(title="动作规划", header_style="bold")
        anim.add_column("动作")
        anim.add_column("帧数", justify="right")
        anim.add_column("fps", justify="right")
        anim.add_column("loop", justify="center")
        anim.add_column("网格")
        anim.add_column("物理尺寸")
        anim.add_column("生成方向")
        anim.add_column("镜像派生")
        for planned in result.animations:
            anim.add_row(
                planned.action,
                str(planned.frames),
                str(planned.fps),
                "✓" if planned.loop else "—",
                f"{planned.layout.cols}×{planned.layout.rows}",
                f"{planned.layout.width}×{planned.layout.height}",
                ", ".join(planned.generated) or "—",
                ", ".join(f"{d}←{s}" for d, s in planned.derived) or "—",
            )
        console.print(anim)
        console.print()

    jobs = Table(title="任务 DAG（拓扑序）", header_style="bold")
    jobs.add_column("#", justify="right")
    jobs.add_column("任务 ID")
    jobs.add_column("类型")
    jobs.add_column("状态")
    jobs.add_column("API", justify="center")
    jobs.add_column("依赖")
    for index, job in enumerate(result.jobs.topological_order(), start=1):
        kind_style = {
            JobKind.SEED: "[magenta]seed[/magenta]",
            JobKind.ANIMATION: "animation",
            JobKind.DERIVED: "[cyan]derived[/cyan]",
        }[job.kind]
        jobs.add_row(
            str(index),
            job.id,
            kind_style,
            job.status.value,
            "[yellow]✓[/yellow]" if job.calls_api else "—",
            ", ".join(job.depends_on) or "—",
        )
    console.print(jobs)

    console.print()
    summary = result.jobs.summary()
    console.print(
        f"[bold]共 {summary['total_jobs']} 个任务[/bold] · "
        f"预计 API 调用 [bold yellow]{result.estimated_api_calls}[/bold yellow] 次 · "
        f"镜像派生 {summary['derived_jobs']} 个（不计费）"
    )
    if resumed:
        console.print("[dim]已并入既有任务表：完成过的任务不会重跑。[/dim]")

    for warning in result.warnings:
        console.print(f"[yellow]![/yellow] {warning}")

    console.print()
    console.print(
        "[dim]下一步：create-character 产出 canonical seed → "
        "【人工闸门】确认 seed → create-animation → validate。[/dim]"
    )


# ---------------------------------------------------------------------------
# 尚未实现的命令（PLAN §6.1 的其余 6 个）
# ---------------------------------------------------------------------------


def _pending(feature: str, sprint: str) -> None:
    _fail(NotImplementedYetError(feature, sprint))


@app.command("create-character")
def create_character(
    request_file: Annotated[Path, typer.Argument(help="Asset Request YAML")],
    regenerate: Annotated[
        bool, typer.Option("--regenerate", help="重新生成 seed（旧图归档，级联作废下游）")
    ] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", help="指定项目配置文件")
    ] = None,
) -> None:
    """生成 canonical seed。调用 API。

    产出后停在**人工闸门**：seed 是所有动画的身份基准，
    它不对则后续动画全部作废重来。确认后用 create-animation --approve-seed 放行。
    """
    config = _load_config(config_path)
    try:
        result = run_create_character(request_file, config, regenerate=regenerate)
    except PixelAssetError as exc:
        _fail(exc)
        raise  # pragma: no cover

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("资产", result.asset_id)
    table.add_row("原图", str(result.seed_path))
    table.add_row("像素图", str(result.pixel_path))
    table.add_row(
        "尺寸",
        f"请求 {result.requested_size[0]}×{result.requested_size[1]} → "
        f"实际 {result.actual_size[0]}×{result.actual_size[1]}"
        + ("  [yellow]（端点吸附）[/yellow]" if result.size_snapped else ""),
    )
    table.add_row("键控色", f"{result.key_color}（阈值 {result.key_threshold:.0f}）")
    table.add_row("调色板", f"{len(result.palette)} 色")
    table.add_row("缓存", "命中（未计费）" if result.cached else "未命中")
    console.print(table)

    for warning in result.warnings:
        console.print(f"[yellow]![/yellow] {warning}")

    console.print()
    console.print("[bold yellow]【人工闸门】[/bold yellow] 请查看 seed 后再往下：")
    console.print(f"  {result.pixel_path}")
    console.print(
        "\n确认无误后放行：\n"
        f"  [cyan]pixel-asset create-animation --asset {result.asset_id} "
        f"--action <动作> --approve-seed[/cyan]"
    )


@app.command("import")
def import_assets(
    request_file: Annotated[Path, typer.Argument(help="Asset Request YAML")],
    source: Annotated[Path, typer.Argument(help="图片文件，或装着关键帧的目录")],
    as_: Annotated[
        str | None,
        typer.Option("--as", help="意图：seed（静态图→整套动作）| keyframes（关键帧→补帧）"),
    ] = None,
    action: Annotated[
        str, typer.Option("--action", help="--as keyframes 时这段关键帧属于哪个动作")
    ] = "idle",
    direction: Annotated[
        str, typer.Option("--direction", help="方向")
    ] = "down",
    source_fps: Annotated[
        int | None, typer.Option("--source-fps", help="关键帧序列本身的帧率")
    ] = None,
    target_fps: Annotated[
        int | None, typer.Option("--target-fps", help="想补到的帧率")
    ] = None,
    target_frames: Annotated[
        int | None,
        typer.Option("--target-frames", help="想补到的帧数（优先于 --target-fps 换算）"),
    ] = None,
    loop: Annotated[bool, typer.Option("--loop/--one-shot", help="是否循环动作")] = True,
    replace: Annotated[
        bool, typer.Option("--replace", help="替换已导入的素材（旧图归档）")
    ] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", help="指定项目配置文件")
    ] = None,
) -> None:
    """导入用户自己的像素资产。不调用 API。

    **必须显式声明意图。** 同一批文件对应两种完全不同的处理，
    光看文件数量分不出来，猜错的代价还不对称 —— 所以这里不猜。
    """
    if as_ is None:
        console.print(
            "[red]✗[/red]  必须用 --as 说明这批素材要做什么。"
            "同一批文件对应两种意图：\n"
        )
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold cyan")
        table.add_column()
        table.add_row("--as seed", "把它当角色身份基准，生成整套动作")
        table.add_row("", "[dim]适合：一张立绘、或几张同一角色的不同表情/变体[/dim]")
        table.add_row("", "[dim]产出：seed-pixel.png，之后 create-animation 生成各动作[/dim]")
        table.add_row("", "")
        table.add_row("--as keyframes", "把它当同一段动作的关键帧，补出中间帧")
        table.add_row("", "[dim]适合：手上已有一段动作的两三张关键帧[/dim]")
        table.add_row("", "[dim]产出：原帧原样保留 + 补到目标帧率[/dim]")
        console.print(table)
        console.print(
            "\n[dim]分不清就问用户 —— 三张图可能是「笑 / 平 / 晕」三个独立状态"
            "（seed），也可能是一段动作的三个关键帧（keyframes）。猜错要么白花"
            "整套动作的生成调用，要么补出一堆「半笑半晕」的废帧。[/dim]"
        )
        raise typer.Exit(EXIT_ERROR)

    if as_ not in ("seed", "keyframes"):
        console.print(f"[red]✗[/red]  --as 只接受 seed 或 keyframes，收到 {as_!r}")
        raise typer.Exit(EXIT_ERROR)

    config = _load_config(config_path)

    if as_ == "seed":
        try:
            seed = run_import_seed(request_file, source, config, replace=replace)
        except PixelAssetError as exc:
            _fail(exc)
            raise  # pragma: no cover

        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold")
        table.add_column()
        table.add_row("资产", seed.asset_id)
        table.add_row("原图", str(seed.seed_path))
        table.add_row("像素图", str(seed.pixel_path))
        table.add_row("素材尺寸", f"{seed.source_size[0]}×{seed.source_size[1]}")
        table.add_row("键控色", seed.key_color)
        table.add_row("调色板", f"{len(seed.palette)} 色")
        console.print(table)
        for warning in seed.warnings:
            console.print(f"[yellow]![/yellow] {warning}")

        console.print()
        console.print("[bold yellow]【人工闸门】[/bold yellow] 请查看 seed 后再往下：")
        console.print(f"  {seed.pixel_path}")
        console.print(
            "\n确认无误后放行：\n"
            f"  [cyan]pixel-asset create-animation --asset {seed.asset_id} "
            f"--action <动作> --approve-seed[/cyan]"
        )
        return

    try:
        imported = run_import_keyframes(
            request_file, source, config,
            action=action, direction=direction,
            source_fps=source_fps, target_fps=target_fps,
            target_frames=target_frames, loop=loop,
        )
    except PixelAssetError as exc:
        _fail(exc)
        raise  # pragma: no cover

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("资产", imported.asset_id)
    table.add_row("动作", imported.key)
    table.add_row("关键帧", f"{len(imported.keyframe_paths)} 张 → " +
                  ", ".join(p.name for p in imported.keyframe_paths))
    table.add_row("画布", f"{imported.canvas[0]}×{imported.canvas[1]}")
    table.add_row("调色板", f"{len(imported.palette)} 色（各帧共用）")
    table.add_row("锚点漂移", f"{imported.anchor_drift_px:.2f}px")
    if imported.budget is not None:
        table.add_row("补间预算", imported.budget.describe())
    console.print(table)
    for warning in imported.warnings:
        console.print(f"[yellow]![/yellow] {warning}")


@app.command("interpolate")
def interpolate(
    asset_dir: Annotated[Path, typer.Argument(help="资产目录")],
    key: Annotated[str, typer.Option("--key", help="动作键，如 react_down")],
    target_fps: Annotated[
        int | None, typer.Option("--target-fps", help="想补到的帧率")
    ] = None,
    target_frames: Annotated[
        int | None, typer.Option("--target-frames", help="想补到的帧数")
    ] = None,
    regenerate: Annotated[
        bool, typer.Option("--regenerate", help="绕开缓存重新生成中间帧")
    ] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", help="指定项目配置文件")
    ] = None,
) -> None:
    """把已导入的关键帧补到目标帧率。调用 API。

    关键帧原样保留，只补中间帧；中间帧的颜色锁死到关键帧的调色板。
    每个间隔一次调用画完 —— 逐帧调用会让身份漂移。
    """
    config = _load_config(config_path)
    try:
        result = run_interpolate(
            asset_dir, key=key, config=config,
            target_fps=target_fps, target_frames=target_frames,
            regenerate=regenerate,
        )
    except PixelAssetError as exc:
        _fail(exc)
        raise  # pragma: no cover

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("资产", result.asset_id)
    table.add_row("动作", result.key)
    table.add_row("预算", result.budget.describe())
    table.add_row("API 调用", str(result.api_calls))
    table.add_row("调色板", f"{len(result.palette)} 色（锁死到关键帧）")
    table.add_row("最大色偏", f"{result.max_palette_drift:.0f}")
    table.add_row("锚点漂移", f"{result.anchor_drift_px:.2f}px")
    console.print(table)

    for warning in result.warnings:
        console.print(f"[yellow]![/yellow] {warning}")

    console.print()
    console.print("[dim]帧序（key=关键帧原样保留，gap=补出来的）：[/dim]")
    console.print("[dim]  " + " ".join(result.order) + "[/dim]")
    console.print(
        "\n[dim]帧序错乱无法自动检测 —— 请看 previews/"
        f"{result.key}.gif 确认播放顺序。[/dim]"
    )


@app.command("create-animation")
def create_animation(
    asset: Annotated[str, typer.Option("--asset", help="asset_id")],
    action: Annotated[str, typer.Option("--action", help="动作名，如 walk")],
    direction: Annotated[
        str | None, typer.Option("--direction", help="方向，如 down；无方向资产可省略")
    ] = "down",
    approve_seed_flag: Annotated[
        bool, typer.Option("--approve-seed", help="先记录人工批准，再生成")
    ] = False,
    regenerate: Annotated[
        bool, typer.Option("--regenerate", help="重新生成该动作（旧图归档）")
    ] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", help="指定项目配置文件")
    ] = None,
) -> None:
    """一次生成完整动作网格。调用 API。**绝不逐帧生成。**"""
    config = _load_config(config_path)
    asset_dir = config.asset_dir(asset)

    try:
        if approve_seed_flag:
            asset_id, unlocked = run_approve_seed(asset_dir)
            console.print(
                f"[green]✓[/green] {asset_id} 的 seed 已批准，解锁 {unlocked} 个动画任务"
            )

        result = run_create_animation(
            asset_dir,
            action=action,
            direction=None if direction in (None, "", "none") else direction,  # type: ignore[arg-type]
            config=config,
            regenerate=regenerate,
        )
    except PixelAssetError as exc:
        _fail(exc)
        raise  # pragma: no cover

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("动作", result.key)
    table.add_row("帧数", f"{result.frames} 帧 · {result.frame_size[0]}×{result.frame_size[1]}")
    if result.derived_from:
        table.add_row("来源", f"由 {result.derived_from} 翻转派生（未调用 API）")
    else:
        table.add_row(
            "尺寸",
            f"请求 {result.requested_size[0]}×{result.requested_size[1]} → "
            f"实际 {result.actual_size[0]}×{result.actual_size[1]}",
        )
        table.add_row("键控阈值", f"{result.key_threshold:.0f}")
        overflow = (
            "[green]干净[/green]"
            if result.overflow_clean
            else f"[red]{result.overflow_summary}[/red]"
        )
        table.add_row("越界", overflow)
    table.add_row("锚点漂移", f"{result.anchor_drift:.1f}px")
    table.add_row("缓存", "命中（未计费）" if result.cached else "未命中")
    console.print(table)

    for warning in result.warnings:
        console.print(f"[yellow]![/yellow] {warning}")

    pending = next_pending(ArtifactStore(root=asset_dir))
    if pending:
        console.print(f"\n[dim]还有 {len(pending)} 个动画任务可跑：{', '.join(pending[:4])}"
                      + (" …" if len(pending) > 4 else "") + "[/dim]")
    console.print("\n[dim]下一步：validate。API 返回成功 ≠ 资产合格。[/dim]")


@app.command()
def process(
    asset_dir: Annotated[Path, typer.Argument(help="outputs/<asset_id>")],
    only: Annotated[
        str | None, typer.Option("--only", help="只处理某个动作，如 walk_down")
    ] = None,
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", help="指定项目配置文件")
    ] = None,
) -> None:
    """仅重跑本地确定性处理链。**不调用 API。**

    原始生成图永不覆盖，所以调参、改阈值、换调色板都可以在这里反复重跑，
    一分钱都不用花。
    """
    _load_config(config_path)
    try:
        summary = run_process(asset_dir, only=only)
    except PixelAssetError as exc:
        _fail(exc)
        raise  # pragma: no cover

    if not summary:
        err_console.print(f"[yellow]![/yellow] {asset_dir}/source 下没有可处理的原图")
        raise typer.Exit(EXIT_ERROR)

    table = Table(title="处理结果", header_style="bold")
    table.add_column("动作")
    table.add_column("帧数", justify="right")
    table.add_column("帧尺寸")
    table.add_column("阈值", justify="right")
    table.add_column("背景", justify="right")
    table.add_column("色数", justify="right")
    table.add_column("锚点漂移", justify="right")
    table.add_column("抽帧")

    for item in summary:
        table.add_row(
            item["key"],
            str(item["frames"]),
            item["frame_size"],
            f"{item['threshold']:.0f}",
            f"{item['background_ratio']:.0%}",
            str(item["colors"]),
            f"{item['anchor_drift']:.1f}px",
            _split_label(item),
        )
    console.print(table)

    for item in summary:
        for warning in item["warnings"]:
            console.print(f"[yellow]![/yellow] {item['key']}：{warning}")

    console.print(
        "\n[dim]下一步：validate 检查产出质量。API 返回成功 ≠ 资产合格。[/dim]"
    )


@app.command()
def validate(
    asset_dir: Annotated[Path, typer.Argument(help="outputs/<asset_id>")],
    as_json: Annotated[bool, typer.Option("--json", help="输出 JSON")] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", help="指定项目配置文件")
    ] = None,
) -> None:
    """运行验证引擎。不调用 API。

    **API 返回成功 ≠ 资产合格。** 存在 fatal / high 级失败时退出码非零。
    """
    config = _load_config(config_path)
    try:
        report = run_validation(asset_dir)
        report.save(Path(asset_dir) / VALIDATION_REPORT_FILE)
        plan = plan_repairs(
            report,
            rounds_used=repair_rounds_used(asset_dir),
            max_rounds=config.max_repair_rounds,
        )
        if not plan.empty:
            _write_repair_plan(Path(asset_dir), plan)
    except PixelAssetError as exc:
        _fail(exc)
        raise  # pragma: no cover
    except FileNotFoundError as exc:
        err_console.print(f"[red]✗[/red] 找不到文件：{exc}")
        raise typer.Exit(EXIT_ERROR) from exc

    if as_json:
        console.print_json(json.dumps(report.to_dict(), ensure_ascii=False))
    else:
        _render_validation(report, plan, Path(asset_dir))

    if not report.passed:
        raise typer.Exit(EXIT_VALIDATION_FAILED)


def _write_repair_plan(asset_dir: Path, plan: Any) -> Path:
    path = asset_dir / REPAIR_PLAN_FILE
    path.write_text(
        json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def _render_validation(report: Any, plan: Any, asset_dir: Path) -> None:
    table = Table(title=f"验证报告 · {report.asset_id}", header_style="bold")
    table.add_column("动作")
    table.add_column("检查项")
    table.add_column("严重度", justify="center")
    table.add_column("结果", justify="center")
    table.add_column("实测 / 阈值")

    marks = {
        "pass": "[green]✓[/green]",
        "fail": "[red]✗[/red]",
        "warn": "[yellow]![/yellow]",
        "skip": "[dim]—[/dim]",
    }
    for check in report.checks:
        if check.result.value == "pass":
            continue  # 只列需要注意的项，通过的项在汇总里给计数
        measured = "—" if check.measured is None else f"{check.measured}"
        threshold = "" if check.threshold is None else f" / {check.threshold}"
        table.add_row(
            check.target,
            check.id,
            check.severity.value,
            marks[check.result.value],
            measured + threshold,
        )
    if table.row_count:
        console.print(table)

    summary = report.summary()
    verdict = (
        "[bold green]通过[/bold green]" if report.passed else "[bold red]未通过[/bold red]"
    )
    console.print(
        f"\n{verdict} · 共 {summary['total']} 项："
        f"通过 {summary['passed']} · 失败 {summary['failed']} · "
        f"警告 {summary['warnings']} · 跳过 {summary['skipped']}"
    )

    for check in report.blocking_checks:
        console.print(f"[red]✗[/red] {check.target} / {check.id}：{check.message or ''}")

    if not report.thresholds_calibrated:
        console.print(
            "[yellow]![/yellow] per-action 阈值尚未用真实数据校准，"
            "中低严重度告警可能是误报 —— 以人眼判断为准（PLAN §9.1）"
        )

    skipped_order = [c for c in report.checks if c.id == "frame_order_continuity"]
    if skipped_order:
        console.print(
            "[yellow]![/yellow] 帧序是否被打乱**无法自动检测**（实测判据不可区分）。"
            f"请人眼查看 {asset_dir / 'previews'} 下的 GIF。"
        )

    if not plan.empty:
        console.print(f"\n[bold]修复计划[/bold]（已写入 {REPAIR_PLAN_FILE}）：")
        for step in plan.steps:
            cost = "[yellow]调用 API[/yellow]" if step.calls_api else "[green]离线[/green]"
            console.print(f"  · {step.target} → {step.action.value}（{cost}）：{step.detail}")
        console.print(
            f"\n执行：[cyan]pixel-asset repair {asset_dir}[/cyan]"
            + ("  [dim]（需重生成的步骤要加 --allow-api）[/dim]" if plan.api_calls else "")
        )


@app.command()
def repair(
    asset_dir: Annotated[Path, typer.Argument(help="outputs/<asset_id>")],
    allow_api: Annotated[
        bool, typer.Option("--allow-api", help="允许执行需要重生成的步骤（会计费）")
    ] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", help="指定项目配置文件")
    ] = None,
) -> None:
    """执行修复计划。

    默认**只跑离线修复** —— 花钱的重生成必须由 ``--allow-api`` 显式同意。
    """
    config = _load_config(config_path)
    try:
        report = run_validation(asset_dir)
        plan = plan_repairs(
            report,
            rounds_used=repair_rounds_used(asset_dir),
            max_rounds=config.max_repair_rounds,
        )
        if plan.empty:
            console.print("[green]✓[/green] 没有需要修复的失败项")
            return
        outcomes = execute_repairs(asset_dir, plan, config, allow_api=allow_api)
    except PixelAssetError as exc:
        _fail(exc)
        raise  # pragma: no cover

    table = Table(title="修复结果", header_style="bold")
    table.add_column("目标")
    table.add_column("动作")
    table.add_column("API", justify="center")
    table.add_column("执行", justify="center")
    table.add_column("说明")
    for outcome in outcomes:
        table.add_row(
            outcome.step.target,
            outcome.step.action.value,
            "[yellow]✓[/yellow]" if outcome.step.calls_api else "—",
            "[green]✓[/green]" if outcome.performed else "[dim]跳过[/dim]",
            outcome.detail,
        )
    console.print(table)
    console.print(
        f"\n[dim]第 {plan.rounds_used + 1}/{plan.max_rounds} 轮。"
        f"修复后请重新 validate 确认。[/dim]"
    )


@app.command()
def export(
    asset_dir: Annotated[Path, typer.Argument(help="outputs/<asset_id>")],
    target: Annotated[
        list[str] | None,
        typer.Option("--target", "-t", help="generic-json / godot，可重复"),
    ] = None,
    no_contact_sheet: Annotated[
        bool, typer.Option("--no-contact-sheet", help="不生成 contact sheet")
    ] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", help="指定项目配置文件")
    ] = None,
) -> None:
    """导出引擎格式并生成 Contact Sheet。**不调用 API。**"""
    _load_config(config_path)
    targets = list(target) if target else ["generic-json", "godot"]
    try:
        summary = run_export(
            asset_dir, targets=targets, contact_sheet=not no_contact_sheet
        )
    except PixelAssetError as exc:
        _fail(exc)
        raise  # pragma: no cover

    table = Table(title=f"导出 · {summary.asset_id}", header_style="bold")
    table.add_column("文件")
    table.add_column("大小", justify="right")
    for path in summary.files:
        table.add_row(
            str(path.relative_to(Path(asset_dir))), f"{path.stat().st_size / 1024:.1f} KB"
        )
    if summary.contact_sheet:
        table.add_row(
            str(summary.contact_sheet.relative_to(Path(asset_dir))),
            f"{summary.contact_sheet.stat().st_size / 1024:.1f} KB",
        )
    console.print(table)

    for note in summary.notes:
        console.print(f"[yellow]·[/yellow] {note}")


@app.callback()
def main(
    log_level: Annotated[str, typer.Option("--log-level", help="日志级别")] = "INFO",
    json_logs: Annotated[bool, typer.Option("--json-logs", help="结构化 JSON 日志")] = False,
) -> None:
    configure_logging(log_level, json_output=json_logs)


if __name__ == "__main__":  # pragma: no cover
    app()
