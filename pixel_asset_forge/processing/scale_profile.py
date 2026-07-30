"""跨动作缩放一致性（scale profile）。

## 问题

处理链是**逐动作**跑的：每个动作各自取内容包围盒、各自缩放到目标画布。
结果是每个动作都填满画布 —— 于是**同一个角色在不同动作之间会变大变小**。

实测：把源图里的角色整体缩到 60% 模拟蹲伏，输出后的内容高度与全高走路
**完全一样**（27–32px / 32）。蹲伏这个真实的姿势变化被 normalize 掉了。

`attack`（前冲）、`hurt`（后仰）、`death`（倒地）都会踩到，
而它们正是 Sprint 6.5 要补的三个动作。

参考 `agent-sprite-forge` 的做法：由一个**参考动作**确定缩放，
后续动作复用同一缩放，"crouching, recoil, hurt 的轮廓变化才仍然是真实的姿势变化，
而不是被归一化回参考高度"。

## 为什么不能直接存"输出像素 / 源像素"

因为**源单元格尺寸会变**：端点对同一请求可能返回 444×444，也可能返回 384×512
（Sprint 0 / A-1）。同一个角色在不同尺寸的格子里，源像素高度天然不同，
直接存比例会把端点的尺寸抖动当成角色体型变化。

所以 profile 存的是**无量纲**的量：

- ``subject_ratio`` = 内容高度 / 单元格高度 —— 角色在格子里占多满
- ``canvas_fraction`` = 输出内容高度 / 目标画布高度 —— 角色在画布里占多满

后续动作按两者的比值推算自己该占画布多少：

```text
canvas_fraction = ref_canvas_fraction × (subject_ratio / ref_subject_ratio)
```
"""

from __future__ import annotations

from dataclasses import dataclass

from ..logging_utils import get_logger

logger = get_logger("processing.scale_profile")


@dataclass(frozen=True, slots=True)
class ScaleProfile:
    """由参考动作确定的跨动作缩放基准。"""

    reference: str
    """确立该基准的动作键，如 ``walk_down``。"""

    subject_ratio: float
    """参考动作的 内容高度 / 单元格高度。"""

    canvas_fraction: float
    """参考动作的 输出内容高度 / 目标画布高度。通常是 1.0（填满）。"""

    def to_dict(self) -> dict[str, float | str]:
        return {
            "reference": self.reference,
            "subject_ratio": round(self.subject_ratio, 6),
            "canvas_fraction": round(self.canvas_fraction, 6),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ScaleProfile:
        return cls(
            reference=str(data["reference"]),
            subject_ratio=float(data["subject_ratio"]),  # type: ignore[arg-type]
            canvas_fraction=float(data["canvas_fraction"]),  # type: ignore[arg-type]
        )

    def target_height(
        self, *, subject_ratio: float, canvas_height: int
    ) -> float:
        """按本基准推算某个动作该占多少输出像素高。

        比参考动作"在格子里更矮"的姿势（蹲伏、倒地）会得到更小的输出高度 ——
        这正是要保住的真实差异。
        """
        if self.subject_ratio <= 0:
            return float(canvas_height)
        fraction = self.canvas_fraction * (subject_ratio / self.subject_ratio)
        return fraction * canvas_height


def derive_profile(
    key: str, *, content_height: int, cell_height: int, canvas_height: int,
    output_height: int,
) -> ScaleProfile:
    """从参考动作的处理结果导出基准。"""
    if cell_height <= 0 or canvas_height <= 0:
        raise ValueError("单元格与画布高度必须为正")
    return ScaleProfile(
        reference=key,
        subject_ratio=content_height / cell_height,
        canvas_fraction=output_height / canvas_height,
    )


def uneven_upscale(scale: float) -> bool:
    """放大系数不是整数倍时为真 —— 只用来告警，**不强制修正**。

    像素画放大 1.17 倍，意味着有的像素占 1 个输出像素、有的占 2 个，
    刚由 pixel_grid 还原出来的等宽块当场被打回参差不齐。所以这件事值得说。

    但它**不能**被自动改掉。曾经在这里向下取整到整数倍，结果是：
    只有需要放大的动作被砍，需要缩小的不受影响 —— 跨动作缩放基准当场失效，
    实测同一个角色 hurt 占画布 49%、attack 占 78%，连参考动作自己都够不到
    自己的目标（要 1.745× 被砍成 1.0）。

    跨动作一致性是硬要求，像素等宽是加分项。冲突时前者赢，后者转告警：
    想两者兼得，就把 target_size 设成原生高度的整数倍。
    """
    return scale > 1.0 and abs(scale - round(scale)) > 1e-6


def scale_for(
    profile: ScaleProfile | None,
    *,
    content_size: tuple[int, int],
    cell_height: int,
    canvas: tuple[int, int],
) -> float:
    """返回该动作应使用的缩放系数。

    没有 profile（即本动作就是参考动作）时按等比填满画布。
    有 profile 时按基准推算，**但结果会被钳制在画布内**。

    钳制不是"悄悄缩回去"：被裁掉一半身子的 sprite 是**坏的**，
    而小一点的 sprite 只是小一点。所以宁可在极端处牺牲相对大小，
    也不能产出残缺帧。钳制生效时会告警，让用户知道该调大 ``target_size``。

    放大方向不做取整，理由见 :func:`uneven_upscale`。
    """
    content_w, content_h = content_size
    canvas_w, canvas_h = canvas
    if content_w <= 0 or content_h <= 0:
        return 1.0

    fit = min(canvas_w / content_w, canvas_h / content_h)
    if profile is None:
        return fit

    wanted = profile.target_height(
        subject_ratio=content_h / max(1, cell_height), canvas_height=canvas_h
    )
    # 钳制在画布内 —— 残缺帧比略小的帧严重得多
    return min(wanted / content_h, fit)


def clamp_warning(
    key: str, wanted_scale: float, applied_scale: float
) -> str | None:
    """跨动作缩放被钳制时的告警。

    钳制说明该动作按基准算应当**比画布还大** —— 它确实比参考动作大
    （例如挥剑前冲），但已被缩回画布内以免出残缺帧。
    """
    if applied_scale >= wanted_scale - 1e-6:
        return None
    shrink = 1 - applied_scale / wanted_scale
    return (
        f"{key} 按跨动作缩放基准应放大到画布之外（超出约 {shrink:.0%}），已缩回画布内。"
        "该动作确实比参考动作大 —— 想保住这个差异就调大 target_size，"
        "或把参考动作换成幅度最大的那个。"
    )
