"""``pixel-asset`` 命令行。

13 个命令中有 6 个完全不调用 API（``init`` / ``plan`` / ``process`` / ``validate`` /
``export`` / ``import``）
—— 这是刻意设计：调试与迭代应尽量在离线侧完成（PLAN §6.1）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
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
    PROJECT_ENV_NAME,
    Config,
    _read_yaml,
    find_project_config,
    load_config,
)
from .constants import (
    ALLOWED_FRAME_COUNTS,
    REPAIR_PLAN_FILE,
    THRESHOLDS_CALIBRATED,
)
from .errors import (
    NotImplementedYetError,
    PixelAssetError,
    PlanError,
    RequestValidationError,
)
from .logging_utils import configure_logging
from .models.job import JobKind, JobStatus
from .models.pack import load_pack
from .models.request import STATIC_ASSET_TYPES, AssetRequest, load_request
from .pipelines import approve_seed as run_approve_seed
from .pipelines import create_animation as run_create_animation
from .pipelines import create_character as run_create_character
from .pipelines import import_keyframes as run_import_keyframes
from .pipelines import import_seed as run_import_seed
from .pipelines import next_pending, run_export, run_interpolate, run_process
from .pipelines.asset_pack import PackRunControl, run_asset_pack
from .pipelines.static_asset import create_static_asset as run_create_static_asset
from .pipelines.static_asset import validate_and_export_static_asset
from .pipelines.tilemap import create_map as run_create_map
from .pipelines.tileset import create_tileset as run_create_tileset
from .pipelines.validation import run_validation
from .planning import layout_for_frames, plan_pack, plan_request, seed_layout
from .repair import execute_plan as execute_repairs
from .repair import plan_repairs
from .repair import rounds_used as repair_rounds_used
from .schema_registry import SCHEMA_FILES, schema_dir
from .storage import ArtifactStore

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


def _next_step_hint(request: AssetRequest) -> str:
    """`plan` 之后该跑哪条命令。

    以前无论什么资产都指向 `create-character` —— 对静态资产与 tileset 都是错的，
    tileset 照做只会拿到一个"角色"。
    """
    if request.tileset is not None:
        return "create-tileset 生成整套 tile → validate 查无缝平铺 → export。"
    if request.asset_type in STATIC_ASSET_TYPES and not request.animation_list():
        return "create-asset 一条命令跑完生成 → 处理 → 验证 → 导出。"
    return (
        "create-character 产出 canonical seed → 【人工闸门】确认 seed → "
        "create-animation → validate。"
    )


def _load_config(
    config_path: Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
) -> Config:
    try:
        return load_config(project_config=config_path, overrides=overrides)
    except PixelAssetError as exc:
        _fail(exc)
        raise  # pragma: no cover - _fail 永远抛出


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def _yaml_config_line(field: str, value: str) -> str:
    """把单个字符串值交给 YAML 库转义，避免用户输入破坏模板。"""
    return yaml.safe_dump({field: value}, allow_unicode=True, sort_keys=False).strip()


def _render_config_template(*, base_url: str, model: str) -> str:
    """在保留默认模板注释结构的前提下写入交互项。"""
    rendered = DEFAULT_CONFIG_TEMPLATE.replace(
        "model: gpt-image-2", _yaml_config_line("model", model), 1
    )
    if base_url:
        rendered = rendered.replace("# base_url:", _yaml_config_line("base_url", base_url), 1)
    return rendered


def _update_config_text(text: str, *, base_url: str, model: str) -> str:
    """对已存在的配置只做 ``model`` / ``base_url`` 的行级替换。

    交互式 init 重跑不该重置用户调过的其他字段（``max_concurrency`` /
    ``output_dir`` 等）—— 整体重写模板留给显式 ``--force``。
    """
    lines = text.splitlines(keepends=True)

    model_line = _yaml_config_line("model", model) + "\n"
    for i, line in enumerate(lines):
        if re.match(r"model\s*:", line):
            lines[i] = model_line
            break
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(model_line)

    base_line = _yaml_config_line("base_url", base_url) + "\n" if base_url else "# base_url:\n"
    for i, line in enumerate(lines):
        if re.match(r"(#\s*)?base_url\s*:", line):
            lines[i] = base_line
            break
    else:
        if base_url:
            lines.append(base_line)

    return "".join(lines)


def _prompt_base_url(default: str) -> str:
    """询问并规范化 OpenAI 兼容端点。"""
    while True:
        value = str(
            typer.prompt(
                "端点服务器 base_url（留空使用官方 OpenAI 端点）",
                default=default,
                show_default=bool(default),
            )
        ).strip()
        if not value:
            return ""
        if value.startswith(("http://", "https://")):
            return value.rstrip("/")
        console.print("[red]✗[/red] base_url 必须以 http:// 或 https:// 开头，请重新输入")


def _write_project_api_key(path: Path, key: str) -> None:
    """只更新项目 ``.env`` 的 Key 行，并把文件权限收紧到 0600。

    权限位只在 POSIX 上生效；Windows 没有这套语义，``chmod`` 调用无害但
    不产生 0600，访问控制交给 NTFS 默认 ACL。提示语与测试都按平台如实处理。
    """
    if path.exists():
        path.chmod(0o600)
        original = path.read_text(encoding="utf-8")
    else:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        original = ""

    replacement = f"{API_KEY_ENV_VARS[0]}={key}\n"
    lines: list[str] = []
    replaced = False
    for line in original.splitlines(keepends=True):
        stripped = line.lstrip()
        name = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if name == API_KEY_ENV_VARS[0]:
            if not replaced:
                lines.append(replacement)
                replaced = True
            continue
        lines.append(line)

    if not replaced:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines.append("\n")
        lines.append(replacement)
    path.write_text("".join(lines), encoding="utf-8")
    path.chmod(0o600)


def _probe_provider(config: Config) -> tuple[bool, str]:
    """执行 doctor 与 init 共用的 Provider 连通性探测。"""
    try:
        from .providers import get_provider

        info = get_provider(config).probe()
        return bool(info.get("reachable")), json.dumps(info, ensure_ascii=False)
    except PixelAssetError as exc:
        return False, exc.message


@app.command()
def init(
    directory: Annotated[Path, typer.Argument(help="项目目录，默认当前目录")] = Path(),
    force: Annotated[bool, typer.Option("--force", help="覆盖已存在的配置文件")] = False,
    interactive: Annotated[
        bool | None,
        typer.Option(
            "--interactive/--no-interactive",
            help="启用或关闭交互配置；默认仅在 stdin 是 TTY 时启用",
        ),
    ] = None,
) -> None:
    """初始化项目配置与输出目录。不调用 API。"""
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)

    config_path = directory / PROJECT_CONFIG_NAME
    interactive_mode = sys.stdin.isatty() if interactive is None else interactive
    api_key = ""

    if interactive_mode:
        existing_config: dict[str, Any] = {}
        if config_path.exists() and not force:
            try:
                existing_config = _read_yaml(config_path)
            except PixelAssetError as exc:
                _fail(exc)

        base_url_default = str(existing_config.get("base_url") or "")
        model_default = str(
            existing_config.get("model") or Config.model_fields["model"].default
        )
        base_url = _prompt_base_url(base_url_default)
        api_key = str(
            typer.prompt(
                "API Key（留空跳过）",
                default="",
                hide_input=True,
                show_default=False,
            )
        ).strip()
        model = str(typer.prompt("图片模型名", default=model_default)).strip() or model_default

        if config_path.exists() and not force:
            updated = _update_config_text(
                config_path.read_text(encoding="utf-8"), base_url=base_url, model=model
            )
        else:
            updated = _render_config_template(base_url=base_url, model=model)
        config_path.write_text(updated, encoding="utf-8")
        console.print(f"[green]✓[/green] 写入 {config_path}")
        if api_key:
            env_path = directory / PROJECT_ENV_NAME
            _write_project_api_key(env_path, api_key)
            perm_note = "权限 0600，" if os.name == "posix" else ""
            console.print(f"[green]✓[/green] API Key 已写入 {env_path}（{perm_note}值不显示）")
    elif config_path.exists() and not force:
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
    existing_gitignore = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    missing = [e for e in entries if e not in existing_gitignore]
    if missing:
        with gitignore.open("a", encoding="utf-8") as fh:
            if existing_gitignore and not existing_gitignore.endswith("\n"):
                fh.write("\n")
            fh.write("\n".join(missing) + "\n")
        console.print(f"[green]✓[/green] 追加 .gitignore 条目：{', '.join(missing)}")

    if interactive_mode:
        if api_key and typer.confirm("是否立即探测连通性?", default=False):
            config = _load_config(config_path)
            good, detail = _probe_provider(config)
            mark = "[green]✓[/green]" if good else "[red]✗[/red]"
            console.print(f"{mark} Provider 连通性：{detail}")
            if not good:
                raise typer.Exit(EXIT_ERROR)
        else:
            console.print("\n下一步运行：[cyan]pixel-asset doctor --probe[/cyan]")
        console.print("然后运行：[cyan]pixel-asset plan <request.yaml>[/cyan] 看清楚要生成什么")
    else:
        console.print()
        console.print("下一步：")
        console.print(
            f"  1. 在项目 .env 或环境变量中设置 [cyan]{API_KEY_ENV_VARS[0]}[/cyan]"
            "（切勿写进配置文件）"
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

    # API Key —— 只报告来源，绝不回显值
    key_source = config.resolved_api_key_source()
    if key_source:
        row("API Key", True, f"来源：{key_source}（值不显示）")
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
        good, detail = _probe_provider(config)
        row("Provider 连通性", good, detail)

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
    model: Annotated[
        str | None, typer.Option("--model", help="覆盖有效生成模型")
    ] = None,
) -> None:
    """解析请求并输出任务 DAG 与预计 API 调用次数。不调用 API、不生成任何图。

    大批量生成之前先跑它 —— 不要在用户不知情的情况下发起批量生成。
    """
    overrides = {"model": model} if model is not None else None
    config = _load_config(config_path, overrides=overrides)
    try:
        raw = yaml.safe_load(request_file.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "pack_type" in raw:
            pack = load_pack(request_file)
            existing_by_asset = {
                request.asset_id: table
                for request in pack.expand_requests()
                if (table := ArtifactStore.for_asset(
                    config.output_dir, request.asset_id
                ).load_job_table())
                is not None
            }
            pack_plan = plan_pack(
                pack,
                existing=existing_by_asset,
                provider=config.provider,
                model=config.model,
            )
            if as_json:
                console.print_json(json.dumps(pack_plan.to_dict(), ensure_ascii=False))
            else:
                pack_table = Table(
                    title=f"Pack 规划 · {pack.pack_id}",
                    header_style="bold",
                )
                pack_table.add_column("资产")
                pack_table.add_column("任务", justify="right")
                animated_pack = pack_plan.estimated_seed_api_calls > 0
                if animated_pack:
                    pack_table.add_column("Seed API", justify="right")
                    pack_table.add_column("动画 API", justify="right")
                pack_table.add_column("API 合计", justify="right")
                pack_table.add_column("恢复", justify="center")
                for asset in pack_plan.assets:
                    row = [asset.request.asset_id, str(len(asset.jobs))]
                    if animated_pack:
                        row.extend(
                            [
                                str(
                                    sum(
                                        1
                                        for job in asset.jobs.of_kind(JobKind.SEED)
                                        if job.status
                                        in (JobStatus.PLANNED, JobStatus.GENERATING)
                                    )
                                ),
                                str(
                                    sum(
                                        1
                                        for job in asset.jobs.of_kind(JobKind.ANIMATION)
                                        if job.status
                                        in (JobStatus.PLANNED, JobStatus.GENERATING)
                                    )
                                ),
                            ]
                        )
                    row.extend(
                        [
                            str(asset.estimated_api_calls),
                            "✓" if asset.request.asset_id in existing_by_asset else "—",
                        ]
                    )
                    pack_table.add_row(*row)
                console.print(pack_table)
                cost = (
                    f"预计 seed API 调用 {pack_plan.estimated_seed_api_calls} · "
                    f"预计动画 API 调用 {pack_plan.estimated_animation_api_calls} · "
                    if animated_pack
                    else ""
                )
                console.print(
                    f"[dim]总资产 {len(pack_plan.assets)} · "
                    f"总任务 {pack_plan.total_jobs} · "
                    f"{cost}预计 API 调用 {pack_plan.estimated_api_calls} · "
                    f"有效模型 {config.provider}/{config.model}[/dim]"
                )
            if save:
                for asset in pack_plan.assets:
                    store = ArtifactStore.for_asset(
                        config.output_dir, asset.request.asset_id
                    ).ensure()
                    store.save_job_table(asset.jobs)
                console.print("\n[green]✓[/green] 各资产任务表已写入输出目录")
            return

        request = load_request(request_file)
        store = ArtifactStore.for_asset(config.output_dir, request.asset_id)
        existing = store.load_job_table() if save or store.job_table_path.exists() else None
        request_plan = plan_request(
            request,
            existing=existing,
            provider=config.provider,
            model=config.model,
        )
    except PixelAssetError as exc:
        _fail(exc)
        raise  # pragma: no cover
    except ValueError as exc:
        _fail(PlanError(str(exc)))
        raise  # pragma: no cover

    if as_json:
        console.print_json(json.dumps(request_plan.to_dict(), ensure_ascii=False))
    else:
        _render_plan(request_plan, resumed=existing is not None)

    if save:
        store.ensure()
        store.save_request_copy(request_file)
        path = store.save_job_table(request_plan.jobs)
        console.print(f"\n[green]✓[/green] 任务表已写入 {path}")


@app.command("create-asset")
def create_asset(
    request_file: Annotated[Path, typer.Argument(help="静态 Asset Request YAML")],
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", help="指定项目配置文件")
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="覆盖有效生成模型")
    ] = None,
) -> None:
    """单个静态资产完整链：生成 → 处理 → 验证 → 导出。

    单资产一次 API 调用，无 plan 前置；批量请用 plan + create-asset-pack。
    动画请求请使用 create-character。
    """
    overrides = {"model": model} if model is not None else None
    config = _load_config(config_path, overrides=overrides)
    try:
        request = load_request(request_file)
        if request.asset_type not in STATIC_ASSET_TYPES or request.animation_list():
            allowed = ", ".join(sorted(STATIC_ASSET_TYPES))
            raise RequestValidationError(
                f"{request_file}：create-asset 只接受无 animations 的静态资产类型："
                f"{allowed}。动画请求请使用 `pixel-asset create-character <request.yaml>`。"
            )
        result = run_create_static_asset(request_file, config)
        completion = validate_and_export_static_asset(
            config.asset_dir(request.asset_id),
            targets=request.export.targets,
        )
    except PixelAssetError as exc:
        _fail(exc)
        raise  # pragma: no cover

    if not completion.passed:
        err_console.print(
            f"[red]✗[/red] {request.asset_id} 验证未通过；未导出，"
            f"详见 {config.asset_dir(request.asset_id) / 'validation-report.json'}"
        )
        raise typer.Exit(EXIT_VALIDATION_FAILED)

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("资产", result.asset_id)
    table.add_row("原图", str(result.source_path))
    table.add_row("像素图", str(result.image_path))
    table.add_row("验证", "通过")
    if completion.export is not None:
        table.add_row("导出", f"{len(completion.export.files)} 个文件")
        table.add_row("目录", str(config.asset_dir(request.asset_id) / "exports"))
    table.add_row("缓存", "命中（未计费）" if result.cached else "未命中")
    console.print(table)
    for warning in result.warnings:
        console.print(f"[yellow]![/yellow] {warning}")


@app.command("create-tileset")
def create_tileset_command(
    request_file: Annotated[Path, typer.Argument(help="tileset Asset Request YAML")],
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", help="指定项目配置文件")
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="覆盖有效生成模型")
    ] = None,
    regenerate: Annotated[
        bool, typer.Option("--regenerate", help="重新生成全部 tile（旧图归档）")
    ] = False,
) -> None:
    """生成一整套 tile：逐块生成 → 整套统一处理，停在 processed 等验证。

    每块 tile 一次 API 调用。整套共用一份调色板，所以处理必须等全部原图齐了
    再一起做 —— 中途失败重跑同一条命令即可，已取回的 tile 不会重复计费。
    """
    overrides = {"model": model} if model is not None else None
    config = _load_config(config_path, overrides=overrides)
    try:
        request = load_request(request_file)
        if request.tileset is None:
            raise RequestValidationError(
                f"{request_file}：create-tileset 只接受 asset_type: tileset 的请求。"
                f"单张静态资产请用 `pixel-asset create-asset <request.yaml>`。"
            )
        result = run_create_tileset(request_file, config, regenerate=regenerate)
    except PixelAssetError as exc:
        _fail(exc)
        raise  # pragma: no cover

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("资产", result.asset_id)
    table.add_row("tile", f"{len(result.tile_ids)} 块 · {'×'.join(map(str, result.tile_size))}")
    table.add_row("共享色板", f"{len(result.palette)} 色")
    table.add_row("本次调用", f"{result.generated} 次")
    table.add_row("目录", str(config.asset_dir(result.asset_id) / "frames" / "tiles"))
    console.print(table)
    console.print(
        "\n[dim]下一步：validate 查无缝平铺。API 返回成功 ≠ tile 拼得起来。[/dim]"
    )


@app.command("create-map")
def create_map_command(
    asset_dir: Annotated[Path, typer.Argument(help="已处理好的 tileset 资产目录")],
    width: Annotated[int, typer.Option("--width", "-w", help="地图宽（格）")] = 24,
    height: Annotated[int, typer.Option("--height", "-h", help="地图高（格）")] = 16,
    seed: Annotated[int, typer.Option("--seed", help="随机种子。同 seed 同地图")] = 0,
    name: Annotated[str, typer.Option("--name", help="地图名")] = "overworld",
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", help="指定项目配置文件")
    ] = None,
) -> None:
    """按邻接表铺一张地图（WFC）。**不调用 API。**

    同 seed + 同邻接表 + 同尺寸 → 同一张地图。撞上矛盾会换 seed 重试，
    仍然不行就报错 —— 绝不交一张有非法接缝的半成品。
    """
    _load_config(config_path)
    try:
        result = run_create_map(
            asset_dir, name=name, width=width, height=height, seed=seed
        )
    except PixelAssetError as exc:
        _fail(exc)
        raise  # pragma: no cover

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("地图", f"{result.name} · {result.width}×{result.height} 格")
    table.add_row("种子", str(result.seed))
    table.add_row("用到 tile", f"{len(result.tiles_used)} 种：{'、'.join(result.tiles_used)}")
    table.add_row("文件", str(result.path))
    console.print(table)

    if result.single_material:
        console.print(
            "\n[yellow]![/yellow] 整张地图只有一种材质。这是[bold]正确结果[/bold]："
            "这套 tile 的邻接表是对角矩阵（材质之间接不上），而地图网格是连通的，"
            "\n  于是整张图必然同一种材质。要铺出多材质地图，缺的是[bold]过渡 tile[/bold]，"
            "不是更好的求解器。"
        )
    console.print("\n[dim]下一步：validate 查地图里有没有非法接缝。[/dim]")


@app.command("create-asset-pack")
def create_asset_pack(
    pack_file: Annotated[Path, typer.Argument(help="资产 Pack YAML")],
    as_json: Annotated[bool, typer.Option("--json", help="输出 JSON 汇总")] = False,
    retry_failed: Annotated[
        bool, typer.Option("--retry-failed", help="重试任务表中处于 failed 的资产")
    ] = False,
    config_path: Annotated[
        Path | None, typer.Option("--config", "-c", help="指定项目配置文件")
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="覆盖有效生成模型")
    ] = None,
) -> None:
    """并发生成、验证并导出一个资产 pack。

    批量执行有 plan 前置：必须先运行 ``pixel-asset plan <pack.yaml> --save``，
    否则拒绝执行。
    """
    overrides = {"model": model} if model is not None else None
    config = _load_config(config_path, overrides=overrides)
    control = PackRunControl()
    previous: dict[int, Any] = {}

    def request_pause(signum: int, _frame: Any) -> None:
        control.request_stop(signum)

    for signal_number in (signal.SIGINT, signal.SIGTERM):
        previous[signal_number] = signal.getsignal(signal_number)
        signal.signal(signal_number, request_pause)
    try:
        summary = asyncio.run(
            run_asset_pack(
                pack_file,
                config,
                control=control,
                retry_failed=retry_failed,
            )
        )
    except PixelAssetError as exc:
        _fail(exc)
        raise  # pragma: no cover
    finally:
        for saved_signal, handler in previous.items():
            signal.signal(saved_signal, handler)

    if as_json:
        console.print_json(
            json.dumps(summary.model_dump(mode="json"), ensure_ascii=False)
        )
    else:
        table = Table(title=f"Pack 执行 · {summary.pack_id}", header_style="bold")
        table.add_column("资产")
        table.add_column("结果")
        table.add_column("阶段")
        table.add_column("缓存", justify="center")
        table.add_column("恢复", justify="center")
        table.add_column("说明")
        for asset in summary.assets:
            details = [note for note in (asset.error, *asset.processing_notes) if note]
            if asset.validation_exemptions:
                exempt_targets = ", ".join(
                    exemption.target for exemption in asset.validation_exemptions
                )
                details.append(f"{exempt_targets} 几何检查=action_exempt")
            table.add_row(
                asset.asset_id,
                asset.outcome,
                asset.stage,
                "✓" if asset.cached else "—",
                "✓" if asset.resumed else "—",
                "；".join(details),
            )
        console.print(table)
        summary_path = _summary_display_path(config, summary.pack_id)
        console.print(
            f"[dim]有效模型 {summary.provider}/{summary.model} · "
            f"worker {summary.worker_count} · 汇总 {summary_path}[/dim]"
        )
        awaiting = summary.counts.get("awaiting_approval", 0)
        if awaiting:
            console.print(
                f"[bold yellow]有 {awaiting} 个资产等你看图；"
                "看完逐个用 create-animation --approve-seed 批准，"
                "再重跑同一条 create-asset-pack 命令。[/bold yellow]"
            )

    if summary.interrupted:
        raise typer.Exit(128 + (control.signal_number or signal.SIGINT))
    if summary.counts.get("provider_failed", 0) or summary.counts.get(
        "processing_failed", 0
    ) or summary.counts.get("paused", 0):
        raise typer.Exit(EXIT_ERROR)
    if summary.counts.get("validation_failed", 0):
        raise typer.Exit(EXIT_VALIDATION_FAILED)


def _summary_display_path(config: Config, pack_id: str) -> Path:
    return config.output_dir / "_packs" / pack_id / "pack-summary.json"


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
            JobKind.STATIC: "[green]static[/green]",
            JobKind.SEED: "[magenta]seed[/magenta]",
            JobKind.ANIMATION: "animation",
            JobKind.DERIVED: "[cyan]derived[/cyan]",
            JobKind.TILE: "[green]tile[/green]",
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
    console.print(f"[dim]下一步：{_next_step_hint(result.request)}[/dim]")


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

    # 只对真有帧序列的资产提这条 —— 让 tileset 用户去看不存在的 GIF 是纯噪音。
    skipped_order = [
        c
        for c in report.checks
        if c.id == "frame_order_continuity" and c.skip_reason != "not_applicable"
    ]
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
    asset_dir: Annotated[
        Path, typer.Argument(help="outputs/<asset_id> 或裸 asset_id")
    ],
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
    config = _load_config(config_path)
    resolved_dir = asset_dir if asset_dir.exists() else config.asset_dir(str(asset_dir))
    targets = list(target) if target else ["generic-json", "godot"]
    try:
        summary = run_export(
            resolved_dir, targets=targets, contact_sheet=not no_contact_sheet
        )
    except PixelAssetError as exc:
        _fail(exc)
        raise  # pragma: no cover

    table = Table(title=f"导出 · {summary.asset_id}", header_style="bold")
    table.add_column("文件")
    table.add_column("大小", justify="right")
    for path in summary.files:
        table.add_row(
            str(path.relative_to(resolved_dir)), f"{path.stat().st_size / 1024:.1f} KB"
        )
    if summary.contact_sheet:
        table.add_row(
            str(summary.contact_sheet.relative_to(resolved_dir)),
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
