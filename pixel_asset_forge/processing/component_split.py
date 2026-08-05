"""按连通域抽帧 —— 取代固定网格硬切（ADR-003 修订）。

## 为什么换掉固定网格切分

ADR-003 初版选择固定网格、拒绝"自动帧识别"，理由是自动识别的失败模式不可预测，
且会让"生成错误"与"识别错误"无法区分。**这个理由针对的是开放式帧识别** ——
不知道有几帧、要从像素里推断出来。

但本项目的帧数**始终是已知的**（prompt 里写死、request 里声明）。
于是问题从"推断有几个 sprite"降级为"定位 N 个已知目标"，
后者约束强得多，失败模式也可判定（找不到 N 个就明确报错，而不是猜一个数）。

实测代价很具体：固定网格把一张**完全正常**的产出判成了 fatal。
`walk_down` 的 8 个姿势彼此完全分离、没有任何损伤，只是整体相对我假设的格线
右移了一点 —— 固定切分把剑尖切给了隔壁格，验证器报 3 个跨格连通域要求重生成，
而重生成三次拿到的是同样的布局。我在拿一个不存在的缺陷烧配额。

改用连通域抽帧后，同一张图干净地分出 8 个 sprite、0 重叠、0 碎片。

算法照搬 OpenAI `hatch-pet` 的 `extract_strip_frames.py`（MIT），
并从一维横条推广到二维网格 —— 因为 gpt-image-2 的 3:1 长短边比约束
让 8 帧横条（8:1）不可行，我们必须留在二维网格上生成。

## 四种抽帧方式

| 方式 | 做法 | 何时用 |
|---|---|---|
| ``components`` | 连通域定位，找不到 N 个就报错 | 要求严格时 |
| ``auto`` | 先试 ``components``，失败退回 ``stable_slots`` | **默认** |
| ``stable_slots`` | 等分切格，但共用视口保持缩放与基线 | 连通域粘连时 |
| ``slots`` | 等分硬切（原固定网格行为） | 仅作对照 |

``stable_slots`` 而非 ``slots`` 作为兜底是刻意的：逐帧各自 fit-to-cell 会造成
**尺寸跳动与基线抖动**，而共用视口能保住帧间的相对缩放与站位。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import numpy as np
from scipy.ndimage import find_objects, label

from ..errors import ProcessingError
from ..logging_utils import get_logger
from ..planning.grid_layout import GridLayout
from .frame_split import split_grid

logger = get_logger("processing.component_split")

#: 种子候选的面积下限（相对最大连通域）。低于它的当碎片处理。
SEED_AREA_RATIO = 0.20
SEED_AREA_FLOOR = 120

#: 碎片的面积下限。再小就是噪点，直接丢弃。
NOISE_AREA_RATIO = 0.002
NOISE_AREA_FLOOR = 12

#: 共用视口的四周留白。
VIEWPORT_PADDING = 4


class SplitMethod(StrEnum):
    AUTO = "auto"
    COMPONENTS = "components"
    STABLE_SLOTS = "stable_slots"
    SLOTS = "slots"


@dataclass(frozen=True, slots=True)
class Component:
    area: int
    left: int
    top: int
    right: int
    bottom: int
    mask: np.ndarray
    """该连通域在整图坐标系下的布尔掩膜。"""

    @property
    def centre(self) -> tuple[float, float]:
        return ((self.left + self.right) / 2, (self.top + self.bottom) / 2)


@dataclass(frozen=True, slots=True)
class SplitResult:
    frames: list[np.ndarray]
    method: SplitMethod
    fragments_attached: int
    """吸附到 sprite 上的碎片数（剑尖、飘带等与主体断开的部件）。"""

    overlapping_pairs: int
    """包围盒互相重叠的 sprite 对数。>0 说明姿势确实挤在一起了。"""

    def summary(self) -> str:
        return (
            f"{self.method.value} 抽帧：{len(self.frames)} 帧、"
            f"碎片吸附 {self.fragments_attached} 个、重叠 {self.overlapping_pairs} 对"
        )


def _components(mask: np.ndarray) -> list[Component]:
    labels, _count = label(mask)
    out: list[Component] = []
    for index, box in enumerate(find_objects(labels), start=1):
        if box is None:  # pragma: no cover - scipy 不会返回 None
            continue
        piece = labels[box] == index
        out.append(
            Component(
                area=int(piece.sum()),
                left=box[1].start,
                top=box[0].start,
                right=box[1].stop,
                bottom=box[0].stop,
                mask=piece,
            )
        )
    return out


def _reading_order(seeds: list[Component], height: int, rows: int) -> list[Component]:
    """按从左到右、从上到下排序（PLAN §2.3.1）。

    先按行带分组再按 x 排 —— 直接按 y 排会在两行高度略有差异时把顺序打乱。
    """
    band = height / rows
    return sorted(
        seeds,
        key=lambda c: (min(rows - 1, int(c.centre[1] // band)), c.centre[0]),
    )


def group_components(
    mask: np.ndarray, count: int, *, rows: int
) -> list[list[Component]] | None:
    """把连通域聚成 ``count`` 组，每组一个 sprite。

    帧数已知是这个算法成立的前提：直接取面积最大的 N 个连通域作为 sprite 主体，
    再把碎片按中心距离就近吸附。**不使用任何间隙阈值** ——
    间隙阈值在姿势疏密不均时会整片聚错（实测把 8 个 sprite 聚成 2 个）。
    """
    comps = _components(mask)
    if len(comps) < count:
        return None

    largest = max(c.area for c in comps)
    seed_floor = max(SEED_AREA_FLOOR, largest * SEED_AREA_RATIO)
    candidates = [c for c in comps if c.area >= seed_floor]
    if len(candidates) < count:
        candidates = comps

    seeds = sorted(candidates, key=lambda c: c.area, reverse=True)[:count]
    if len(seeds) < count:
        return None
    seeds = _reading_order(seeds, mask.shape[0], rows)

    groups: list[list[Component]] = [[s] for s in seeds]
    seed_ids = {id(s) for s in seeds}
    noise_floor = max(NOISE_AREA_FLOOR, largest * NOISE_AREA_RATIO)

    for comp in comps:
        if id(comp) in seed_ids or comp.area < noise_floor:
            continue
        nearest = min(
            range(count),
            key=lambda i: (seeds[i].centre[0] - comp.centre[0]) ** 2
            + (seeds[i].centre[1] - comp.centre[1]) ** 2,
        )
        groups[nearest].append(comp)

    return groups


def _group_bounds(group: list[Component]) -> tuple[int, int, int, int]:
    return (
        min(c.left for c in group),
        min(c.top for c in group),
        max(c.right for c in group),
        max(c.bottom for c in group),
    )


def _count_overlaps(boxes: list[tuple[int, int, int, int]]) -> int:
    total = 0
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            if not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]):
                total += 1
    return total


def _render_group(rgba: np.ndarray, group: list[Component]) -> np.ndarray:
    """只取这一组连通域的像素，其余留空。

    按连通域取像素而不是按包围盒裁 —— 包围盒可能框进邻居的一角，
    而连通域掩膜保证只拿到属于本 sprite 的像素。
    """
    combined = np.zeros(rgba.shape[:2], dtype=bool)
    for comp in group:
        combined[comp.top : comp.bottom, comp.left : comp.right] |= comp.mask

    out = np.zeros_like(rgba)
    out[combined] = rgba[combined]
    return out


def _shared_viewports(
    rgba: np.ndarray, groups: list[list[Component]], *, rows: int
) -> list[np.ndarray]:
    """把每个 sprite 放进**共用视口**，保住帧间的相对缩放与垂直站位。

    这是 hatch-pet ``stable-slots`` 的核心：逐帧各自裁到自己的包围盒再缩放，
    每帧缩放比例都不同 —— 播放起来角色一大一小地跳。共用视口消除这个问题。

    **垂直范围必须按网格行分别计算。** hatch-pet 的 strip 是一维横条，
    全局共用上下界是对的；本项目是二维网格，如果跨行取公共上下界，
    视口会高达两行之和：实测 8 帧 4×2 网格上视口 859px 高、而每个 sprite 只有约
    400px —— 缩到 32×32 后角色只占画布 47%，白白丢掉一半分辨率。

    物理行偏移是**布局产物**，不是动画本身的属性。所以按行归一化：
    每个 sprite 相对**它自己那一行**的公共上界定位，行内与跨行的基线都保住。
    """
    boxes = [_group_bounds(g) for g in groups]
    height = rgba.shape[0]
    band = height / rows

    row_of = [min(rows - 1, int(((b[1] + b[3]) / 2) // band)) for b in boxes]
    row_top: dict[int, int] = {}
    row_bottom: dict[int, int] = {}
    for row, box in zip(row_of, boxes, strict=True):
        row_top[row] = min(row_top.get(row, box[1]), box[1])
        row_bottom[row] = max(row_bottom.get(row, box[3]), box[3])

    viewport_height = max(
        row_bottom[r] - row_top[r] for r in row_top
    ) + VIEWPORT_PADDING * 2
    viewport_width = max(b[2] - b[0] for b in boxes) + VIEWPORT_PADDING * 2

    frames: list[np.ndarray] = []
    for group, box, row in zip(groups, boxes, row_of, strict=True):
        rendered = _render_group(rgba, group)
        viewport = np.zeros((viewport_height, viewport_width, 4), dtype=rgba.dtype)

        top = max(0, box[1])
        bottom = min(height, box[3])
        piece = rendered[top:bottom, box[0] : box[2]]

        # 相对本行公共上界定位 —— 行内的高低起伏保留，行间的偏移抹掉。
        y = VIEWPORT_PADDING + (box[1] - row_top[row])
        x = (viewport_width - piece.shape[1]) // 2
        y_end = min(viewport_height, y + piece.shape[0])
        viewport[y:y_end, x : x + piece.shape[1]] = piece[: y_end - y]
        frames.append(viewport)

    return frames


def _slot_frames(rgba: np.ndarray, layout: GridLayout, *, stable: bool) -> list[np.ndarray]:
    """等分切格。``stable=True`` 时共用垂直范围，避免基线抖动。"""
    crops = split_grid(rgba, layout)

    if not stable:
        return crops

    tops, bottoms = [], []
    for crop in crops:
        ys = np.nonzero(crop[:, :, 3])[0]
        if ys.size:
            tops.append(int(ys.min()))
            bottoms.append(int(ys.max()) + 1)
    if not tops:
        return [np.ascontiguousarray(c) for c in crops]

    top = max(0, min(tops) - VIEWPORT_PADDING)
    bottom = min(crops[0].shape[0], max(bottoms) + VIEWPORT_PADDING)
    return [np.ascontiguousarray(c[top:bottom]) for c in crops]


def split_frames(
    rgba: np.ndarray,
    layout: GridLayout,
    *,
    method: SplitMethod | str = SplitMethod.AUTO,
) -> SplitResult:
    """把一张动作网格图切成 ``layout.frames`` 帧。"""
    if rgba.ndim != 3 or rgba.shape[2] != 4:
        raise ProcessingError(f"split_frames 需要 RGBA，收到 {rgba.shape}")

    chosen = SplitMethod(method)
    mask = rgba[:, :, 3] > 0

    if chosen in (SplitMethod.AUTO, SplitMethod.COMPONENTS):
        groups = group_components(mask, layout.frames, rows=layout.rows)
        if groups is not None:
            boxes = [_group_bounds(g) for g in groups]
            return SplitResult(
                frames=_shared_viewports(rgba, groups, rows=layout.rows),
                method=SplitMethod.COMPONENTS,
                fragments_attached=sum(len(g) for g in groups) - len(groups),
                overlapping_pairs=_count_overlaps(boxes),
            )
        if chosen is SplitMethod.COMPONENTS:
            raise ProcessingError(
                f"找不到 {layout.frames} 个 sprite 连通域。"
                "姿势可能粘连成一片，或帧数与实际画出的不符 —— "
                "换 stable_slots 抽帧，或检查生成结果。"
            )
        logger.warning(
            "连通域抽帧失败（找不到 %d 个 sprite），退回 stable_slots", layout.frames
        )
        chosen = SplitMethod.STABLE_SLOTS

    return SplitResult(
        frames=_slot_frames(rgba, layout, stable=chosen is SplitMethod.STABLE_SLOTS),
        method=chosen,
        fragments_attached=0,
        overlapping_pairs=0,
    )


def describe_methods() -> dict[str, Any]:
    """供 CLI / 文档展示。"""
    return {
        "auto": "先试连通域，失败退回 stable_slots（默认）",
        "components": "连通域定位，找不到 N 个即报错",
        "stable_slots": "等分切格 + 共用视口，保住缩放与基线",
        "slots": "等分硬切（原固定网格行为，仅作对照）",
    }
