"""验证引擎 —— 把测量结果按 per-action 阈值判成检查项（PLAN §9）。

一条不可退让的规则：**验证失败时绝不把资产标记为成功。**
``ValidationReport.passed`` 由 checks 推导，没有任何地方能直接把它设成 True。

判定与测量分开：``metrics.py`` 只出数字，这里只查表。
阈值还没校准（PLAN §9.1），把两者绑在一起会让校准变成改代码而不是改常数。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from ..constants import PALETTE_OVERFLOW_MAX, THRESHOLDS_CALIBRATED, Direction
from ..models.manifest import AssetManifest, DerivedAnimation, GeneratedAnimation
from ..models.validation import (
    Check,
    CheckId,
    CheckResult,
    ValidationReport,
    thresholds_for,
)
from ..planning.grid_layout import aspect_mismatch
from .frame_order import UNDETECTABLE_MESSAGE, measure_frame_order
from .metrics import (
    anchor_measurement,
    exact_duplicates,
    height_variation,
    is_blank,
    palette_of,
    silhouette_variation,
    transparent_rgb_residue,
)

#: 相邻帧差异低于此值即认为"几乎没动"。整组都低于它 → static_animation。
STATIC_THRESHOLD = 0.01


def _load_frames(root: Path, paths: list[str]) -> list[np.ndarray]:
    frames = []
    for relative in paths:
        path = root / relative
        if not path.exists():
            raise FileNotFoundError(path)
        frames.append(np.array(Image.open(path).convert("RGBA")))
    return frames


def _action_of(key: str) -> str:
    return key.split("_", 1)[0]


def _direction_of(key: str) -> Direction | None:
    parts = key.split("_", 1)
    if len(parts) < 2:
        return None
    candidate = parts[1]
    return candidate if candidate in ("down", "left", "right", "up") else None  # type: ignore[return-value]


def validate_animation(
    root: Path,
    key: str,
    entry: GeneratedAnimation,
    *,
    expected_frames: int,
    expected_size: tuple[int, int],
    max_colors: int,
    key_color: str,
) -> list[Check]:
    """校验一个生成型动作。"""
    action = _action_of(key)
    direction = _direction_of(key)
    limits = thresholds_for(action, direction)
    checks: list[Check] = []

    def add(check_id: CheckId, result: CheckResult, **kwargs: Any) -> None:
        checks.append(
            Check.make(check_id, key, result, action=action, direction=direction, **kwargs)
        )

    # -- 帧数（致命）------------------------------------------------------
    actual = len(entry.frames)
    add(
        "frame_count",
        CheckResult.PASS if actual == expected_frames else CheckResult.FAIL,
        measured=actual,
        threshold=expected_frames,
        message=None if actual == expected_frames else f"期望 {expected_frames} 帧，实际 {actual}",
    )
    if actual == 0:
        return checks

    try:
        frames = _load_frames(root, entry.frames)
    except FileNotFoundError as exc:
        add("frame_count", CheckResult.FAIL, message=f"帧文件缺失：{exc}")
        return checks

    # -- 帧尺寸（致命）----------------------------------------------------
    sizes = {(f.shape[1], f.shape[0]) for f in frames}
    uniform = len(sizes) == 1 and sizes.pop() == expected_size
    add(
        "frame_size",
        CheckResult.PASS if uniform else CheckResult.FAIL,
        message=None if uniform else f"期望 {expected_size}，实际 {sorted(sizes)}",
    )

    # -- 空白帧（致命）----------------------------------------------------
    blanks = [i for i, f in enumerate(frames) if is_blank(f)]
    add(
        "blank_frame",
        CheckResult.PASS if not blanks else CheckResult.FAIL,
        measured=len(blanks),
        threshold=0,
        message=None if not blanks else f"第 {blanks} 帧是空白",
    )

    # -- 透明 RGB 残留（致命）---------------------------------------------
    residue = sum(transparent_rgb_residue(f) for f in frames)
    add(
        "transparent_rgb_residue",
        CheckResult.PASS if residue == 0 else CheckResult.FAIL,
        measured=residue,
        threshold=0,
        message=None if residue == 0 else f"{residue} 个透明像素带非零 RGB（本地可修）",
    )

    # -- 单元格越界（致命）------------------------------------------------
    checks.append(
        _cell_overflow_check(root, key, entry, action, direction, key_color=key_color)
    )

    # -- 锚点漂移（高）----------------------------------------------------
    anchor = anchor_measurement(frames)
    limit = limits["anchor_drift_max_px"]
    if limit is None:
        add("anchor_drift", CheckResult.SKIP, skip_reason="action_exempt",
            measured=anchor.max_drift_px)
    else:
        add(
            "anchor_drift",
            CheckResult.PASS if anchor.max_drift_px <= limit else CheckResult.FAIL,
            measured=round(anchor.max_drift_px, 2),
            threshold=limit,
            message=f"脚底极差 {anchor.baseline_spread_px:.1f}px、"
                    f"水平极差 {anchor.horizontal_spread_px:.1f}px（本地可修）",
        )

    # -- 高度 / 轮廓变化（中）---------------------------------------------
    variation_checks: tuple[tuple[CheckId, float, str], ...] = (
        ("height_variation", height_variation(frames), "height_variation_max"),
        ("silhouette_variation", silhouette_variation(frames), "silhouette_variation_max"),
    )
    for check_id, value, key_name in variation_checks:
        bound = limits[key_name]
        if bound is None:
            add(check_id, CheckResult.SKIP, skip_reason="action_exempt", measured=round(value, 4))
        else:
            add(
                check_id,
                CheckResult.PASS if value <= bound else CheckResult.FAIL,
                measured=round(value, 4),
                threshold=bound,
            )

    # -- 调色板越界（中）--------------------------------------------------
    colors = palette_of(frames)
    over = max(0, len(colors) - max_colors)
    ratio = over / max(1, len(colors))
    add(
        "palette_overflow",
        CheckResult.PASS if ratio <= PALETTE_OVERFLOW_MAX else CheckResult.FAIL,
        measured=round(ratio, 4),
        threshold=PALETTE_OVERFLOW_MAX,
        message=f"{len(colors)} 色 / 上限 {max_colors}（本地可修）",
    )

    # -- 重复帧（中）------------------------------------------------------
    duplicates = exact_duplicates(frames)
    add(
        "duplicate_frame_exact",
        CheckResult.PASS if not duplicates else CheckResult.FAIL,
        measured=len(duplicates),
        threshold=0,
        message=None if not duplicates else f"完全相同的帧：{duplicates}",
    )

    # -- 静止动画（中）----------------------------------------------------
    stats = measure_frame_order(frames, loop=entry.loop)
    if stats is None:
        add("static_animation", CheckResult.SKIP, skip_reason="not_applicable")
    else:
        largest = max(stats.differences)
        add(
            "static_animation",
            CheckResult.PASS if largest > STATIC_THRESHOLD else CheckResult.FAIL,
            measured=round(largest, 4),
            threshold=STATIC_THRESHOLD,
            message=None if largest > STATIC_THRESHOLD
            else "所有相邻帧几乎无变化 —— 模型很可能画了 N 张相同的站姿",
        )

    # -- 帧序连续性（低，实测不可判定）------------------------------------
    add(
        "frame_order_continuity",
        CheckResult.SKIP,
        skip_reason="not_applicable",
        measured=round(stats.local_outlier, 3) if stats else None,
        message=UNDETECTABLE_MESSAGE,
    )

    return checks


def _cell_overflow_check(
    root: Path,
    key: str,
    entry: GeneratedAnimation,
    action: str,
    direction: Direction | None,
    *,
    key_color: str,
) -> Check:
    """从 ``source/`` 的原图**重新检测**能否分离出 N 个完整 sprite。

    这里刻意不去读处理阶段写下的标记：**验证器信任被验证对象写下的结论，
    就不叫验证了。** 重新检测的代价只是一次离线的键控 + 连通域标注。

    ### 判据改过一次（ADR-003 修订）

    初版判的是"有没有连通域跨越假想格线"。那个判据是错的 ——
    它衡量的是"模型的布局是否符合我假设的网格"，而不是"sprite 有没有被切坏"。
    实测把一张 8 个姿势彼此完全分离、毫无损伤的产出判成了 fatal，
    并让修复器对它重生成了三次（每次都是同样的布局，因为根因不在随机性上）。

    现在判的是能否用连通域分离出 ``frames`` 个 sprite：
    **分不出来才是真的坏了**（姿势粘连成一片、或模型画的数量不对），
    这时本地补不回被切掉的像素，只能重生成。

    ``aspect_mismatch`` 仍然报出来，但降为参考信息 ——
    按连通域抽帧之后，长短边比被改动不再必然导致损伤。
    """
    from ..processing.chroma_key import apply_chroma_key, hex_to_rgb
    from ..processing.component_split import group_components

    grid = entry.grid
    drift_note = ""
    if grid is not None and grid.requested_size and grid.actual_size:
        drift = aspect_mismatch(grid.requested_size, grid.actual_size)
        drift_note = f"；长短边比偏差 {drift:.1%}"

    if entry.source_image is None or grid is None:
        return Check.make(
            "cell_overflow", key, CheckResult.SKIP, action=action, direction=direction,
            skip_reason="not_applicable",
            message="manifest 缺少原图或 grid 溯源信息，无法回溯越界情况",
        )

    source = root / entry.source_image
    if not source.exists():
        return Check.make(
            "cell_overflow", key, CheckResult.SKIP, action=action, direction=direction,
            skip_reason="not_applicable",
            message=f"原图缺失：{entry.source_image}",
        )

    expected = len(entry.frames)
    image = np.array(Image.open(source).convert("RGB"))
    keyed = apply_chroma_key(
        image, hex_to_rgb(key_color), threshold=entry.key_threshold
    )
    groups = group_components(keyed.rgba[:, :, 3] > 0, expected, rows=grid.rows)

    if groups is None:
        return Check.make(
            "cell_overflow", key, CheckResult.FAIL,
            action=action, direction=direction,
            measured=0, threshold=expected,
            message=(
                f"无法从原图分离出 {expected} 个 sprite 连通域 —— "
                f"姿势可能粘连成一片，或模型画的数量不对{drift_note}。"
                "本地补不回被切掉的像素，必须重生成整个动作网格"
            ),
        )

    fragments = sum(len(g) for g in groups) - len(groups)
    return Check.make(
        "cell_overflow", key, CheckResult.PASS,
        action=action, direction=direction,
        measured=expected, threshold=expected,
        message=(
            f"成功分离出 {expected} 个 sprite"
            + (f"（吸附 {fragments} 个断开部件）" if fragments else "")
            + drift_note
        ),
    )


def validate_derived(key: str, entry: DerivedAnimation) -> list[Check]:
    """镜像派生的动作不参与身份漂移与轮廓类校验（ADR-006）。

    它是源方向的精确翻转，几何性质完全由源方向决定 —— 再验一遍只是重复计算，
    而且会把同一个问题报两次。
    """
    action = _action_of(key)
    direction = _direction_of(key)
    return [
        Check.make(
            check_id, key, CheckResult.SKIP, action=action, direction=direction,
            skip_reason="derived_animation",
            message=f"由 {entry.derived_from} 翻转派生，几何性质随源方向",
        )
        for check_id in (
            "anchor_drift", "height_variation", "silhouette_variation",
            "frame_order_continuity",
        )
    ]


def validate_asset(asset_dir: str | Path) -> ValidationReport:
    """校验一个资产目录下的全部动作。"""
    root = Path(asset_dir)
    manifest = AssetManifest.load(root / "asset-manifest.json")

    expected_size = (manifest.canvas.width, manifest.canvas.height)
    report = ValidationReport(
        asset_id=manifest.asset_id,
        thresholds_calibrated=THRESHOLDS_CALIBRATED,
    )

    request_frames: dict[str, int] = {}
    request_path = root / "request.yaml"
    if request_path.exists():
        from ..models.request import load_request

        request = load_request(request_path)
        for spec in request.animation_list():
            request_frames[spec.name] = spec.frames

    for key in sorted(manifest.animations):
        entry = manifest.animations[key]
        if isinstance(entry, DerivedAnimation):
            report.checks.extend(validate_derived(key, entry))
            continue

        expected = request_frames.get(_action_of(key), len(entry.frames))
        report.checks.extend(
            validate_animation(
                root, key, entry,
                expected_frames=expected,
                expected_size=expected_size,
                max_colors=manifest.palette.max_colors,
                key_color=manifest.background.color_used,
            )
        )
        report.thresholds_used[_action_of(key)] = thresholds_for(
            _action_of(key), _direction_of(key)
        )

    return report
