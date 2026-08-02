"""``pixel-asset export`` —— 导出引擎格式 + Contact Sheet。**不调用 API。**

Contact Sheet 在这里不只是"顺手做的预览"：帧序被打乱**无法自动检测**
（见 ``validation/frame_order.py`` 的实测结论），
一屏看完所有动作的 contact sheet 与逐动作 GIF 是**唯一的防线**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw

from ..errors import ExportError
from ..exporters import get_exporter
from ..exporters.base import animation_views, load_frames, load_static_image
from ..logging_utils import get_logger
from ..models.job import JobEvent, JobStatus
from ..models.manifest import AssetManifest
from ..storage.artifacts import ArtifactStore

logger = get_logger("pipeline.export")

CONTACT_SHEET_NAME = "contact-sheet.png"

#: Contact sheet 的放大倍数。32×32 的帧不放大根本看不清。
CONTACT_SCALE = 4
LABEL_WIDTH = 96
BACKGROUND = (34, 34, 44)

#: 贴片区用棋盘格而非纯色。纯色背景与角色描边撞色时，那部分像素在审核图上
#: 直接隐形 —— 实测本仓库两个示例包的标准描边色 ``#211A2C`` 与旧背景
#: ``#22222C`` 距离只有 8.06，wooden_barrel 42% 的像素看不见。棋盘格是两色
#: 交替，任何单色都不可能与整片背景同时同色，同时还区分了"透明"与"深色实体"。
#: 两格拉开亮度差，好让"最坏情况"（颜色恰在两格中间）也还剩约 66 的距离。
CHECKER_LIGHT = (150, 150, 158)
CHECKER_DARK = (74, 74, 82)
CHECKER_SIZE = CONTACT_SCALE * 2


def _checkerboard(size: tuple[int, int]) -> Image.Image:
    """贴片区背景。确定性生成，保证 contact sheet 可逐字节复现。"""
    canvas = Image.new("RGB", size, CHECKER_LIGHT)
    draw = ImageDraw.Draw(canvas)
    for y in range(0, size[1], CHECKER_SIZE):
        for x in range(0, size[0], CHECKER_SIZE):
            if (x // CHECKER_SIZE + y // CHECKER_SIZE) % 2:
                draw.rectangle(
                    [x, y, x + CHECKER_SIZE - 1, y + CHECKER_SIZE - 1],
                    fill=CHECKER_DARK,
                )
    return canvas


@dataclass
class ExportSummary:
    asset_id: str
    targets: list[str] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)
    contact_sheet: Path | None = None
    notes: list[str] = field(default_factory=list)


def build_contact_sheet(manifest: AssetManifest, root: Path, out: Path) -> Path:
    """一屏看完所有动作，供一次性人工审核。

    每行一个动作、行首标注动作名 —— 帧序是否正确、朝向是否搞反、
    某一帧是否塌掉，都只能靠这张图和 GIF 用眼睛看出来。
    """
    views = animation_views(manifest, root)
    if not views:
        if manifest.static_image is None:
            raise ExportError("没有任何静态图或动作，无法生成 contact sheet")
        frame = load_static_image(manifest, root)
        cell_h, cell_w = frame.shape[:2]
        scaled_w, scaled_h = cell_w * CONTACT_SCALE, cell_h * CONTACT_SCALE
        canvas = Image.new("RGB", (LABEL_WIDTH + scaled_w, scaled_h), BACKGROUND)
        canvas.paste(_checkerboard((scaled_w, scaled_h)), (LABEL_WIDTH, 0))
        draw = ImageDraw.Draw(canvas)
        draw.text((6, scaled_h // 2 - 6), "static", fill=(220, 220, 230))
        tile = Image.fromarray(frame, "RGBA").resize(
            (scaled_w, scaled_h), Image.Resampling.NEAREST
        )
        canvas.paste(tile, (LABEL_WIDTH, 0), tile)
        out.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out)
        return out

    frames_by_key = {v.key: load_frames(root, v) for v in views}
    cell_h, cell_w = frames_by_key[views[0].key][0].shape[:2]
    scaled_w, scaled_h = cell_w * CONTACT_SCALE, cell_h * CONTACT_SCALE
    cols = max(v.frame_count for v in views)

    canvas = Image.new(
        "RGB", (LABEL_WIDTH + cols * scaled_w, len(views) * scaled_h), BACKGROUND
    )
    canvas.paste(
        _checkerboard((cols * scaled_w, len(views) * scaled_h)), (LABEL_WIDTH, 0)
    )
    draw = ImageDraw.Draw(canvas)

    for row, view in enumerate(views):
        y = row * scaled_h
        label = view.key + ("↔" if view.derived_from else "")
        draw.text((6, y + scaled_h // 2 - 6), label, fill=(220, 220, 230))

        for col, frame in enumerate(frames_by_key[view.key]):
            tile = Image.fromarray(frame, "RGBA").resize(
                (scaled_w, scaled_h), Image.Resampling.NEAREST
            )
            canvas.paste(tile, (LABEL_WIDTH + col * scaled_w, y), tile)

        # 行间细分隔线，避免两行动作在视觉上黏在一起
        if row:
            draw.line([(0, y), (canvas.width, y)], fill=(70, 70, 84))

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    return out


def run_export(
    asset_dir: str | Path,
    *,
    targets: list[str],
    contact_sheet: bool = True,
) -> ExportSummary:
    """导出一个资产。"""
    root = Path(asset_dir)
    store = ArtifactStore(root=root)
    if not store.manifest_path.exists():
        raise ExportError(f"{root} 下没有 asset-manifest.json —— 先跑 create-character / process")

    manifest = AssetManifest.load(store.manifest_path)
    if manifest.static_image is not None:
        if manifest.status not in ("validated", "exported"):
            raise ExportError(
                f"Manifest 状态为 {manifest.status}，只有 validated/exported 可导出"
            )

        table = store.load_job_table()
        if table is not None:
            pending = [
                job
                for job in table
                if job.kind.value != "seed"
                and job.status not in (JobStatus.VALIDATED, JobStatus.EXPORTED)
            ]
            if pending:
                states = ", ".join(f"{job.id}={job.status.value}" for job in pending)
                raise ExportError(f"导出前成品任务必须 validated；当前 {states}")

    summary = ExportSummary(asset_id=manifest.asset_id)

    for target in targets:
        exporter = get_exporter(target)
        result = exporter.export(manifest, root, store.exports / target)
        summary.targets.append(target)
        summary.files.extend(result.files)
        summary.notes.extend(result.notes)

    if contact_sheet:
        summary.contact_sheet = build_contact_sheet(
            manifest, root, store.previews / CONTACT_SHEET_NAME
        )
        summary.notes.append(
            "帧序被打乱无法自动检测 —— 请看 contact sheet 与 previews/*.gif 确认播放顺序。"
            if manifest.animations
            else "静态资产的 contact sheet 供构图与配色的人工审核。"
        )

    _mark_exported(store, manifest)
    return summary


def _mark_exported(store: ArtifactStore, manifest: AssetManifest) -> None:
    """把已验证的任务推进到 ``exported``。

    只推 ``validated`` 的任务 —— 状态机不允许从别的状态直接跳过来，
    这正是"不要跳过 validate 直接交付"这条规则的落地点。
    """
    table = store.load_job_table()
    if table is None:
        return

    moved = 0
    for job in table:
        if job.status is JobStatus.VALIDATED:
            job.fire(JobEvent.EXPORT)
            moved += 1
    if moved:
        store.save_job_table(table)

    deliverable_jobs = [job for job in table if job.kind.value != "seed"]
    if deliverable_jobs and all(job.status is JobStatus.EXPORTED for job in deliverable_jobs):
        manifest.status = "exported"
    manifest.save(store.manifest_path)
