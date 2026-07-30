"""背景键控色的冲突预检与降级（PLAN §2.4.1 / ADR-004）。

这一步必须发生在**生成之前** —— 键控色要写进 prompt，图一旦生成出来就晚了。

预检刻意偏保守：换一个键控色的代价接近于零，而把角色本体抠掉的代价是整张图作废。
所以"描述里提到紫色但其实只是个小配饰"这种误判是可以接受的。

降级阶梯（ADR-004，Sprint 0 后由六档缩为五档）：

1. ``tolerant_key`` —— 默认键控色 + 逐图自适应阈值 + Despill（主路径）
2. ``alt_key_color`` —— 冲突时切换备用键控色
3. ``transparent_model`` —— 改用 gpt-image-1.5 重生成，直接请求透明背景
4. ``rembg`` —— 语义抠图
5. ``manual`` —— 人工审核

本模块只负责前两档 —— 它们是"选哪个颜色"的问题，且在**生成前**就要定。
后三档是"已经生成完但抠不干净"的问题，属于处理与修复阶段（Sprint 3 / Sprint 5）。

初版还有一档"默认键控色**精确**色键"排在最前。它已被删除：
实测模型产出的背景是 ``#F204EA`` 这类近洋红，精确 ``#FF00FF`` 命中率 **0.00%**
（Sprint 0 / A-5）。那一档不是"很少成功"，是"永远不成功"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from ..constants import (
    CONFLICT_KEYWORDS,
    DEFAULT_FALLBACK_COLORS,
    DEFAULT_KEY_COLOR,
    FallbackStage,
)
from ..errors import PlanError


@dataclass(frozen=True, slots=True)
class BackgroundDecision:
    """键控色的最终决定。要原样写进 ``manifest.background``。"""

    color_requested: str
    color_used: str
    fallback_stage: FallbackStage
    conflicts: tuple[str, ...] = ()
    """在 ``color_requested`` 上命中的冲突词。空元组表示预检通过。"""

    considered: tuple[tuple[str, tuple[str, ...]], ...] = field(default=())
    """逐个候选色的检查轨迹 ``(color, hit_keywords)``，供 ``plan`` 输出解释原因。"""

    @property
    def downgraded(self) -> bool:
        return self.fallback_stage != "tolerant_key"

    def explain(self) -> str:
        if not self.downgraded:
            return f"键控色 {self.color_used}（无冲突）"
        hits = "、".join(self.conflicts) or "未知"
        return (
            f"键控色由 {self.color_requested} 降级为 {self.color_used}"
            f"（{self.fallback_stage}；冲突词：{hits}）"
        )


@lru_cache(maxsize=64)
def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    """拉丁词按词边界匹配，CJK 词按子串匹配。

    没有词边界会踩到一个很具体的坑：``slime`` 里含 ``lime``，
    于是史莱姆会被判定与纯绿键控色冲突 —— 而它本该降级到的正是纯绿。
    CJK 没有词边界概念，``\\b`` 对中文无效，只能用子串。
    """
    if keyword.isascii():
        return re.compile(rf"\b{re.escape(keyword.lower())}\b")
    return re.compile(re.escape(keyword))


def _hits(text: str, color: str) -> tuple[str, ...]:
    keywords = CONFLICT_KEYWORDS.get(color.upper(), ())
    lowered = text.lower()
    return tuple(kw for kw in keywords if _keyword_pattern(kw).search(lowered))


def resolve_key_color(
    description: str,
    *,
    requested: str = DEFAULT_KEY_COLOR,
    fallbacks: tuple[str, ...] = DEFAULT_FALLBACK_COLORS,
    conflict_hint: str | None = None,
    palette: tuple[str, ...] = (),
) -> BackgroundDecision:
    """挑一个不会把角色本体抠掉的键控色。

    ``palette`` 是已知的目标调色板色值；只要其中出现与候选键控色完全相同的颜色，
    该候选立即出局 —— 这比关键词匹配硬得多，不需要任何启发式。
    """
    text = " ".join(filter(None, (description, conflict_hint)))
    palette_upper = {c.upper() for c in palette}

    candidates = (requested, *fallbacks)
    trail: list[tuple[str, tuple[str, ...]]] = []

    for stage, color in enumerate(candidates, start=1):
        hits = _hits(text, color)
        if color.upper() in palette_upper:
            hits = (*hits, f"调色板中存在同色 {color}")
        trail.append((color, hits))
        if not hits:
            return BackgroundDecision(
                color_requested=requested,
                color_used=color,
                # 用了请求色即主路径；用了任何备用色都是 alt_key_color（ADR-004）。
                fallback_stage="tolerant_key" if stage == 1 else "alt_key_color",
                conflicts=trail[0][1] if stage > 1 else (),
                considered=tuple(trail),
            )

    detail = "；".join(f"{c}（命中 {'、'.join(h)}）" for c, h in trail)
    raise PlanError(
        "全部候选键控色都与角色配色冲突：" + detail + "。"
        "请在 request 的 background.fallback_colors 中显式指定一个不冲突的颜色，"
        "或改用 background.mode: transparent_model。"
    )
