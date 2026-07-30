"""全局常量：API 尺寸约束、网格档位、per-action 阈值、默认参数。

这个模块是**唯一的真实来源**。网格表、阈值表在 PLAN / ADR / schema 里各出现一次，
但运行时只允许从这里读——避免三处各改一半。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

# ---------------------------------------------------------------------------
# gpt-image-2 尺寸约束（PLAN §2.3 / ADR-003）
# ---------------------------------------------------------------------------

SIZE_MULTIPLE: Final = 16
MAX_ASPECT_RATIO: Final = 3.0
MIN_TOTAL_PIXELS: Final = 655_360
MAX_TOTAL_PIXELS: Final = 8_294_400
MAX_SIDE: Final = 3840


# ---------------------------------------------------------------------------
# 网格布局（PLAN §2.3 表）
# ---------------------------------------------------------------------------

CELL_SIZE: Final = 512
"""单元格统一 512×512。所有网格档位都由它推导物理尺寸。"""

SEED_SIZE: Final = (1024, 1024)
"""种子图固定 1024×1024（1,048,576 px，合规）。"""

GRID_LAYOUTS: Final[dict[int, tuple[int, int]]] = {
    4: (2, 2),
    6: (3, 2),
    8: (4, 2),
    9: (3, 3),
    12: (4, 3),
}
"""帧数 → (cols, rows)。只有这五个档位能映射到合规的物理尺寸。"""

ALLOWED_FRAME_COUNTS: Final = tuple(sorted(GRID_LAYOUTS))

STRIP_LAYOUTS: Final[dict[int, tuple[int, int]]] = {
    1: (1024, 1024),
    2: (640, 640),
    3: (480, 640),
    4: (480, 640),
    5: (384, 640),
    6: (320, 640),
}
"""单行条带的 帧数 → 名义单元格 (宽, 高)。整幅图一律 1920×640。

**一张图只放一条完整的帧序列**，模型才会把它当成一个连续循环来画，
而不是八张彼此独立的立绘 —— 后者会让角色在帧之间时而偏左、时而偏右。

为什么只有 4 和 6：单行 N 格的整幅长宽比是 ``N × 格子宽高比``，
而 API 限制长短边比 ≤ 3，所以格子宽高比必须 ≤ 3/N。

    4 帧 → ≤ 0.75  宽松
    6 帧 → ≤ 0.50  刚好
    8 帧 → ≤ 0.375 装不下带跨步和佩剑的角色

这个比例与图放多大无关 —— 把图整体放大并不会松绑，所以 8 帧单行无解。
要单行就把动作降到 6 帧，要 8 帧就仍用 4×2 双行。
"""


# ---------------------------------------------------------------------------
# 逻辑尺寸与方向
# ---------------------------------------------------------------------------

LOGICAL_SIZES: Final = (16, 24, 32, 48, 64, 96, 128)
"""允许的输出画布边长。

128 是为**导入用户自有素材**加的：用户导出的像素资产常常比模型产出大得多
（实测一张 256×256 素材里角色有 205px 高），封在 96 会白白丢掉一半细节。
生成路径用不到它 —— 模型原生只画到 80 逻辑像素上下。"""

DEFAULT_TARGET_SIZE: Final = (48, 48)
"""默认逻辑尺寸。

**不是 32×32。** 实测 gpt-image-2 画出的角色原生约 80 逻辑像素高
（它按约 5–8px 的块作画），压到 32×32 要丢掉六成细节，脸与武器直接糊掉。
48 是参考站的常见规格（CraftPix 资产包典型 48×48、SpriteCook 用 66×66），
也是本流水线实测能稳定产出可读资产的下限。详见 processing/pixel_grid.py。
"""

Direction = Literal["down", "left", "right", "up"]
DIRECTIONS: Final[tuple[Direction, ...]] = ("down", "left", "right", "up")

GENERATION_ORDER: Final[tuple[Direction, ...]] = ("down", "left", "up", "right")
"""优先试错顺序（PLAN §8 Sprint 4）：up（背面）身份一致性最难，应尽早暴露。"""

MIRROR_PAIR: Final[dict[Direction, Direction]] = {"left": "right", "right": "left"}


# ---------------------------------------------------------------------------
# 背景与键控（PLAN §2.4 / ADR-004）
# ---------------------------------------------------------------------------

DEFAULT_KEY_COLOR: Final = "#FF00FF"
DEFAULT_FALLBACK_COLORS: Final = ("#00FF00", "#00FFFF")

CONFLICT_KEYWORDS: Final[dict[str, tuple[str, ...]]] = {
    "#FF00FF": (
        "magenta", "pink", "violet", "purple", "fuchsia", "lilac", "mauve",
        "洋红", "品红", "粉", "紫",
    ),
    "#00FF00": (
        "green", "lime", "emerald", "jade", "olive", "moss",
        "绿",
    ),
    "#00FFFF": (
        "cyan", "aqua", "turquoise", "teal", "azure",
        "青", "蓝绿",
    ),
}
"""键控色 → 会与之撞色的描述词。用于生成**前**的冲突预检（PLAN §2.4.1）。

只做保守的关键词匹配：宁可多降一级键控色（代价为零），也不要把角色本体抠掉。
"""

FallbackStage = Literal[
    "tolerant_key", "alt_key_color", "transparent_model", "rembg", "manual"
]

FALLBACK_STAGES: Final[tuple[FallbackStage, ...]] = (
    "tolerant_key",       # 默认键控色 + 逐图自适应阈值 + Despill —— 主路径
    "alt_key_color",      # 冲突预检命中，切换备用键控色
    "transparent_model",  # 改用 gpt-image-1.5 请求透明背景
    "rembg",              # 语义抠图
    "manual",             # 人工审核
)
"""背景处理降级阶梯（ADR-004）。

用具名标识而非序号：增删档位时序号会整体平移，已落盘的 Manifest 会被静默误读。
初版的「精确色键」档已按 Sprint 0 / A-5 删除 —— 实测精确 #FF00FF 命中率为零。
"""

ESCALATED_STAGES: Final[frozenset[str]] = frozenset(
    {"transparent_model", "rembg", "manual"}
)
"""生成后才会升到的档位。触发即应在验证报告中告警。"""


# ---------------------------------------------------------------------------
# 动作默认值（SKILL.md 参数补全表）
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ActionDefault:
    """一个动作的默认参数。

    用 dataclass 而非 dict：``dict[str, object]`` 会让每个取值点都退化成
    ``int(d["frames"])  # type: ignore``，类型检查在最需要它的地方失效。
    """

    frames: int
    fps: int
    loop: bool
    directional: bool


ACTION_DEFAULTS: Final[dict[str, ActionDefault]] = {
    #                        帧数  fps  loop   有方向
    "idle":   ActionDefault(4,   6,  True,  True),
    "walk":   ActionDefault(8,  10,  True,  True),
    "attack": ActionDefault(6,  12,  False, True),
    "hurt":   ActionDefault(4,   8,  False, True),
    "death":  ActionDefault(8,   8,  False, True),
    "cast":   ActionDefault(6,  10,  False, True),
    "travel": ActionDefault(4,  12,  True,  True),
    # 爆炸是各向同性的，不该被强行摊成四个方向。
    "impact": ActionDefault(6,  15,  False, False),
    "loop":   ActionDefault(4,   8,  True,  False),
}

MVP_ACTIONS: Final = ("idle", "walk")
"""MVP 范围（PLAN §8 Sprint 6）。attack/hurt/death 推迟到 Sprint 6.5。"""


# ---------------------------------------------------------------------------
# per-action 验证阈值（PLAN §9.1）
# ---------------------------------------------------------------------------


class ActionThresholds:
    """单个动作的阈值集合。None 表示该项对此动作不检查。"""

    __slots__ = ("anchor_drift_max_px", "height_variation_max", "silhouette_variation_max")

    def __init__(
        self,
        height_variation_max: float | None,
        silhouette_variation_max: float | None,
        anchor_drift_max_px: int | None,
    ) -> None:
        self.height_variation_max = height_variation_max
        self.silhouette_variation_max = silhouette_variation_max
        self.anchor_drift_max_px = anchor_drift_max_px

    def as_dict(self) -> dict[str, float | int | None]:
        return {
            "height_variation_max": self.height_variation_max,
            "silhouette_variation_max": self.silhouette_variation_max,
            "anchor_drift_max_px": self.anchor_drift_max_px,
        }


ACTION_THRESHOLDS: Final[dict[str, ActionThresholds]] = {
    #                        高度变化   轮廓面积变化  锚点漂移
    "idle":   ActionThresholds(0.06, 0.10, 1),
    "walk":   ActionThresholds(0.12, 0.20, 1),
    "attack": ActionThresholds(0.30, 0.45, 3),
    "hurt":   ActionThresholds(0.25, 0.35, 2),
    # 倒地是形变的极端情况，几何检查无意义 —— 只做人工审核（PLAN §9.1）。
    "death":  ActionThresholds(None, None, None),
    "cast":   ActionThresholds(0.30, 0.45, 3),
    # 特效类：飞行/爆炸形变剧烈，用角色阈值会全面误报。
    "travel": ActionThresholds(0.40, 0.60, 4),
    "impact": ActionThresholds(None, None, None),
    "loop":   ActionThresholds(0.20, 0.30, 2),
}

DIRECTION_MULTIPLIER: Final[dict[Direction, float]] = {
    "down": 1.0,
    "left": 1.0,
    "right": 1.0,
    # 背面缺少正面细节，轮廓天然更不稳定（假设 A-7，PLAN §9.1）。
    "up": 1.3,
}

PALETTE_OVERFLOW_MAX: Final = 0.02
"""调色板越界率上限（PLAN §9.2）。不分动作。"""

FRAME_ORDER_JUMP_MAX: Final = 1
"""帧序连续性：允许的差异突变点数上限。仅对 loop=true 的动作启用。"""

THRESHOLDS_CALIBRATED: Final = False
"""上面的阈值仍是**初始值**（PLAN §9.1）。

Sprint 5 尝试过校准，未完成 —— 真实样本只有 3 组、其中合格的只有 2 组，
而 P95 需要的是分布。详见 `docs/threshold-calibration.md`。

已知的两处疑点（证据不足以支撑改动，但足以指出下次该往哪看）：

- ``silhouette_variation ≤ 0.20`` 在一个**人工判定合格**的样本上实测 0.222，
  已经在误报；
- ``up`` 方向的 ×1.3 修正系数方向可能相反 —— 实测背面在三个指标上都更稳。

写入 validation-report.thresholds_calibrated。为 False 时中低严重度告警
可能是误报，以人眼判断为准。
"""


# ---------------------------------------------------------------------------
# 目录结构（PLAN §11）
# ---------------------------------------------------------------------------

OUTPUT_SUBDIRS: Final = (
    "source",          # 原始生成图，永不覆盖 —— process 能离线重跑的前提
    "intermediate/keyed",
    "intermediate/split",
    "intermediate/cropped",
    "intermediate/normalized",
    "intermediate/quantized",
    "frames",
    "sheets",
    "previews",
    "exports",
    "jobs",            # job 状态记录
)

MANIFEST_FILE: Final = "asset-manifest.json"
REQUEST_FILE: Final = "request.yaml"
VALIDATION_REPORT_FILE: Final = "validation-report.json"
REPAIR_PLAN_FILE: Final = "repair-plan.json"
GENERATION_LOG_FILE: Final = "generation-log.json"


# ---------------------------------------------------------------------------
# 运行时默认值
# ---------------------------------------------------------------------------

DEFAULT_MAX_CONCURRENCY: Final = 3
DEFAULT_MAX_RETRIES: Final = 4
DEFAULT_MAX_REPAIR_ROUNDS: Final = 2
DEFAULT_TIMEOUT_SECONDS: Final = 300
