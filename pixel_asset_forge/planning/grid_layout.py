"""网格布局与 API 尺寸约束（PLAN §2.3 / ADR-003）。

网格划分在**请求发出前**就已确定，切帧时不做任何内容分析。这里是那个"确定"发生的地方。

四条 API 约束必须在本地先跑一遍：让 API 用 400 告诉我们尺寸不合规，
既慢又浪费一次往返，而且错误信息远不如这里精确。

> **Sprint 0 修正（见 [sprint-0-report.md](../../docs/sprint-0-report.md) A-1）**
>
> 实测端点**不保证按请求尺寸返回**：请求 2048×1024，两次分别返回 1536×1024 与 1774×887，
> 且不报任何错。因此切帧**必须按比例**（`col/cols × 实际宽度`），
> 绝不能按 512px 绝对偏移 —— 后者会让每一帧都静默错位。
> 非整除的实际尺寸会在处理层先居中裁到网格整倍数，再继续按比例切帧；
> 这是消除格线取整误差的收尾，不是对比例切帧的替代。
>
> 请求尺寸从此只承担两个作用：表达期望的长短边比、以及本地合规自检。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..constants import (
    ALLOWED_FRAME_COUNTS,
    CELL_SIZE,
    GRID_LAYOUTS,
    MAX_ASPECT_RATIO,
    MAX_SIDE,
    MAX_TOTAL_PIXELS,
    MIN_TOTAL_PIXELS,
    SEED_SIZE,
    SIZE_MULTIPLE,
    STRIP_LAYOUTS,
)
from ..errors import GridLayoutError


@dataclass(frozen=True, slots=True)
class SizeViolation:
    constraint: str
    detail: str


def check_size(width: int, height: int) -> list[SizeViolation]:
    """返回 ``(width, height)` 违反的全部 API 约束。空列表表示合规。"""
    violations: list[SizeViolation] = []

    if width % SIZE_MULTIPLE or height % SIZE_MULTIPLE:
        violations.append(
            SizeViolation("multiple_of_16", f"{width}×{height} 的边长不都是 {SIZE_MULTIPLE} 的倍数")
        )

    long_side, short_side = max(width, height), min(width, height)
    if short_side <= 0:
        violations.append(SizeViolation("positive", f"{width}×{height} 含非正数边长"))
        return violations

    ratio = long_side / short_side
    if ratio > MAX_ASPECT_RATIO:
        violations.append(
            SizeViolation("aspect_ratio", f"长短边比 {ratio:.2f} > {MAX_ASPECT_RATIO}")
        )

    total = width * height
    if total < MIN_TOTAL_PIXELS:
        violations.append(
            SizeViolation("min_pixels", f"总像素 {total:,} < {MIN_TOTAL_PIXELS:,}")
        )
    if total > MAX_TOTAL_PIXELS:
        violations.append(
            SizeViolation("max_pixels", f"总像素 {total:,} > {MAX_TOTAL_PIXELS:,}")
        )

    if long_side > MAX_SIDE:
        violations.append(SizeViolation("max_side", f"最长边 {long_side} > {MAX_SIDE}"))

    return violations


@dataclass(frozen=True, slots=True)
class GridLayout:
    """一次 API 调用要产出的物理网格。"""

    frames: int
    cols: int
    rows: int
    cell: tuple[int, int] = (CELL_SIZE, CELL_SIZE)

    @property
    def width(self) -> int:
        return self.cols * self.cell[0]

    @property
    def height(self) -> int:
        return self.rows * self.cell[1]

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)

    @property
    def capacity(self) -> int:
        return self.cols * self.rows

    def cell_box(
        self, index: int, size: tuple[int, int] | None = None
    ) -> tuple[int, int, int, int]:
        """第 ``index`` 帧在尺寸为 ``size`` 的图上的裁剪框 ``(left, top, right, bottom)``。

        ``size`` 省略时用本布局的名义尺寸；**处理真实产出时必须显式传入实际图像尺寸**
        —— 端点不保证按请求尺寸返回（Sprint 0 / A-1）。

        阅读顺序固定为**从左到右、从上到下**（PLAN §2.3.1）。这个顺序同时写进 prompt
        并单独验证 —— 模型排乱帧序时全部其他检查项都会通过，是典型的静默失败。
        """
        if not 0 <= index < self.capacity:
            raise GridLayoutError(f"帧下标 {index} 超出 {self.cols}×{self.rows} 网格容量")
        width, height = size or self.size
        col, row = index % self.cols, index // self.cols
        return (
            round(col * width / self.cols),
            round(row * height / self.rows),
            round((col + 1) * width / self.cols),
            round((row + 1) * height / self.rows),
        )

    def boxes(self, size: tuple[int, int] | None = None) -> list[tuple[int, int, int, int]]:
        return [self.cell_box(i, size) for i in range(self.frames)]

    def actual_cell(self, size: tuple[int, int]) -> tuple[int, int]:
        """居中裁成网格整倍数后的实际单元格尺寸。写入 manifest。

        记录的必须是**实际**值而非名义 512。源图原尺寸另存于 ``actual_size``；
        ``process`` 从原图重放相同的确定性居中裁剪后，会得到这里记录的精确格子。
        """
        width, height = size
        return (width // self.cols, height // self.rows)

    def describe(self) -> str:
        return (
            f"{self.frames} 帧 → {self.cols}×{self.rows} 网格 · "
            f"期望 {self.width}×{self.height} · 名义单元格 {self.cell[0]}×{self.cell[1]}"
        )


def aspect_mismatch(requested: tuple[int, int], actual: tuple[int, int]) -> float:
    """返回图与请求的长短边比偏差（相对值）。

    偏差不为 0 意味着单元格不再是正方形，下采样到逻辑尺寸时会引入非等比压缩 ——
    角色会被拉扁或拉长。这个量必须写进验证报告，否则用户只会觉得"图怎么怪怪的"。
    """
    req = requested[0] / requested[1]
    act = actual[0] / actual[1]
    return abs(act - req) / req


def strip_for_frames(frames: int) -> GridLayout:
    """帧数 → **单行**条带布局。装不下就报错并把算式讲清楚。

    一张图只放一条完整的帧序列。分成两行时模型倾向于把每行当作独立的一组，
    行与行之间的角色朝向、体型、基线都会各走各的。
    """
    cell = STRIP_LAYOUTS.get(frames)
    if cell is None:
        ratio_cap = MAX_ASPECT_RATIO / frames
        options = " / ".join(str(f) for f in sorted(STRIP_LAYOUTS))
        raise GridLayoutError(
            f"{frames} 帧排成单行时，格子宽高比必须 ≤ {MAX_ASPECT_RATIO}/{frames} "
            f"= {ratio_cap:.3f} 才能满足 API 的长短边比限制 —— 这么窄的格子装不下"
            f"带跨步和佩剑的角色。可用单行档位：{options} 帧；"
            f"要保留 {frames} 帧就用多行网格（grid_for_frames）。"
        )

    grid = GridLayout(frames=frames, cols=frames, rows=1, cell=cell)
    violations = check_size(grid.width, grid.height)
    if violations:  # pragma: no cover - 常量写死，除非有人改坏了
        detail = "；".join(f"{v.constraint}（{v.detail}）" for v in violations)
        raise GridLayoutError(
            f"{frames} 帧单行的物理尺寸 {grid.width}×{grid.height} 不合规：{detail}"
        )
    return grid


def supported_batch_sizes() -> tuple[int, ...]:
    """一次调用能画出的帧数档位。补间要按它决定每个间隔最多补几帧。

    把这个集合放在布局模块里而不是抄一份到帧率模块：档位随
    ``STRIP_LAYOUTS`` / ``GRID_LAYOUTS`` 变，抄一份就会悄悄过期。
    """
    return tuple(sorted(set(STRIP_LAYOUTS) | set(GRID_LAYOUTS)))


def layout_for_frames(frames: int, *, single_row: bool = True) -> GridLayout:
    """默认排单行；帧数排不下单行时自动退回多行网格。

    退回是**静默且确定**的：调用方拿 ``layout.rows`` 就能知道走了哪条路，
    不必先问一遍能不能单行。
    """
    if single_row and frames in STRIP_LAYOUTS:
        return strip_for_frames(frames)
    return grid_for_frames(frames)


def grid_for_frames(frames: int, cell: int = CELL_SIZE) -> GridLayout:
    """帧数 → 多行网格布局。非法帧数直接报错并给出可选档位。"""
    layout = GRID_LAYOUTS.get(frames)
    if layout is None:
        allowed = " / ".join(str(f) for f in ALLOWED_FRAME_COUNTS)
        nearest = min((f for f in ALLOWED_FRAME_COUNTS if f >= frames), default=None)
        hint = f"建议向上取到 {nearest} 帧。" if nearest else "请降低帧数。"
        raise GridLayoutError(
            f"帧数 {frames} 无法映射到合规网格。可选档位：{allowed}。{hint}"
        )

    cols, rows = layout
    grid = GridLayout(frames=frames, cols=cols, rows=rows, cell=(cell, cell))

    violations = check_size(grid.width, grid.height)
    if violations:
        detail = "；".join(f"{v.constraint}（{v.detail}）" for v in violations)
        raise GridLayoutError(
            f"{frames} 帧对应的物理尺寸 {grid.width}×{grid.height} 不满足 API 约束：{detail}"
        )
    return grid


def seed_layout() -> GridLayout:
    """种子图布局：单格 1024×1024。"""
    width, height = SEED_SIZE
    grid = GridLayout(frames=1, cols=1, rows=1, cell=(width, height))
    violations = check_size(width, height)
    if violations:  # pragma: no cover - 常量写死，除非有人改坏了
        detail = "；".join(f"{v.constraint}（{v.detail}）" for v in violations)
        raise GridLayoutError(f"种子图尺寸 {width}×{height} 不合规：{detail}")
    return grid


def layout_matching_cell(frames: int, cell: tuple[int, int]) -> GridLayout:
    """帧数 → 单元格**形状与大小都贴着 ``cell``** 的网格。

    补间专用。中间帧必须与关键帧画在同样形状、同样大小的格子里：

    - **形状不同，模型会重新取景。** 实测弓手关键帧格 543×724（竖），
      补间却按 ``layout_for_frames`` 要了 1024×1024（方），模型于是把角色
      按方格重画 —— 头和兜帽被放大成一团，播放时中间帧像换了个人。
    - **大小不同，缩到画布后的细节密度不同。** 关键帧格 724px 缩到 74px 是
      9.8×，间隔格 1254px 缩到 74px 是 17×，同一个角色一半清晰一半糊。

    API 的最小总像素约束通常让格子没法真的等于关键帧格（543×724 只有 39 万
    像素，低于下限 65 万），所以取**合规范围内最接近的**那档，而不是最大的一档
    —— 最大的一档会把格子推到 3000px 上下，比不匹配更糟。
    """
    base = layout_for_frames(frames)
    target_w, target_h = cell
    if target_w <= 0 or target_h <= 0:
        return base
    aspect = target_w / target_h

    best: GridLayout | None = None
    best_gap = float("inf")
    for height in range(SIZE_MULTIPLE, MAX_SIDE + 1, SIZE_MULTIPLE):
        width = max(SIZE_MULTIPLE, round(height * aspect / SIZE_MULTIPLE) * SIZE_MULTIPLE)
        candidate = GridLayout(frames=frames, cols=base.cols, rows=base.rows, cell=(width, height))
        if check_size(candidate.width, candidate.height):
            continue
        gap = abs(height - target_h)
        if gap < best_gap:
            best, best_gap = candidate, gap
    return best if best is not None else base
