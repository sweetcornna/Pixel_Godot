"""per-action 阈值校准 harness。

一条命令生成固定校准矩阵、复用生产流水线处理帧，再调用
``pixel_asset_forge.validation.metrics`` 的纯量测函数采集证据。脚本只写运行目录，
绝不修改 ``pixel_asset_forge/constants.py``。

真实模式的 API Key 只允许由 :func:`pixel_asset_forge.config.load_config` 从环境变量或
项目 ``.env`` 读取；命令行刻意不提供 ``--api-key``。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import shutil
import sys
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Any, Final, Literal

import numpy as np
import yaml
from PIL import Image

from pixel_asset_forge.config import (
    Config,
    environment_secrets,
    load_config,
    redact_secret_values,
)
from pixel_asset_forge.constants import ACTION_DEFAULTS, ACTION_THRESHOLDS, Direction
from pixel_asset_forge.models.manifest import AssetManifest
from pixel_asset_forge.models.request import AssetType, Locomotion
from pixel_asset_forge.models.validation import thresholds_for
from pixel_asset_forge.pipelines import (
    approve_seed,
    build_contact_sheet,
    create_animation,
    create_character,
)
from pixel_asset_forge.pipelines.export import CONTACT_SHEET_NAME
from pixel_asset_forge.prompts.poses import beats_commanding_size_change
from pixel_asset_forge.storage import ArtifactStore
from pixel_asset_forge.validation.metrics import (
    anchor_measurement,
    height_variation,
    silhouette_variation,
)

EXIT_OK = 0
EXIT_ERROR = 2

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
RUNS_DIR = SCRIPT_DIR / "runs"
#: 与 live-gate 同一约定：runs/ 里的原图/GIF 体积大不入库，
#: 量化产出镜像到 reports/ 接受版本审查。
REPORTS_DIR = SCRIPT_DIR / "reports"
CONSTANTS_PATH = REPO_ROOT / "pixel_asset_forge" / "constants.py"

RAW_METRICS_NAME = "threshold-calibration-metrics.json"
AGGREGATES_NAME = "threshold-calibration-aggregates.json"
REPORT_NAME = "threshold-recommendations.md"
PREVIEWS_NAME = "preview-paths.json"
MANIFEST_NAME = "run-manifest.json"
TEXT_SUFFIXES: Final = frozenset({".json", ".jsonl", ".log", ".md", ".txt", ".yaml", ".yml"})
METRIC_NAMES: Final = ("height_variation", "silhouette_variation", "anchor_drift")
MetricName = Literal["height_variation", "silhouette_variation", "anchor_drift"]


class BudgetExceededError(RuntimeError):
    """固定计划超过用户给出的调用硬顶。"""


class BudgetReconciliationError(RuntimeError):
    """关闭缓存后，流水线完成调用数与固定计划仍不相等。"""


class ConstantsChangedError(RuntimeError):
    """运行期间 constants.py 内容发生变化。"""


class SecretLeakError(RuntimeError):
    """运行目录的文本产物中发现 API Key 明文。"""


@dataclass(frozen=True, slots=True)
class ActionSample:
    """一个要生成并量测的动作。``direction=None`` 表示各向同性动作。"""

    action: str
    direction: Direction | None

    @property
    def key(self) -> str:
        return f"{self.action}_{self.direction}" if self.direction else self.action


@dataclass(frozen=True, slots=True)
class AssetSample:
    """一个 seed 加若干校准动作组成的独立资产单元。"""

    asset_id: str
    label: str
    asset_type: AssetType
    description: str
    actions: tuple[ActionSample, ...]
    target_size: tuple[int, int] = (96, 96)
    max_colors: int = 32
    outline: str = "single_pixel_dark"
    shading: str = "two_tone"
    lighting: str = "fixed_top_left"
    locomotion: Locomotion | None = None

    @property
    def planned_calls(self) -> int:
        # 每个资产固定一次 canonical seed；每个样本动作固定一次完整网格生成。
        return 1 + len(self.actions)


@dataclass(frozen=True, slots=True)
class CalibrationMatrix:
    assets: tuple[AssetSample, ...]

    @property
    def planned_calls(self) -> int:
        return sum(asset.planned_calls for asset in self.assets)

    @property
    def sample_count(self) -> int:
        return sum(len(asset.actions) for asset in self.assets)


@dataclass(frozen=True, slots=True)
class MetricRecord:
    """字段与 docs/threshold-calibration-metrics.json 的既有记录同构。"""

    asset: str
    key: str
    anchor_drift: float
    height_variation: float
    silhouette_variation: float

    def to_dict(self) -> dict[str, str | float]:
        return {
            "asset": self.asset,
            "key": self.key,
            "anchor_drift": self.anchor_drift,
            "height_variation": self.height_variation,
            "silhouette_variation": self.silhouette_variation,
        }

    def value(self, metric: MetricName) -> float:
        return float(getattr(self, metric))


@dataclass(frozen=True, slots=True)
class CalibrationRun:
    run_dir: Path
    provider: str
    model: str
    planned_calls: int
    completed_calls: int
    records: tuple[MetricRecord, ...]
    raw_metrics_path: Path
    aggregates_path: Path
    report_path: Path
    previews_path: Path
    manifest_path: Path


CAST_DOWN = (ActionSample("cast", "down"),)
CAST_DOWN_WITH_UP = (ActionSample("cast", "down"), ActionSample("walk", "up"))

FULL_MATRIX = CalibrationMatrix(
    assets=(
        AssetSample(
            "cal_knight",
            "骑士",
            "character",
            "Young forest knight, short brown hair, green cloak, leather armor, short sword "
            "held in the right hand. Slim human build, no shield, no cape hood.",
            CAST_DOWN_WITH_UP,
        ),
        AssetSample(
            "cal_archer",
            "弓手",
            "character",
            "Very slim forest archer with a long wooden bow, fitted leather armor and a narrow "
            "human silhouette. The bow stays in the left hand; no cloak and no shield.",
            CAST_DOWN_WITH_UP,
        ),
        AssetSample(
            "cal_mage",
            "法师",
            "character",
            "Human mage in an extremely wide floor-length blue robe that hides both feet, with "
            "a tall pointed hat and a short wooden staff in the right hand.",
            CAST_DOWN_WITH_UP,
        ),
        AssetSample(
            "cal_golem",
            "石魔",
            "character",
            "Massive squat stone golem with square shoulders much wider than its hips, a broad "
            "blocky torso, oversized fists and very short thick legs.",
            CAST_DOWN,
        ),
        AssetSample(
            "cal_imp",
            "小恶魔",
            "character",
            "Tiny red imp with two horns, a thin body, short legs and fully spread bat wings much "
            "wider than its torso. No weapon and no clothing.",
            CAST_DOWN,
        ),
        AssetSample(
            "cal_slime",
            "史莱姆",
            "character",
            "Small round translucent magenta-to-violet slime with two dark eyes, no limbs, no "
            "accessories and a soft compressible blob silhouette.",
            CAST_DOWN,
            target_size=(64, 64),
            max_colors=24,
            locomotion="legless",
        ),
        AssetSample(
            "cal_fireball",
            "火球",
            "projectile",
            "Compact orange fireball effect with a white-yellow core, layered flame petals and a "
            "short directional ember trail. No character, ground or shadow.",
            (ActionSample("travel", "down"), ActionSample("impact", None)),
            target_size=(64, 64),
            max_colors=16,
            outline="none",
            shading="three_tone",
            lighting="none",
        ),
        AssetSample(
            "cal_lightning_chain",
            "闪电链",
            "spell",
            "Jagged blue-white chain lightning effect with branching electric arcs, a bright "
            "impact flash and a pulsing charged loop. No character, ground or shadow.",
            (
                ActionSample("travel", "down"),
                ActionSample("impact", None),
                ActionSample("loop", None),
            ),
            target_size=(64, 64),
            max_colors=16,
            outline="none",
            shading="three_tone",
            lighting="none",
        ),
        AssetSample(
            "cal_healing_aura",
            "治疗光环",
            "spell",
            "Circular green-gold healing aura with rising light motes, a soft expanding activation "
            "pulse and a steady breathing ring loop. No character, ground or shadow.",
            (ActionSample("impact", None), ActionSample("loop", None)),
            target_size=(64, 64),
            max_colors=16,
            outline="none",
            shading="three_tone",
            lighting="none",
        ),
    )
)
PLANNED_CALLS = FULL_MATRIX.planned_calls


def enforce_budget(max_calls: int, matrix: CalibrationMatrix = FULL_MATRIX) -> None:
    """在创建运行目录和调用 provider 之前检查硬顶。矩阵不会随上限增大。"""
    if max_calls < matrix.planned_calls:
        raise BudgetExceededError(
            f"固定矩阵规划调用 {matrix.planned_calls} 次，超过 --max-calls={max_calls}；拒绝执行"
        )


def reconcile_budget(planned_calls: int, completed_calls: int) -> None:
    """缓存已关闭，因此完成数必须与固定计划逐笔相等。"""
    if completed_calls != planned_calls:
        raise BudgetReconciliationError(
            f"调用对账失败：规划 {planned_calls} 次，实际完成 {completed_calls} 次"
        )


def budget_rows(matrix: CalibrationMatrix = FULL_MATRIX) -> list[dict[str, Any]]:
    return [
        {
            "asset": asset.asset_id,
            "label": asset.label,
            "seed_calls": 1,
            "actions": [{"key": action.key, "calls": 1} for action in asset.actions],
            "calls": asset.planned_calls,
        }
        for asset in matrix.assets
    ]


class ContractConflictError(RuntimeError):
    """prompt 与阈值互相矛盾。**在花掉第一次调用之前**就该抛。"""


def prompt_threshold_conflicts(
    matrix: CalibrationMatrix = FULL_MATRIX,
) -> dict[str, dict[str, list[str]]]:
    """矩阵里"prompt 命令整体缩放、而阈值禁止尺寸变化"的动作。

    2026-08-05 的教训：`loop` 的节拍逐字写着"从最小变到最大",而它的
    `height_variation` / `silhouette_variation` 阈值恰恰禁止尺寸变化。两份规格
    从未对账,于是那次 25 次真实调用只换回"样本越线"这一个早就可以静态判定的结论。

    **成对判定,不是只看命令词。** `impact` 同样命令扩张,但它的两项阈值都是豁免
    (`None`) —— 那是设计如此(爆开消散),不构成矛盾。只有"命令改尺寸"且"阈值管着
    尺寸"同时成立才算冲突。
    """
    conflicts: dict[str, dict[str, list[str]]] = {}
    for asset in matrix.assets:
        for sample in asset.actions:
            action = sample.action
            if action in conflicts:
                continue
            limits = ACTION_THRESHOLDS.get(action)
            if limits is None:
                continue
            # 只有这两项由整体尺寸驱动；anchor_drift 是位置量,与缩放无关。
            guarded = (
                limits.height_variation_max is not None
                or limits.silhouette_variation_max is not None
            )
            if not guarded:
                continue
            hits = beats_commanding_size_change(action)
            if hits:
                conflicts[action] = hits
    return conflicts


def enforce_prompt_threshold_contract(
    matrix: CalibrationMatrix = FULL_MATRIX,
    *,
    emit: Callable[[str], None] = print,
) -> None:
    """开跑前自检。有矛盾就抛错,**一次调用都不发**。"""
    conflicts = prompt_threshold_conflicts(matrix)
    if not conflicts:
        emit("prompt 与阈值契约自检：通过（无动作既被命令改尺寸又被尺寸阈值管着）")
        return
    lines = [
        "prompt 与阈值互相矛盾，这样跑下去只会烧掉调用换回一个静态就能得出的结论：",
    ]
    for action, hits in sorted(conflicts.items()):
        limits = ACTION_THRESHOLDS[action]
        beats = "；".join(f"{name}={words}" for name, words in sorted(hits.items()))
        lines.append(
            f"- {action}：节拍命令尺寸变化（{beats}），"
            f"而阈值 height={limits.height_variation_max} / "
            f"silhouette={limits.silhouette_variation_max} 正是管尺寸的。"
        )
    lines.append(
        "先改一致再跑：要么把节拍改成尺寸不变、由内部变化区分帧，"
        "要么把该动作的尺寸阈值改成豁免。**不要靠放宽阈值让越线样本通过。**"
    )
    raise ContractConflictError("\n".join(lines))


def print_budget(
    max_calls: int,
    matrix: CalibrationMatrix = FULL_MATRIX,
    *,
    emit: Callable[[str], None] = print,
) -> None:
    emit("固定调用预算（缓存强制关闭）：")
    for row in budget_rows(matrix):
        actions = " + ".join(f"{item['key']} 1" for item in row["actions"])
        emit(
            f"- {row['asset']}（{row['label']}）：seed 1 + {actions} = {row['calls']} 次"
        )
    emit(
        f"合计：{len(matrix.assets)} 个资产、{matrix.sample_count} 个动作样本、"
        f"{matrix.planned_calls} 次；--max-calls={max_calls}"
    )


# 脱敏与"环境里有哪些 Key 值"都委托给生产模块 —— 这是密钥处理，两边各写一份时
# 某一侧漏掉一种形态就会把 Key 写进日志。见 config.redact_secret_values 的说明。
redact_text = redact_secret_values
_environment_secrets = environment_secrets


def _install_log_redaction(secrets: Sequence[str]) -> None:
    if not secrets:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        try:
            rendered = record.getMessage()
        except (TypeError, ValueError):
            rendered = str(record.msg)
        record.msg = redact_text(rendered, secrets)
        record.args = ()
        return record

    logging.setLogRecordFactory(factory)


def _assert_no_secrets(run_dir: Path, secrets: Sequence[str]) -> None:
    secret_bytes = tuple(secret.encode("utf-8") for secret in secrets if secret)
    if not secret_bytes:
        return
    candidates = (
        path
        for path in run_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES
    )
    if any(secret in path.read_bytes() for path in candidates for secret in secret_bytes):
        raise SecretLeakError("落盘自检发现 API Key 明文；请移除本次运行目录后排查")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _request_payload(asset: AssetSample) -> dict[str, Any]:
    animations: list[dict[str, Any]] = []
    for sample in asset.actions:
        defaults = ACTION_DEFAULTS[sample.action]
        animation: dict[str, Any] = {
            "name": sample.action,
            "frames": defaults.frames,
            "fps": defaults.fps,
            "loop": defaults.loop,
        }
        if sample.direction is not None:
            animation["directions"] = [sample.direction]
        animations.append(animation)

    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "asset_id": asset.asset_id,
        "asset_type": asset.asset_type,
        "description": asset.description,
        "style": {
            "perspective": "top_down_3_4",
            "target_size": list(asset.target_size),
            "max_colors": asset.max_colors,
            "outline": asset.outline,
            "shading": asset.shading,
            "antialiasing": False,
            "lighting": asset.lighting,
        },
        "background": {
            "mode": "chroma_key",
            "color": "#FF00FF",
            "fallback_colors": ["#00FF00", "#00FFFF"],
        },
        "animations": animations,
        "export": {"targets": ["generic-json"]},
    }
    if asset.asset_type == "character":
        payload["mirroring"] = {
            "enabled": False,
            "reason": "calibration samples must be independently generated",
        }
    if asset.locomotion is not None:
        payload["locomotion"] = asset.locomotion
    return payload


def _write_request(asset: AssetSample, request_dir: Path) -> Path:
    path = request_dir / f"{asset.asset_id}.yaml"
    path.write_text(
        yaml.safe_dump(_request_payload(asset), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def _load_frames(store: ArtifactStore, key: str) -> list[np.ndarray]:
    frames: list[np.ndarray] = []
    for path in sorted(store.frames_of(key).glob("*.png")):
        with Image.open(path) as image:
            frames.append(np.array(image.convert("RGBA")))
    if not frames:
        raise RuntimeError(f"{store.root} 的 {key} 没有成品帧")
    return frames


def measure_sample(store: ArtifactStore, sample: ActionSample) -> MetricRecord:
    """只负责装载帧；三项几何量全部委托给现有 metrics.py。"""
    frames = _load_frames(store, sample.key)
    return MetricRecord(
        asset=store.root.name,
        key=sample.key,
        anchor_drift=float(anchor_measurement(frames).max_drift_px),
        height_variation=float(height_variation(frames)),
        silhouette_variation=float(silhouette_variation(frames)),
    )


def _aggregate_group(records: Sequence[MetricRecord]) -> dict[str, Any]:
    result: dict[str, Any] = {"sample_count": len(records)}
    for raw_name in METRIC_NAMES:
        name = raw_name  # 让 Literal 收窄保持在这个小边界内。
        values = [record.value(name) for record in records]  # type: ignore[arg-type]
        result[name] = {
            "min": min(values),
            "max": max(values),
            "mean": fmean(values),
        }
    return result


def aggregate_metrics(
    records: Sequence[MetricRecord], matrix: CalibrationMatrix
) -> dict[str, Any]:
    action_by_identity = {
        (asset.asset_id, sample.key): sample.action
        for asset in matrix.assets
        for sample in asset.actions
    }
    by_action: dict[str, list[MetricRecord]] = defaultdict(list)
    by_key: dict[str, list[MetricRecord]] = defaultdict(list)
    for record in records:
        by_action[action_by_identity[(record.asset, record.key)]].append(record)
        by_key[record.key].append(record)
    return {
        "schema_version": "1.0",
        "sample_count": len(records),
        "by_action": {
            action: _aggregate_group(group) for action, group in sorted(by_action.items())
        },
        "by_action_direction": {
            key: _aggregate_group(group) for key, group in sorted(by_key.items())
        },
    }


def _write_json(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _relative(path: Path, run_dir: Path) -> str:
    return str(path.relative_to(run_dir))


def _threshold_for_metric(action: str, metric: MetricName) -> float | int | None:
    limits = ACTION_THRESHOLDS[action]
    field = {
        "height_variation": "height_variation_max",
        "silhouette_variation": "silhouette_variation_max",
        "anchor_drift": "anchor_drift_max_px",
    }[metric]
    value = getattr(limits, field)
    return value if isinstance(value, (int, float)) else None


def _candidate_floor(metric: MetricName, maximum: float) -> str:
    if metric == "anchor_drift":
        return str(max(1, math.ceil(maximum)))
    return f"{math.ceil(maximum * 100) / 100:.2f}"


def _advice(provider: str, metric: MetricName, maximum: float, current: float | int | None) -> str:
    if provider == "mock":
        return "mock 只证明链路；不支持阈值决策"
    if current is None:
        return "当前豁免；先人工确认该指标是否适合作为阻断项"
    if maximum > current:
        floor = _candidate_floor(metric, maximum)
        return f"若全部样本人工判定合格，才可考虑放宽；候选下限 {floor}"
    if metric == "anchor_drift":
        return "绝对像素量例外：人工审核后可评估收紧；脚本不执行"
    return "实测未越线；按不对称策略不据此收紧"


def build_recommendation_report(
    *,
    timestamp: str,
    provider: str,
    model: str,
    matrix: CalibrationMatrix,
    records: Sequence[MetricRecord],
    aggregates: Mapping[str, Any],
    completed_calls: int,
    preview_payload: Sequence[Mapping[str, Any]],
) -> str:
    lines = [
        "# 阈值校准建议报告",
        "",
        f"- UTC：`{timestamp}`",
        f"- Provider：`{provider}` / `{model}`",
        f"- 样本：{len(matrix.assets)} 个资产，{matrix.sample_count} 个动作",
        f"- 调用对账：规划 {matrix.planned_calls}，完成 {completed_calls}",
        "- 结论边界：本报告只产出证据与建议，未修改 `constants.py`。",
        "",
        "## 判读策略",
        "",
        "只有人工确认合格的真实样本越过现阈值，才构成放宽证据；样本远低于现阈值不构成",
        "收紧依据。`anchor_drift` 是与轮廓无关的绝对像素量，可作为收紧例外单独审计。",
    ]
    if provider == "mock":
        lines.extend(
            [
                "",
                "> 本次是 mock 演练。数值只能证明生成、处理、量测、聚合和报告链路可运行，",
                "> 不能用于调整真实模型阈值或 `up` 修正系数。",
            ]
        )

    lines.extend(
        [
            "",
            "## 按动作聚合",
            "",
            "| 动作 | n | 指标 | min | max | mean | 当前阈值 | 建议 |",
            "|---|---:|---|---:|---:|---:|---:|---|",
        ]
    )
    by_action: Mapping[str, Any] = aggregates["by_action"]
    for action, group in by_action.items():
        for raw_metric in METRIC_NAMES:
            metric: MetricName = raw_metric
            summary = group[metric]
            current = _threshold_for_metric(action, metric)
            current_text = "豁免" if current is None else str(current)
            advice = _advice(provider, metric, float(summary["max"]), current)
            lines.append(
                f"| `{action}` | {group['sample_count']} | `{metric}` | "
                f"{summary['min']:.4f} | {summary['max']:.4f} | {summary['mean']:.4f} | "
                f"{current_text} | {advice} |"
            )

    up_records = [record for record in records if record.key.endswith("_up")]
    lines.extend(
        [
            "",
            "## `up` ×1.3 证据",
            "",
            "| 样本 | 指标 | 实测 | down 基准阈值 | up ×1.3 阈值 | 判读 |",
            "|---|---|---:|---:|---:|---|",
        ]
    )
    for record in up_records:
        action = record.key.removesuffix("_up")
        adjusted = thresholds_for(action, "up")
        for metric, field in (
            ("height_variation", "height_variation_max"),
            ("silhouette_variation", "silhouette_variation_max"),
        ):
            typed_metric: MetricName = metric
            measured = record.value(typed_metric)
            base = _threshold_for_metric(action, typed_metric)
            up_limit = adjusted[field]
            if provider == "mock":
                verdict = "mock 不支持系数决策"
            elif base is not None and measured <= base:
                verdict = "该样本未显示需要放宽"
            elif up_limit is not None and measured <= float(up_limit):
                verdict = "该样本只在 ×1.3 后过线"
            else:
                verdict = "该样本超过现有修正；先人工审图"
            lines.append(
                f"| `{record.asset}/{record.key}` | `{metric}` | {measured:.4f} | "
                f"{base if base is not None else '豁免'} | "
                f"{up_limit if up_limit is not None else '豁免'} | {verdict} |"
            )

    lines.extend(
        [
            "",
            "## 人工审图",
            "",
            "先逐个查看 contact sheet 的轮廓、动作语义和方向，再播放 GIF 检查帧序。任何不合格",
            "样本都必须从阈值证据中排除，不能靠放宽阈值让它通过。",
            "",
        ]
    )
    for preview in preview_payload:
        lines.append(f"- `{preview['asset']}` contact sheet：`{preview['contact_sheet']}`")
        for gif in preview["gifs"]:
            lines.append(f"- `{preview['asset']}` GIF：`{gif}`")

    lines.extend(
        [
            "",
            "## 人工落回流程",
            "",
            "1. 只保留人工判定合格的 live 样本，复算对应动作的 min/max/mean。",
            "2. 按上述不对称策略形成阈值变更提案；`up` 系数单独比较基准阈值与 ×1.3。",
            "3. 人工修改 `pixel_asset_forge/constants.py`，再用同一批合格帧复验。",
            "4. 将日期、模型、样本量、人工审核结论和改动依据追加到 "
            "`docs/threshold-calibration.md`。",
        ]
    )
    return "\n".join(lines) + "\n"


def _load_run_config(run_dir: Path, env: Mapping[str, str]) -> Config:
    # 项目 YAML/.env 仍按 load_config 的正常优先级生效；只锁定本次隔离边界与缓存开关。
    return load_config(
        overrides={
            "output_dir": run_dir / "assets",
            "cache_dir": run_dir / "cache",
            "cache_enabled": False,
        },
        env=dict(env),
    )


def execute_calibration(
    run_dir: Path,
    *,
    matrix: CalibrationMatrix = FULL_MATRIX,
    max_calls: int | None = None,
    env: Mapping[str, str] | None = None,
    timestamp: str | None = None,
    emit: Callable[[str], None] = print,
    reports_dir: Path | None = None,
) -> CalibrationRun:
    """执行一个固定矩阵；测试可注入缩小矩阵，但与 CLI 共用全部逻辑。"""
    allowed = matrix.planned_calls if max_calls is None else max_calls
    enforce_budget(allowed, matrix)
    constants_before = _sha256(CONSTANTS_PATH)
    run_dir.mkdir(parents=True, exist_ok=False)
    request_dir = run_dir / "requests"
    request_dir.mkdir()

    effective_env = dict(os.environ if env is None else env)
    config = _load_run_config(run_dir, effective_env)
    secrets = list(_environment_secrets(effective_env))
    if config.provider != "mock":
        # 在第一笔 provider 调用前失败；Key 值只进内存，用于 provider 与落盘自检。
        secret = config.require_api_key().get_secret_value()
        secrets.append(secret)
    _install_log_redaction(secrets)

    completed_calls = 0
    records: list[MetricRecord] = []
    previews: list[dict[str, Any]] = []

    for asset in matrix.assets:
        emit(f"生成 {asset.asset_id}（{asset.label}）seed")
        request_path = _write_request(asset, request_dir)
        seed = create_character(request_path, config)
        completed_calls += int(not seed.cached)
        store = ArtifactStore.for_asset(config.output_dir, asset.asset_id)
        approve_seed(store.root)

        for sample in asset.actions:
            emit(f"生成并量测 {asset.asset_id}/{sample.key}")
            result = create_animation(
                store.root,
                action=sample.action,
                direction=sample.direction,
                config=config,
            )
            completed_calls += int(result.calls_api and not result.cached)
            records.append(measure_sample(store, sample))

        manifest = AssetManifest.load(store.manifest_path)
        contact_sheet = build_contact_sheet(
            manifest, store.root, store.previews / CONTACT_SHEET_NAME
        )
        gifs = sorted(store.previews.glob("*.gif"))
        previews.append(
            {
                "asset": asset.asset_id,
                "contact_sheet": _relative(contact_sheet, run_dir),
                "gifs": [_relative(path, run_dir) for path in gifs],
            }
        )

    reconcile_budget(matrix.planned_calls, completed_calls)
    emit(f"调用对账通过：规划 {matrix.planned_calls}，完成 {completed_calls}")

    generated_at = timestamp or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    raw_path = _write_json(
        run_dir / RAW_METRICS_NAME,
        [record.to_dict() for record in records],
    )
    aggregates = aggregate_metrics(records, matrix)
    aggregates_path = _write_json(run_dir / AGGREGATES_NAME, aggregates)
    previews_path = _write_json(run_dir / PREVIEWS_NAME, previews)
    report_path = run_dir / REPORT_NAME
    report_path.write_text(
        build_recommendation_report(
            timestamp=generated_at,
            provider=config.provider,
            model=config.model,
            matrix=matrix,
            records=records,
            aggregates=aggregates,
            completed_calls=completed_calls,
            preview_payload=previews,
        ),
        encoding="utf-8",
    )

    constants_after = _sha256(CONSTANTS_PATH)
    if constants_after != constants_before:
        raise ConstantsChangedError("constants.py 在 harness 运行期间发生变化；拒绝产出校准结论")

    manifest_path = _write_json(
        run_dir / MANIFEST_NAME,
        {
            "schema_version": "1.0",
            "generated_at_utc": generated_at,
            "provider": config.provider,
            "model": config.model,
            "cache_enabled": config.cache_enabled,
            "matrix": {
                "asset_count": len(matrix.assets),
                "sample_count": matrix.sample_count,
                "assets": budget_rows(matrix),
            },
            "budget": {
                "max_calls": allowed,
                "planned_calls": matrix.planned_calls,
                "completed_calls": completed_calls,
                "reconciled": True,
            },
            "constants": {
                "path": str(CONSTANTS_PATH.relative_to(REPO_ROOT)),
                "sha256_before": constants_before,
                "sha256_after": constants_after,
                "unchanged": True,
            },
            "outputs": {
                "raw_metrics": RAW_METRICS_NAME,
                "aggregates": AGGREGATES_NAME,
                "recommendation_report": REPORT_NAME,
                "preview_paths": PREVIEWS_NAME,
            },
        },
    )
    _assert_no_secrets(run_dir, secrets)

    if reports_dir is not None:
        reports_dir.mkdir(parents=True, exist_ok=False)
        for artifact in (raw_path, aggregates_path, report_path, previews_path, manifest_path):
            shutil.copy2(artifact, reports_dir / artifact.name)

    return CalibrationRun(
        run_dir=run_dir,
        provider=config.provider,
        model=config.model,
        planned_calls=matrix.planned_calls,
        completed_calls=completed_calls,
        records=tuple(records),
        raw_metrics_path=raw_path,
        aggregates_path=aggregates_path,
        report_path=report_path,
        previews_path=previews_path,
        manifest_path=manifest_path,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行固定矩阵的 per-action 阈值校准 harness")
    parser.add_argument(
        "--max-calls",
        type=int,
        default=PLANNED_CALLS,
        help=f"允许的调用硬顶（默认 {PLANNED_CALLS}；固定矩阵不会随上限扩大）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    initial_secrets = _environment_secrets(os.environ)

    def emit(message: str) -> None:
        print(redact_text(message, initial_secrets), flush=True)

    print_budget(args.max_calls, emit=emit)
    try:
        enforce_budget(args.max_calls)
    except BudgetExceededError as exc:
        emit(f"RESULT ERROR（退出码 {EXIT_ERROR}）：{exc}")
        return EXIT_ERROR

    # 契约自检放在这里：预算已经打印（用户看得到要花多少），但**还没建运行目录、
    # 也没发出任何一次调用**。矛盾是静态可判定的，没有理由花钱去发现它。
    try:
        enforce_prompt_threshold_contract(emit=emit)
    except ContractConflictError as exc:
        emit(f"RESULT ERROR（退出码 {EXIT_ERROR}）：{exc}")
        return EXIT_ERROR

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = RUNS_DIR / stamp
    reports_dir = REPORTS_DIR / stamp
    try:
        result = execute_calibration(
            run_dir,
            max_calls=args.max_calls,
            timestamp=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            emit=emit,
            reports_dir=reports_dir,
        )
    except Exception as exc:
        message = redact_text(f"{type(exc).__name__}: {exc}", initial_secrets)
        emit(f"RESULT ERROR（退出码 {EXIT_ERROR}）：{message}")
        if run_dir.exists():
            emit(f"失败运行目录保留供排查：{run_dir.relative_to(REPO_ROOT)}")
        return EXIT_ERROR

    emit(f"运行目录：{result.run_dir.relative_to(REPO_ROOT)}")
    emit(f"量化报告（入库）：{reports_dir.relative_to(REPO_ROOT)}")
    emit(
        "关键产出："
        f"{result.raw_metrics_path.name}；{result.aggregates_path.name}；"
        f"{result.report_path.name}；{result.previews_path.name}；{result.manifest_path.name}"
    )
    emit(f"RESULT PASS（退出码 {EXIT_OK}）")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
