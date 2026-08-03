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
from ..errors import PlanError, ProcessingError
from ..models.manifest import (
    AssetManifest,
    DerivedAnimation,
    GeneratedAnimation,
    StaticImageInfo,
    TilesetInfo,
)
from ..models.request import STATIC_ASSET_TYPES
from ..models.validation import (
    ALL_CHECK_IDS,
    Check,
    CheckId,
    CheckResult,
    SkipReason,
    ValidationReport,
    thresholds_for,
)
from ..planning.grid_layout import GridLayout, aspect_mismatch
from ..processing.bounds import OverflowReport, detect_overflow
from ..processing.chroma_key import apply_chroma_key, hex_to_rgb
from ..processing.palette import palette_overflow_ratio
from ..processing.pixel_cleanup import count_isolated
from ..processing.pixel_grid import (
    KEY_RESIDUE_WARN_RATIO,
    snap_rgba_to_grid,
    strip_key_residue,
)
from ..prompts.poses import pose_sequence
from ..storage.hashes import hash_file
from .adjacency import derive_adjacency
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
from .seamless import measure_seamless

#: 相邻帧差异低于此值即认为"几乎没动"。整组都低于它 → static_animation。
STATIC_THRESHOLD = 0.01

_STATIC_APPLICABLE_CHECKS: frozenset[CheckId] = frozenset(
    {
        "artifact_exists",
        "artifact_hash",
        "frame_size",
        "blank_frame",
        "cell_overflow",
        "content_bounds",
        "palette_membership",
        "transparent_rgb_residue",
        "partial_alpha",
        "isolated_pixel",
        "key_color_residue",
        "palette_overflow",
    }
)



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


#: tileset 上真正会跑的检查。其余的在报告里显式记为不适用，
#: 理由同静态家族：不能把"没运行"悄悄呈现成"零跳过"。
_TILESET_APPLICABLE_CHECKS: frozenset[CheckId] = frozenset(
    {
        "artifact_exists", "frame_size", "palette_membership",
        "tile_seam", "tile_border", "tile_adjacency", "map_adjacency",
    }
)


def _complete_tileset_checks(checks: list[Check]) -> list[Check]:
    """tileset 报告同样必须列全防线。"""
    present = {check.id for check in checks}
    for check_id in ALL_CHECK_IDS:
        if check_id in present or check_id in _TILESET_APPLICABLE_CHECKS:
            continue
        checks.append(
            Check.make(
                check_id,
                "tileset",
                CheckResult.SKIP,
                skip_reason="not_applicable",
                message="tileset 没有动画帧序列，也不做去背景，此检查不适用",
            )
        )
    return checks


def _complete_static_checks(checks: list[Check]) -> list[Check]:
    """静态报告必须显式列出全部防线，不能把未运行误报成零跳过。"""
    present = {check.id for check in checks}
    for check_id in ALL_CHECK_IDS:
        if check_id in present:
            continue
        applicable = check_id in _STATIC_APPLICABLE_CHECKS
        checks.append(
            Check.make(
                check_id,
                "static",
                CheckResult.SKIP,
                skip_reason="dependency_failed" if applicable else "static_asset",
                message=(
                    "静态产物或溯源依赖缺失，无法运行此检查"
                    if applicable
                    else "静态资产没有动画帧序列，此检查不适用"
                ),
            )
        )
    return checks


def _static_source_quality(
    source_path: Path,
    entry: StaticImageInfo,
    *,
    key_color: str,
) -> tuple[float, OverflowReport]:
    """从原图重放量化前阶段，独立复核键控残留与源构图边界。"""
    source = np.array(Image.open(source_path).convert("RGB"))
    key = hex_to_rgb(key_color)
    keyed = apply_chroma_key(source, key, threshold=entry.key_threshold)
    prequant = keyed.rgba
    if entry.grid_block_size is not None:
        prequant = snap_rgba_to_grid(
            prequant,
            block_size=entry.grid_block_size,
        ).image

    cleaned, key_residue = strip_key_residue(prequant, key)
    height, width = cleaned.shape[:2]
    layout = GridLayout(frames=1, cols=1, rows=1, cell=(width, height))
    overflow = detect_overflow(cleaned[:, :, 3] > 0, layout)
    return key_residue, overflow


#: 无缝判据的工程默认值。**未用真实 tile 校准**（同 §9.1 的口径）。
#: 四张合成 tile 实测分离度：可平铺 0.96/0.19，渐变 31.67，带边框 11.69，暗角 11.84 ——
#: 两侧余量都在一个量级以上，先用着。
TILE_SEAM_RATIO_MAX = 3.0
TILE_BORDER_DEVIATION_MAX = 2.0


def validate_tileset(
    root: Path,
    entry: TilesetInfo,
    *,
    palette: list[str],
) -> list[Check]:
    """验证整套 tile：尺寸、产物、以及两条无缝判据。

    两条判据各抓一种失败，缺一不可（PLAN §8.1）：``tile_seam`` 抓"对边接不上"，
    ``tile_border`` 抓"带边框 / 暗角" —— 后者接缝处是连续的，接缝判据对它恒判通过。
    """
    checks: list[Check] = []
    expected = entry.tile_size
    # 邻接要拿全套 tile 一起重算，所以边验边收 —— 只收尺寸也对的，
    # 尺寸不对的那块连接缝方向的长度都对不上，喂进去只会炸在无关的地方。
    usable: dict[str, np.ndarray] = {}
    for tile_id, tile in sorted(entry.tiles.items()):
        image_path = root / tile.image
        exists = image_path.is_file()
        # 通过时也要记一笔 —— 只在缺失时才发出的检查项，会在顺利路径上
        # 从报告里整条消失，那正是"列全防线"要防的事。
        checks.append(
            Check.make(
                "artifact_exists",
                tile_id,
                CheckResult.PASS if exists else CheckResult.FAIL,
                measured=exists,
                threshold=True,
                message=None if exists else f"tile 成品缺失：{image_path}",
            )
        )
        if not exists:
            continue

        image = np.array(Image.open(image_path).convert("RGBA"))
        actual = (image.shape[1], image.shape[0])
        if actual == expected:
            usable[tile_id] = image
        checks.append(
            Check.make(
                "frame_size",
                tile_id,
                CheckResult.PASS if actual == expected else CheckResult.FAIL,
                message=(
                    None
                    if actual == expected
                    else f"tile 尺寸 {actual} ≠ {expected}，整张地图的网格会错位"
                ),
            )
        )

        measured = measure_seamless(image)
        seam = measured.worst_seam_ratio
        checks.append(
            Check.make(
                "tile_seam",
                tile_id,
                CheckResult.PASS if seam <= TILE_SEAM_RATIO_MAX else CheckResult.FAIL,
                measured=round(seam, 3),
                threshold=TILE_SEAM_RATIO_MAX,
                message=(
                    None
                    if seam <= TILE_SEAM_RATIO_MAX
                    else "对边接不上：平铺后每隔一格会有一道可见的突变"
                ),
            )
        )
        border = measured.border_deviation
        checks.append(
            Check.make(
                "tile_border",
                tile_id,
                CheckResult.PASS
                if border <= TILE_BORDER_DEVIATION_MAX
                else CheckResult.FAIL,
                measured=round(border, 3),
                threshold=TILE_BORDER_DEVIATION_MAX,
                message=(
                    None
                    if border <= TILE_BORDER_DEVIATION_MAX
                    else "整圈边缘明显不同于中心（边框或暗角）：平铺后是一片规则网格线"
                ),
            )
        )
        if palette:
            outside = palette_of([image]) - {hex_to_rgb(color) for color in palette}
            checks.append(
                Check.make(
                    "palette_membership",
                    tile_id,
                    CheckResult.PASS if not outside else CheckResult.FAIL,
                    measured=len(outside),
                    threshold=0,
                    message=(
                        None
                        if not outside
                        else f"{len(outside)} 个颜色不属于整套共享 palette"
                    ),
                )
            )

    checks.extend(_check_tile_adjacency(entry, usable))
    checks.extend(_check_maps(root, entry))
    return checks


def _map_violations(
    rows: list[list[str]], entry: TilesetInfo
) -> tuple[list[str], list[str]]:
    """地图里所有非法的相邻对，水平与垂直**分开**返回。

    分开不是为了报错信息好看，是为了这条检查有判别力：横着查得再仔细，
    竖着接错的地图照样满分。两个方向必须各自被反例验过（PLAN §8.3）。
    """
    assert entry.adjacency is not None
    known = set(entry.tiles)
    horizontal: list[str] = []
    vertical: list[str] = []
    for y, row in enumerate(rows):
        for x, tile in enumerate(row):
            if tile not in known:
                horizontal.append(f"({x},{y}) 是不存在的 tile：{tile}")
                continue
            if x + 1 < len(row) and row[x + 1] not in entry.adjacency.neighbours(
                tile, "right"
            ):
                horizontal.append(f"({x},{y}){tile} | {row[x + 1]}")
            if y + 1 < len(rows) and rows[y + 1][x] not in entry.adjacency.neighbours(
                tile, "down"
            ):
                vertical.append(f"({x},{y}){tile} / {rows[y + 1][x]}")
    return horizontal, vertical


def _check_maps(root: Path, entry: TilesetInfo) -> list[Check]:
    """每张地图的每一对相邻格都必须出现在邻接表里（PLAN §8.3）。

    这是 8.3 唯一有判别力的检查：一个写错的求解器产出的地图，尺寸、格子填满、
    tile 都认识 —— 全都对，只有这条会露馅。

    通过时也要记一笔，理由同 8.1 的 ``artifact_exists``。
    """
    if not entry.maps:
        return [
            Check.make(
                "map_adjacency",
                "tileset",
                CheckResult.SKIP,
                skip_reason="not_applicable",
                message="这套 tile 还没铺过地图（跑 `create-map`，不调用 API）",
            )
        ]
    if entry.adjacency is None:
        return [
            Check.make(
                "map_adjacency",
                "tileset",
                CheckResult.SKIP,
                skip_reason="dependency_failed",
                message="Manifest 里没有邻接表，地图合法性无从判起",
            )
        ]

    checks: list[Check] = []
    for name, map_entry in sorted(entry.maps.items()):
        try:
            rows = map_entry.load_rows(root)
        except (OSError, ProcessingError, ValueError) as exc:
            checks.append(
                Check.make(
                    "map_adjacency", name, CheckResult.FAIL,
                    message=f"地图读不出来：{exc}",
                )
            )
            continue

        size = (len(rows[0]), len(rows))
        if size != (map_entry.width, map_entry.height):
            checks.append(
                Check.make(
                    "map_adjacency", name, CheckResult.FAIL,
                    message=f"地图实际 {size[0]}×{size[1]}，Manifest 记的是 "
                            f"{map_entry.width}×{map_entry.height}",
                )
            )
            continue

        horizontal, vertical = _map_violations(rows, entry)
        total = len(horizontal) + len(vertical)
        detail = "；".join(
            part
            for part in (
                f"水平 {len(horizontal)} 处（如 {horizontal[0]}）" if horizontal else "",
                f"垂直 {len(vertical)} 处（如 {vertical[0]}）" if vertical else "",
            )
            if part
        )
        checks.append(
            Check.make(
                "map_adjacency",
                name,
                CheckResult.PASS if total == 0 else CheckResult.FAIL,
                measured=total,
                threshold=0,
                message=(
                    None
                    if total == 0
                    else f"地图里有非法接缝：{detail} —— 平铺后这些位置会是可见的断裂"
                ),
            )
        )
    return checks


def _check_tile_adjacency(
    entry: TilesetInfo, images: dict[str, np.ndarray]
) -> list[Check]:
    """邻接表说的话，与盘上的像素现在说的话，是否还是同一句（PLAN §8.2）。

    **用 Manifest 里记着的阈值重算，不用当前默认值。** 产物是在那组阈值下做出来的，
    以后改了默认值不该反过来把旧产物判成坏的 —— 那不是漂移，是记录得诚实。
    这条检查抓的是真会裂开的那道缝：产出之后有人动过 tile 图、手改过 Manifest、
    或者推导本身有 bug。

    通过时也要记一笔 —— 只在出问题时才发出的检查项会在顺利路径上从报告里整条
    消失，8.1 的 ``artifact_exists`` 踩的就是这个坑。
    """
    if entry.adjacency is None:
        return [
            Check.make(
                "tile_adjacency",
                "tileset",
                CheckResult.SKIP,
                skip_reason="not_applicable",
                message="这套 tile 的 Manifest 里没有邻接表（8.1 及更早的产物）",
            )
        ]
    if set(images) != set(entry.tiles):
        missing = sorted(set(entry.tiles) - set(images))
        return [
            Check.make(
                "tile_adjacency",
                "tileset",
                CheckResult.SKIP,
                skip_reason="dependency_failed",
                message=f"这些 tile 缺失或尺寸不符，邻接无从重算：{missing}",
            )
        ]

    fresh = derive_adjacency(
        images,
        seam_max=entry.adjacency.seam_ratio_max,
        gap_max=entry.adjacency.edge_color_gap_max,
    )
    checks: list[Check] = []
    for tile_id in sorted(images):
        drifted = [
            direction
            for direction, recomputed in (("right", fresh.right), ("down", fresh.down))
            if sorted(entry.adjacency.neighbours(tile_id, direction))
            != sorted(recomputed[tile_id])
        ]
        checks.append(
            Check.make(
                "tile_adjacency",
                tile_id,
                CheckResult.PASS if not drifted else CheckResult.FAIL,
                measured=len(drifted),
                threshold=0,
                message=(
                    None
                    if not drifted
                    else f"邻接表的 {'、'.join(drifted)} 方向与当前像素对不上 —— "
                    "重跑 `create-tileset` 重算（不调用 API、不产生计费）"
                ),
            )
        )
    return checks


def validate_static_image(
    root: Path,
    entry: StaticImageInfo,
    *,
    expected_size: tuple[int, int],
    palette: list[str],
    max_colors: int,
    key_color: str,
    antialiasing: bool | None,
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
        return _complete_static_checks(checks)

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

    alpha = frame[:, :, 3]
    partial_alpha = int(((alpha > 0) & (alpha < 255)).sum())
    if antialiasing is None:
        checks.append(
            Check.make(
                "partial_alpha",
                target,
                CheckResult.SKIP,
                measured=partial_alpha,
                skip_reason="dependency_failed",
                message="request.yaml 缺失，无法确认 antialiasing 约束",
            )
        )
    else:
        partial_alpha_failed = not antialiasing and partial_alpha > 0
        checks.append(
            Check.make(
                "partial_alpha",
                target,
                CheckResult.FAIL if partial_alpha_failed else CheckResult.PASS,
                measured=partial_alpha,
                threshold=0 if not antialiasing else None,
                message=(
                    f"antialiasing=false，但有 {partial_alpha} 个半透明像素（本地可修）"
                    if partial_alpha_failed
                    else (
                        "request 允许 antialiasing，半透明 alpha 不构成违规"
                        if antialiasing and partial_alpha
                        else None
                    )
                ),
            )
        )

    isolated = count_isolated(frame)
    checks.append(
        Check.make(
            "isolated_pixel",
            target,
            CheckResult.PASS if isolated == 0 else CheckResult.FAIL,
            measured=isolated,
            threshold=0,
            message=None if isolated == 0 else f"{isolated} 个四面无邻的孤立像素（本地可修）",
        )
    )

    source_key_residue, source_overflow = _static_source_quality(
        source_path,
        entry,
        key_color=key_color,
    )
    # 两级判据，别把这两件事混成一个数（真实生成实测暴露的误报）：
    #
    # - **成品**里还有过近键控色的不透明像素 → FAIL。处理链在量化**前**就跑过
    #   ``strip_key_residue`` 把它们删成透明了，成品里还剩就说明真没清干净
    #   （例如显式色板里有近洋红色，量化又把像素映射了回去）。阈值是 0，不是 5%。
    # - **量化前**删掉的比例高 → WARN。这个比例的大头是「被前景围住的封闭背景
    #   区域」—— 钥匙的圆环孔、两腿之间、弓的弯里 —— 色键的漫水填充只清与画布
    #   外缘连通的部分，这些本来就该在那一步删掉（见 ``strip_key_residue`` 文档）。
    #   实测一个**完全合格**的金钥匙图标在这里报 6.4%，判 FAIL 就把它挡在了导出
    #   之外。常量名 ``KEY_RESIDUE_WARN_RATIO`` 写的就是 WARN，pipeline 里也一直
    #   只当告警用。"一个天天误报的验证器最终会被开发者关掉"（PLAN §9.1/§9.2）。
    _, final_key_residue = strip_key_residue(frame, hex_to_rgb(key_color))
    if final_key_residue > 0:
        residue_result = CheckResult.FAIL
        residue_message = (
            f"成品里仍有 {final_key_residue:.1%} 的前景像素过近键控色 —— "
            "量化前已经清过一遍，这里还剩说明没清干净"
        )
    elif source_key_residue > KEY_RESIDUE_WARN_RATIO:
        residue_result = CheckResult.WARN
        residue_message = (
            f"量化前删掉了 {source_key_residue:.1%} 的前景像素（过近键控色）。"
            "大头通常是被前景围住的封闭背景区（孔洞、两腿之间），属正常；"
            "但比例这么高也可能是主体配色与键控色撞了 —— 请看 contact sheet 确认"
        )
    else:
        residue_result = CheckResult.PASS
        residue_message = None
    checks.append(
        Check.make(
            "key_color_residue",
            target,
            residue_result,
            measured=round(max(source_key_residue, final_key_residue), 4),
            threshold=KEY_RESIDUE_WARN_RATIO,
            message=residue_message,
        )
    )

    source_touches_edge = source_overflow.min_margin <= 0
    checks.append(
        Check.make(
            "cell_overflow",
            target,
            CheckResult.FAIL if source_touches_edge else CheckResult.PASS,
            measured=round(source_overflow.min_margin, 4),
            threshold=0.0,
            message=(
                None
                if not source_touches_edge
                else "主体在原图上接触画布边缘，抠图前可能已被裁切，必须重生成"
            ),
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

    actual = palette_of([frame])
    if palette:
        allowed = {hex_to_rgb(color) for color in palette}
        outside = actual - allowed
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

        outside_ratio = palette_overflow_ratio([frame], palette)
        color_count = len(actual)
        color_limit_ratio = float(color_count > max_colors)
        overflow_ratio = max(outside_ratio, color_limit_ratio)
        palette_failed = (
            outside_ratio > PALETTE_OVERFLOW_MAX or color_count > max_colors
        )
        checks.append(
            Check.make(
                "palette_overflow",
                target,
                CheckResult.FAIL if palette_failed else CheckResult.PASS,
                measured=round(overflow_ratio, 4),
                threshold=PALETTE_OVERFLOW_MAX,
                message=(
                    f"越界像素 {outside_ratio:.1%}；{color_count} 色 / 上限 {max_colors}"
                    + ("（本地可修）" if palette_failed else "")
                ),
            )
        )
    else:
        for check_id in ("palette_membership", "palette_overflow"):
            checks.append(
                Check.make(
                    check_id,
                    target,
                    CheckResult.SKIP,
                    skip_reason="dependency_failed",
                    message="Manifest 没有声明 palette，无法复核调色板约束",
                )
            )
    return _complete_static_checks(checks)


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
    antialiasing: bool | None = None
    if request_path.exists():
        from ..models.request import load_request

        request = load_request(request_path)
        locomotion = request.resolved_locomotion
        antialiasing = request.style.antialiasing
        for spec in request.animation_list():
            request_frames[spec.name] = spec.frames

    if manifest.tileset is not None:
        report.checks.extend(
            _complete_tileset_checks(
                validate_tileset(
                    root, manifest.tileset, palette=manifest.palette.colors
                )
            )
        )
    elif manifest.static_image is not None:
        report.checks.extend(
            validate_static_image(
                root,
                manifest.static_image,
                expected_size=expected_size,
                palette=manifest.palette.colors,
                max_colors=manifest.palette.max_colors,
                key_color=manifest.background.key_color,
                antialiasing=antialiasing,
            )
        )
    elif manifest.asset_type in STATIC_ASSET_TYPES and not manifest.animations:
        report.checks.extend(
            _complete_static_checks(
                [
                    Check.make(
                        "artifact_exists",
                        "static",
                        CheckResult.FAIL,
                        measured=False,
                        threshold=True,
                        message="静态资产的 Manifest 缺少 static_image",
                    )
                ]
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
                key_color=manifest.background.key_color,
                locomotion=locomotion,
            )
        )
        report.thresholds_used[_action_of(key)] = thresholds_for(
            _action_of(key), _direction_of(key), locomotion
        )

    return report
