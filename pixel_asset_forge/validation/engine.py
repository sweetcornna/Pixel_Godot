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

from ..constants import (
    ACTION_THRESHOLDS,
    PALETTE_OVERFLOW_MAX,
    THRESHOLDS_CALIBRATED,
    Direction,
    split_animation_key,
)
from ..errors import PlanError
from ..models.manifest import AssetManifest, DerivedAnimation, GeneratedAnimation, StaticImageInfo
from ..models.request import STATIC_ASSET_TYPES
from ..models.validation import (
    Check,
    CheckId,
    CheckResult,
    SkipReason,
    ValidationReport,
    thresholds_for,
)
from ..planning.grid_layout import aspect_mismatch
from ..processing.chroma_key import hex_to_rgb
from ..prompts.poses import pose_sequence
from ..storage.hashes import hash_file
from .beat_signature import BeatSignature, check_beat_signature
from .frame_order import UNDETECTABLE_MESSAGE, measure_frame_order
from .metrics import (
    anchor_measurement,
    content_box,
    exact_duplicates,
    height_variation,
    is_blank,
    palette_of,
    silhouette_variation,
    transparent_rgb_residue,
)
from .mirror_flip import detect_mirror_flips

#: 相邻帧差异低于此值即认为"几乎没动"。整组都低于它 → static_animation。
STATIC_THRESHOLD = 0.01



def _skip_reason(action: str) -> SkipReason:
    """几何检查被跳过时，说清楚是**哪一种**跳过。

    两件事长得一样但用户该做的不同：

    - ``action_exempt`` —— ``death`` / ``impact`` 这类**刻意豁免**的动作。
      倒地时身体形变本就是极端的，几何检查无意义，跳过是设计（PLAN §9.1）。
    - ``custom_action_unthresholded`` —— 自定义动作，我们**根本没有阈值**。
      不知道一个 dodge_roll 该有多大高度变化，猜一个数只会产出无意义的红叉。
      用户要靠 contact sheet 人工看。

    混成一个理由，用户会以为自定义动作也是"设计上不需要查"。
    """
    return "action_exempt" if action in ACTION_THRESHOLDS else "custom_action_unthresholded"


def _load_frames(root: Path, paths: list[str]) -> list[np.ndarray]:
    frames = []
    for relative in paths:
        path = root / relative
        if not path.exists():
            raise FileNotFoundError(path)
        frames.append(np.array(Image.open(path).convert("RGBA")))
    return frames


def _action_of(key: str) -> str:
    return split_animation_key(key)[0]


def _direction_of(key: str) -> Direction | None:
    return split_animation_key(key)[1]


def validate_animation(
    root: Path,
    key: str,
    entry: GeneratedAnimation,
    *,
    expected_frames: int,
    expected_size: tuple[int, int],
    max_colors: int,
    key_color: str,
    locomotion: str = "biped",
) -> list[Check]:
    """校验一个生成型动作。"""
    action = _action_of(key)
    direction = _direction_of(key)
    limits = thresholds_for(action, direction, locomotion)
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
        add("anchor_drift", CheckResult.SKIP, skip_reason=_skip_reason(action),
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
            add(check_id, CheckResult.SKIP, skip_reason=_skip_reason(action),
                measured=round(value, 4))
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

    # -- 节拍特征（中）——帧序问题上唯一被实测支撑的自动判据 ---------------
    #
    # 它问的不是"顺序对不对"（那个测不了），而是"第 N 格画的是不是我要的
    # 那一拍"。只在特征经得起投影时给结论 —— 详见 beat_signature 模块。
    signature = _beat_signature_check(frames, action, direction)
    if not signature.applicable:
        add("beat_signature", CheckResult.SKIP, skip_reason="not_applicable",
            message=signature.reason)
    else:
        add(
            "beat_signature",
            CheckResult.PASS if signature.consistent else CheckResult.FAIL,
            measured=round(signature.separation, 3),
            threshold=1.0,
            message=signature.summary() + (
                "" if signature.consistent else
                " —— 要么帧被排错了格子，要么模型没照姿势描述画。看 contact sheet。"
            ),
        )

    # -- 帧间镜像翻转（高）------------------------------------------------
    #
    # 播放时极刺眼，静态看单帧却完全正常。实测 30 个样本检出 4 个，
    # 全部集中在 hurt / attack —— 那两个动作的姿势节拍没说清是哪一边。
    mirror = detect_mirror_flips(frames)
    if not mirror.applicable:
        add("mirror_flip", CheckResult.SKIP, skip_reason="not_applicable",
            message=mirror.summary())
    else:
        add(
            "mirror_flip",
            CheckResult.PASS if not mirror.flipped else CheckResult.FAIL,
            measured=len(mirror.flipped),
            threshold=0,
            message=mirror.summary(),
        )

    return checks


def _beat_signature_check(
    frames: list[np.ndarray], action: str, direction: Direction | None
) -> BeatSignature:
    """取该动作请求的节拍名，与观察到的脚跨度分布对账。

    拿不到节拍（自定义动作没走内置模板、或帧数与节拍对不上）时返回"不适用"，
    不猜。
    """
    try:
        beats = [
            line.split(" —")[0]
            for line in pose_sequence(action, len(frames), direction)
        ]
    except PlanError as exc:
        return BeatSignature(False, f"取不到 {action} 的节拍：{exc}")
    return check_beat_signature(frames, beats, direction=direction)


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


def validate_static_image(
    root: Path,
    entry: StaticImageInfo,
    *,
    expected_size: tuple[int, int],
    palette: list[str],
) -> list[Check]:
    """验证静态成品及其 Manifest 溯源。"""
    target = "static"
    image_path = root / entry.image
    source_path = root / entry.source_image
    image_exists = image_path.is_file()
    source_exists = source_path.is_file()
    checks = [
        Check.make(
            "artifact_exists",
            target,
            CheckResult.PASS if image_exists and source_exists else CheckResult.FAIL,
            measured=image_exists and source_exists,
            threshold=True,
            message=(
                None
                if image_exists and source_exists
                else f"静态产物缺失：成品={image_exists}，原图={source_exists}"
            ),
        )
    ]
    if not image_exists or not source_exists:
        return checks

    source_hash_ok = hash_file(source_path) == entry.source_hash
    image_hash_ok = hash_file(image_path) == entry.processed_hash
    checks.append(
        Check.make(
            "artifact_hash",
            target,
            CheckResult.PASS if source_hash_ok and image_hash_ok else CheckResult.FAIL,
            measured=source_hash_ok and image_hash_ok,
            threshold=True,
            message=(
                None
                if source_hash_ok and image_hash_ok
                else "磁盘内容哈希与 Manifest 不一致"
            ),
        )
    )

    frame = np.array(Image.open(image_path).convert("RGBA"))
    actual_size = (frame.shape[1], frame.shape[0])
    checks.append(
        Check.make(
            "frame_size",
            target,
            CheckResult.PASS if actual_size == expected_size else CheckResult.FAIL,
            measured=actual_size[0] * actual_size[1],
            threshold=expected_size[0] * expected_size[1],
            message=(
                None
                if actual_size == expected_size
                else f"期望 {expected_size}，实际 {actual_size}"
            ),
        )
    )

    blank = is_blank(frame)
    checks.append(
        Check.make(
            "blank_frame",
            target,
            CheckResult.FAIL if blank else CheckResult.PASS,
            measured=blank,
            threshold=False,
            message="静态成品全透明" if blank else None,
        )
    )
    residue = transparent_rgb_residue(frame)
    checks.append(
        Check.make(
            "transparent_rgb_residue",
            target,
            CheckResult.PASS if residue == 0 else CheckResult.FAIL,
            measured=residue,
            threshold=0,
            message=None if residue == 0 else f"{residue} 个透明像素带非零 RGB",
        )
    )

    box = content_box(frame)
    touches_edge = box is not None and (
        box[0] <= 0 or box[1] <= 0 or box[2] >= frame.shape[1] or box[3] >= frame.shape[0]
    )
    checks.append(
        Check.make(
            "content_bounds",
            target,
            CheckResult.FAIL if touches_edge or box is None else CheckResult.PASS,
            measured=bool(touches_edge),
            threshold=False,
            message="主体接触画布边缘，可能已被裁切" if touches_edge else None,
        )
    )

    allowed = {hex_to_rgb(color) for color in palette}
    actual = palette_of([frame])
    outside = actual - allowed if allowed else set()
    checks.append(
        Check.make(
            "palette_membership",
            target,
            CheckResult.PASS if not outside else CheckResult.FAIL,
            measured=len(outside),
            threshold=0,
            message=None if not outside else f"{len(outside)} 个颜色不属于共享 palette",
        )
    )
    return checks


def validate_asset(asset_dir: str | Path) -> ValidationReport:
    """校验一个资产目录下的静态图与全部动作。"""
    root = Path(asset_dir)
    manifest = AssetManifest.load(root / "asset-manifest.json")

    expected_size = (manifest.canvas.width, manifest.canvas.height)
    report = ValidationReport(
        asset_id=manifest.asset_id,
        thresholds_calibrated=THRESHOLDS_CALIBRATED,
    )

    request_frames: dict[str, int] = {}
    request_path = root / "request.yaml"
    locomotion = "biped"
    if request_path.exists():
        from ..models.request import load_request

        request = load_request(request_path)
        locomotion = request.resolved_locomotion
        for spec in request.animation_list():
            request_frames[spec.name] = spec.frames

    if manifest.static_image is not None:
        report.checks.extend(
            validate_static_image(
                root,
                manifest.static_image,
                expected_size=expected_size,
                palette=manifest.palette.colors,
            )
        )
    elif manifest.asset_type in STATIC_ASSET_TYPES and not manifest.animations:
        report.checks.append(
            Check.make(
                "artifact_exists",
                "static",
                CheckResult.FAIL,
                measured=False,
                threshold=True,
                message="静态资产的 Manifest 缺少 static_image",
            )
        )

    for key in sorted(manifest.animations):
        entry = manifest.animations[key]
        if isinstance(entry, DerivedAnimation):
            report.checks.extend(validate_derived(key, entry))
            continue

        # 补过间的动作，帧数**本来就该**多于 request 里写的那个数 ——
        # request 写的是关键帧数，补间的产出是目标帧率下的帧数。
        # 不认这一点的话，任何补过间的资产都会挂在 frame_count 这条致命项上。
        expected = request_frames.get(_action_of(key), len(entry.frames))
        if entry.keyframe_count is not None and len(entry.frames) > entry.keyframe_count:
            expected = len(entry.frames)
        report.checks.extend(
            validate_animation(
                root, key, entry,
                expected_frames=expected,
                expected_size=expected_size,
                max_colors=manifest.palette.max_colors,
                key_color=manifest.background.color_used,
                locomotion=locomotion,
            )
        )
        report.thresholds_used[_action_of(key)] = thresholds_for(
            _action_of(key), _direction_of(key), locomotion
        )

    return report
