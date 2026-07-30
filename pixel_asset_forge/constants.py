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
    # 2026-07-30 用 6 角色 × 5 动作 = 30 个真实样本校准（docs/threshold-calibration.md）。
    # 调整策略是**不对称**的，理由见文件末尾 THRESHOLDS_CALIBRATED 的说明。
    "idle":   ActionThresholds(0.14, 0.12, 1),   # 高度 0.06→0.14：史莱姆的挤压回弹
    "walk":   ActionThresholds(0.12, 0.20, 1),   # 实测远低于此，不动
    "attack": ActionThresholds(0.30, 0.45, 1),   # 锚点 3→1
    "hurt":   ActionThresholds(0.30, 0.35, 1),   # 高度 0.25→0.30、锚点 2→1
    # 倒地是形变的极端情况，几何检查无意义 —— 只做人工审核（PLAN §9.1）。
    # 实测 6 个样本的高度变化 0.818~1.419，比任何角色动作高一个量级，
    # 定阈值只能定到 1.7 上下，那种数字拦不住任何真实缺陷。维持豁免。
    "death":  ActionThresholds(None, None, None),
    # 以下四项**没有校准样本**，仍是初始值。
    "cast":   ActionThresholds(0.30, 0.45, 3),
    # 特效类：飞行/爆炸形变剧烈，用角色阈值会全面误报。
    "travel": ActionThresholds(0.40, 0.60, 4),
    "impact": ActionThresholds(None, None, None),
    "loop":   ActionThresholds(0.20, 0.30, 2),
}

#: 按移动形态改写的阈值。只写**需要改**的动作，其余回落 ``ACTION_THRESHOLDS``。
#:
#: 弹跳/浮沉式的走路踩的是与 ``death`` 一样的问题：**形变本身就是动作**。
#: 实测史莱姆一个弹跳周期高度 24→54px，height_variation 0.82、
#: silhouette_variation 0.59，都比双足走路的阈值（0.12 / 0.20）高好几倍。
#: 这不是缺陷，这就是压缩与拉伸。
#:
#: 定一个能放过它的上限（1.0 上下）也拦不住任何真实缺陷，所以和 ``death``
#: 一样明写豁免 —— 一个只会误报的阻断项，最终一定会被开发者整个关掉（PLAN §9.1）。
#: 锚点漂移仍然管着：弹得再高，落地也该落在同一条线上。
LOCOMOTION_THRESHOLDS: Final[dict[str, dict[str, ActionThresholds]]] = {
    "legless":  {"walk": ActionThresholds(None, None, 1)},
    "floating": {"walk": ActionThresholds(None, None, 1)},
}


#: 各动作相对**站立基准高度**的可信区间。超出的部分判为模型漂移，钳回区间内。
#:
#: 跨动作缩放基准的前提是"尺寸差异是真实的姿势差异"，于是它把模型的随机漂移
#: 也原样保住了。实测 6 个角色的站立类动作（idle/walk/attack/hurt）高度极差：
#:
#:     骑士 15%   法师 17%   小恶魔 27%   弓手 32%   石魔 37%   史莱姆 42%
#:
#: 史莱姆待机 70px、走路 45px —— 走路不会让角色矮三成，那是漂移不是姿势。
#: 真实的姿势差异是有界的：走路与待机几乎同高，挥砍前冲可以略低，
#: 倒地才真的矮一大截。所以按动作给区间，超出就钳。
#:
#: ``None`` 表示不钳（倒地类动作、以及我们不了解的自定义动作）。
ACTION_SIZE_BAND: Final[dict[str, tuple[float, float] | None]] = {
    "idle":   (0.95, 1.05),
    "walk":   (0.92, 1.05),
    "attack": (0.85, 1.12),   # 前冲会压低，举手会拔高
    "hurt":   (0.80, 1.05),   # 后仰蜷缩会压低
    "cast":   (0.88, 1.12),
    "death":  None,           # 倒地天然矮一大截，钳它就是把动作毁了
    "impact": None,
    "travel": None,
    "loop":   None,
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

THRESHOLDS_CALIBRATED: Final = True
"""五个角色动作的阈值已用真实样本校准（2026-07-30）。

样本：6 个刻意选取轮廓差异极大的角色（骑士 / 史莱姆 / 法师 / 弓手 / 石魔 / 小恶魔）
× 5 个动作 = 30 个动作，全部人工审核通过。详见 `docs/threshold-calibration.md`。

## 调整策略是**不对称**的

放宽与收紧的风险不对等：**误报会让开发者关掉验证器**（PLAN §9.1 拒绝统一
阈值时给的就是这个理由），漏报只是少抓一个缺陷。6 个样本足以证明"某个阈值
太紧"，但不足以证明"某个阈值可以收多紧"。所以：

- **有证据就放宽。** 一个**完全正确**的史莱姆待机（挤压回弹 68→73→76→68）
  实测高度变化 0.112，而阈值是 0.06 —— 它会被判失败。人形调出来的阈值
  对非人形误报，这是本次校准最有价值的发现。``hurt`` 的高度变化实测 0.248、
  阈值 0.25，只差 1%，同样放宽。
- **不轻易收紧。** ``walk`` 的轮廓变化实测最大 0.078、阈值 0.20，看着可以
  收到 0.09 —— 但那是拿 6 个样本去赌第 7 个角色。留着。
- **锚点漂移例外，可以收。** 它是绝对像素量，**与角色形状无关**，
  30 个样本全部落在 0.18~0.66。原来 attack 给 3px、hurt 给 2px，
  比实测宽 3~6 倍。统一收到 1px 仍有 1.5 倍余量。

## 仍未校准的部分

``cast`` / ``travel`` / ``impact`` / ``loop`` **没有样本**，维持初始值。
``death`` 维持豁免（实测高度变化 0.818~1.419，比角色动作高一个量级，
能定出的阈值只有 1.7 上下，那种数字拦不住任何真实缺陷）。

``up`` 方向的 ×1.3 修正系数也**没有验证** —— 本次样本全是 ``down``。

写入 validation-report.thresholds_calibrated。
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


def split_animation_key(key: str) -> tuple[str, Direction | None]:
    """``walk_down`` → ``("walk", "down")``；``dodge_roll_down`` → ``("dodge_roll", "down")``。

    **必须从右边切。** 动作键的形状是 ``{action}_{direction}``，而自定义动作名
    本身允许带下划线（``dodge_roll``）—— 从左切第一个下划线会把它劈成
    ``("dodge", "roll_down")``，动作名和方向双双解错。实测踩过：
    验证器报出"取不到 dodge 的节拍"，而请求里根本没有 dodge 这个动作。

    从右切之所以可行，是因为方向是个**有限集合**：最后一段在集合里才算方向，
    否则整串都是动作名（无方向资产如 ``impact``）。

    仍有一处**无解的歧义**：``charge_up`` 是"动作 charge 朝上"还是"动作
    charge_up 无方向"？光看字符串分不开。所以自定义动作名**不许以方向词结尾**，
    这条在 ``AnimationSpec`` 里拦（从构造上消除歧义，而不是在这里猜）。
    """
    head, sep, tail = key.rpartition("_")
    if sep and tail in DIRECTIONS:
        return head, tail
    return key, None
