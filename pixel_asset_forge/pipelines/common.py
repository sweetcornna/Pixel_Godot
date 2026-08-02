"""生成流水线的公共部件。"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from ..errors import ProcessingError
from ..logging_utils import get_logger
from ..models.job import Job, JobEvent, JobTable
from ..models.manifest import (
    AssetManifest,
    BackgroundInfo,
    CanvasInfo,
    PaletteInfo,
    ProviderInfo,
    ScaleProfileInfo,
)
from ..models.request import AssetRequest
from ..planning.grid_layout import GridLayout
from ..processing.background import BackgroundDecision
from ..processing.chroma_key import apply_chroma_key, hex_to_rgb
from ..processing.pipeline import ProcessResult
from ..processing.scale_profile import ScaleProfile, derive_profile
from ..providers.base import GenerationResult
from ..storage.artifacts import ArtifactStore

logger = get_logger("pipeline.common")


def blank_canvas(size: tuple[int, int], key_color: str) -> bytes:
    """生成一张纯键控色的空白画布，作为 ``images.edit`` 的 base image。

    **不传 mask**（ADR-003 / PLAN §2.6）：GPT Image 的 mask 是提示性约束，
    不保证遵循边界。用它保护"第一格已画好的 seed"既不可靠，
    又会让模型把注意力放在边界处理而非身份一致性上。
    seed 以纯参考图身份传入，实测身份保持极好（Sprint 0 / A-6，16 格零漂移）。

    动作网格现在默认用 :func:`anchor_sheet` 代替这张空白画布，见该函数。
    """
    width, height = size
    canvas = Image.new("RGB", (width, height), hex_to_rgb(key_color))
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue()


#: anchor sheet 的构图参数，取自 agent-sprite-forge 的 ``make_anchor_layout.py`` 默认值。
#: 角色占格子高度的 66%、宽度的 72%，脚底落在格子 82% 高处。
ANCHOR_SUBJECT_HEIGHT_RATIO = 0.66
ANCHOR_SUBJECT_WIDTH_RATIO = 0.72
ANCHOR_FEET_RATIO = 0.82


def anchor_sheet(
    seed_rgb: np.ndarray,
    size: tuple[int, int],
    layout: GridLayout,
    key_color: str,
) -> bytes:
    """把已批准的 seed 按固定缩放与脚线平铺进每一格，作为 ``edit`` 的 base image。

    这是 agent-sprite-forge 的 **character anchor sheet** 手法：光靠文字约束
    压不住的漂移，给一张"每格都已经站好一个正确角色"的模板就压得住 ——
    模型要做的从"照描述画六个角色"变成"把这六个角色摆成不同姿势"。

    它同时解决三件文字说不清的事：

    - **镜像翻转**。步态第二个半周期的描述会把左右腿对调
      （"the RIGHT leg strides far forward"），模型常把这个对调理解成整体镜像，
      于是剑换到另一只手、斗篷翻到另一边。每格已有一个持剑手正确的角色时，
      "只改姿势"就不会牵动持械手。
    - **体型与脚线漂移**。格子里已经画好了该多大、脚踩在哪。
    - **构图漂移**。角色在格子里的位置由模板给定，不再逐格重新构图。

    背景仍是纯键控色，抠图链路完全不变。
    """
    width, height = size
    cell_w, cell_h = width // layout.cols, height // layout.rows

    keyed = apply_chroma_key(seed_rgb, hex_to_rgb(key_color))
    ys, xs = np.nonzero(keyed.rgba[:, :, 3])
    if xs.size == 0:
        logger.warning("seed 抠不出主体，anchor sheet 退回空白画布")
        return blank_canvas(size, key_color)

    subject = Image.fromarray(
        keyed.rgba[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    )
    scale = min(
        cell_h * ANCHOR_SUBJECT_HEIGHT_RATIO / subject.height,
        cell_w * ANCHOR_SUBJECT_WIDTH_RATIO / subject.width,
    )
    subject = subject.resize(
        (max(1, round(subject.width * scale)), max(1, round(subject.height * scale))),
        Image.Resampling.LANCZOS,
    )

    canvas = Image.new("RGB", (width, height), hex_to_rgb(key_color))
    offset_x = (cell_w - subject.width) // 2
    offset_y = round(cell_h * ANCHOR_FEET_RATIO) - subject.height
    if offset_x < 0 or offset_y < 0:  # pragma: no cover - 比例常量保证不会发生
        logger.warning("anchor sheet 的主体放不进格子，退回空白画布")
        return blank_canvas(size, key_color)

    for index in range(layout.capacity):
        col, row = index % layout.cols, index // layout.cols
        canvas.paste(
            subject,
            (col * cell_w + offset_x, row * cell_h + offset_y),
            subject,
        )

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue()


def load_rgb(path: str | Path) -> np.ndarray:
    return np.array(Image.open(path).convert("RGB"))


@dataclass
class GenerationContext:
    """一次生成调用所需的全部上下文。"""

    request: AssetRequest
    store: ArtifactStore
    background: BackgroundDecision
    table: JobTable

    @property
    def key_color(self) -> str:
        return self.background.color_used


def record_generation(
    store: ArtifactStore, job: Job, result: GenerationResult, *, prompt_chars: int
) -> None:
    """把一次生成写进日志与任务记录。

    日志条目**不含 prompt 原文与响应体** —— 前者可能很长，后者含图像 base64，
    两者都会让日志文件迅速失控。溯源靠 ``prompt_hash`` 与 ``request_id``。
    """
    entry: dict[str, Any] = result.log_entry()
    entry["job_id"] = job.id
    entry["key"] = job.key
    entry["prompt_chars"] = prompt_chars
    store.append_generation_log(entry)

    job.prompt_hash = result.prompt_hash
    job.request_id = result.request_id


def advance_through_processing(job: Job, *, validated: bool = True) -> None:
    """把任务从 ``generated`` 推到 ``processed``。

    验证引擎是 Sprint 5 的事，这里只走到 ``processed``；
    **绝不直接跳到 validated** —— 状态机不允许，也不该允许
    （"不要跳过 validate 直接交付"这条规则要靠状态机强制，不是靠自觉）。
    """
    job.fire(JobEvent.START_PROCESSING)
    job.fire(JobEvent.PROCESSING_DONE)


def ensure_manifest(
    store: ArtifactStore,
    request: AssetRequest,
    background: BackgroundDecision,
    *,
    provider_name: str,
    model: str,
) -> AssetManifest:
    """读取既有 Manifest；没有就按请求新建一份。"""
    if store.manifest_path.exists():
        return AssetManifest.load(store.manifest_path)

    return AssetManifest(
        asset_id=request.asset_id,
        asset_type=request.asset_type,
        provider=ProviderInfo(name=provider_name, model=model),  # type: ignore[arg-type]
        canvas=CanvasInfo(
            width=request.style.target_size[0], height=request.style.target_size[1]
        ),
        background=BackgroundInfo(
            mode=request.background.mode,
            color_requested=background.color_requested,
            color_used=background.color_used,
            fallback_stage=background.fallback_stage,
        ),
        palette=PaletteInfo(max_colors=request.style.max_colors, colors=[]),
        mirroring=None,
        status="planned",
    )


def profile_from_manifest(manifest: AssetManifest) -> ScaleProfile | None:
    """从 Manifest 取跨动作缩放基准。第一个动作时还没有，返回 None。"""
    info = manifest.scale_profile
    if info is None:
        return None
    return ScaleProfile(
        reference=info.reference,
        subject_ratio=info.subject_ratio,
        canvas_fraction=info.canvas_fraction,
    )


def store_profile(
    manifest: AssetManifest, key: str, result: ProcessResult
) -> tuple[ScaleProfile, bool]:
    """更新跨动作缩放基准。返回 ``(基准, 是否被本动作顶替)``。

    基准该取**幅度最大**的动作 —— 参考动作按定义填满画布，比它小的动作
    等比缩小，这样才不会有动作被推到画布外。可是增量生成时看不到未来的动作，
    只能边走边修：本动作在自己格子里占得比现任参考更满，就顶替它。

    顶替意味着此前那些动作是按旧基准出的图，相对大小已经不对了。这里不去
    重出它们（那要重跑整条链、还得改已写好的 frames/），而是让调用方告诉用户
    去跑 ``pixel-asset process`` —— 那条命令看得到全部动作，一次就能定对基准。
    """
    candidate = derive_profile(
        key,
        content_height=result.content_source_height,
        cell_height=result.source_cell_height,
        canvas_height=manifest.canvas.height,
        output_height=result.output_content_height,
    )
    current = profile_from_manifest(manifest)
    # 重新生成参考动作本身时，新值直接覆盖旧值 —— 那不是"被别的动作顶替"，
    # 否则会报出 "walk_down 比原参考动作 walk_down 幅度更大" 这种废话。
    if current is not None and current.reference == key:
        stored_needs_reprocess = (
            manifest.scale_profile.needs_reprocess
            if manifest.scale_profile is not None
            else False
        )
        manifest.scale_profile = ScaleProfileInfo(
            reference=candidate.reference,
            subject_ratio=candidate.subject_ratio,
            canvas_fraction=candidate.canvas_fraction,
            needs_reprocess=stored_needs_reprocess,
        )
        return (candidate, False)
    if current is not None and current.subject_ratio >= candidate.subject_ratio:
        return (current, False)

    manifest.scale_profile = ScaleProfileInfo(
        reference=candidate.reference,
        subject_ratio=candidate.subject_ratio,
        canvas_fraction=candidate.canvas_fraction,
        needs_reprocess=current is not None,
    )
    return (candidate, current is not None)


def load_job_table(store: ArtifactStore, request: AssetRequest) -> JobTable:
    """读取既有任务表；没有就现规划一份。"""
    from ..planning.planner import plan_request

    existing = store.load_job_table()
    return plan_request(request, existing=existing).jobs


def require_source_slot(store: ArtifactStore, key: str, *, regenerate: bool) -> None:
    """重生成前先归档既有原图。

    原始生成图永不覆盖 —— 这是 ``process`` 能离线重跑的前提。
    归档而非删除：失败样本对调 prompt 很有价值。
    """
    path = store.source_path(key)
    if not path.exists():
        return
    if not regenerate:
        raise ProcessingError(
            f"{path} 已存在。重新生成会覆盖原图 —— 加 --regenerate 显式确认，"
            f"届时旧图会被归档而不是删除。"
        )
    store.archive_source(key)
