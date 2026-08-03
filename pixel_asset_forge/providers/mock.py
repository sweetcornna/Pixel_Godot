"""Mock Provider —— 完全离线的确定性生成后端。

Mock **不是测试用的桩，而是一等公民**（ADR-002）：Sprint 1 的退出门槛就是
"不调用真实 API 即可走完整工作流"。整条流水线的骨架必须在花第一分钱之前就验证过。

因此合成图必须真的能驱动下游处理逻辑：

- 真的画出 N 个格子，每格一个**姿势不同**的图形
- 真的用键控色铺满背景
- 每个姿势四周留出 ≥8% 边距（不跨格线）
- 脚底对齐到统一基线（锚点检查有意义）
- 帧与帧之间平滑变化（帧序连续性检查有意义）

否则 Mock 测试就流于形式 —— 一张纯色图能通过的验证器等于没有验证器。

网格布局从 **prompt 文本**里解析，而不是通过额外参数传入。理由：Mock 是模型的替身，
它该消费的输入就是模型消费的输入。这样也顺带验证了 prompt 里的网格约束确实写清楚了。
"""

from __future__ import annotations

import io
import math
import random
import re
from collections.abc import Sequence

from PIL import Image, ImageDraw

from ..constants import CELL_SIZE, DEFAULT_KEY_COLOR, SEED_SIZE
from ..errors import ModerationBlockedError, TransientProviderError
from ..storage.hashes import hash_bytes
from .base import ImageProvider, ReferenceImage

_GRID_RE = re.compile(r"(\d+)\s*[x×]\s*(\d+)\s*(?:cell\s*)?grid", re.IGNORECASE)
_COLS_ROWS_RE = re.compile(
    r"(\d+)\s*columns?\s*[x×]\s*(\d+)\s*rows?", re.IGNORECASE
)
"""prompt 里描述网格的**规范措辞**，两条生成路径都用它：

- ``compiler``：``a 4 columns x 3 rows grid of 12 equally sized cells``
- ``inbetween``：``Layout: exactly 3 columns x 1 rows of 3 equally sized cells``

补间那条曾经两条正则都不命中（它既不写 ``NxM grid`` 也不写 ``N poses``），
于是 Mock 退回按物理尺寸猜格数 —— 1440 // 512 = 2，在一张要求 3 格的图上
只画了 2 个人形。中间那格是纯背景，下游抽帧抽出一张空帧，
再往下就是 :mod:`..pipelines.interpolate` 里那次失控的放大。
"""

_POSES_RE = re.compile(r"exactly\s+(\d+)\s+(?:distinct\s+)?poses?", re.IGNORECASE)

#: prompt 在描述格子布局时必然出现的短语。见 :func:`_parse_layout` 里"拒绝猜"的判据。
_LAYOUT_MARKER = "equally sized cells"
_HEX_RE = re.compile(r"#([0-9A-Fa-f]{6})")
_BACKGROUND_HEX_RE = re.compile(
    r"Background:\s*[^#]*#([0-9A-Fa-f]{6})",
    re.IGNORECASE,
)

#: 触发模拟 moderation 拦截的词。用于测试永久错误路径不会被重试。
_BLOCKED_WORDS = ("gore", "mutilat", "dismember")

_MARGIN_RATIO = 0.12
"""姿势四周留白比例。prompt 要求 ≥8%，Mock 留 12% 以确保不会卡在边界上。"""


def _parse_key_color(prompt: str) -> tuple[int, int, int]:
    match = _BACKGROUND_HEX_RE.search(prompt) or _HEX_RE.search(prompt)
    raw = match.group(1) if match else DEFAULT_KEY_COLOR.lstrip("#")
    return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))


def _parse_layout(prompt: str, size: tuple[int, int]) -> tuple[int, int, int]:
    """返回 ``(cols, rows, frames)``。

    优先信 prompt 里写明的网格；prompt 没写就按 512 单元格从物理尺寸推断。
    种子图尺寸是特例 —— 它是单幅立绘，不是 2×2 网格。
    """
    width, height = size
    if size == SEED_SIZE:
        return (1, 1, 1)

    poses = _POSES_RE.search(prompt)
    frames = int(poses.group(1)) if poses else 0

    match = _COLS_ROWS_RE.search(prompt) or _GRID_RE.search(prompt)
    if match:
        cols, rows = int(match.group(1)), int(match.group(2))
    elif frames and "one horizontal row" in prompt.lower():
        cols, rows = frames, 1
    elif _LAYOUT_MARKER in prompt.lower():
        # prompt 明写了格子布局，却没解析出格数 —— 此时**不许猜**。
        # 按物理尺寸猜出的格数一旦与 prompt 不一致，Mock 画的格数就与下游按
        # prompt 切分的格数对不上，切出来的"帧"落在格间空白上。这种分歧不会
        # 报错，只会在下游变成一张空帧，是最难查的一类问题（实测它一路走到
        # 重采样才以 OOM 收场）。ADR-002：Mock 是一等公民，宁可在这里失败。
        raise ValueError(
            "Mock 解析不出 prompt 里的网格布局，拒绝按物理尺寸猜 —— "
            "prompt 描述了 equally sized cells，却没有可识别的 "
            "'N columns x M rows' / 'NxM grid' / 'exactly N poses'。"
            "改 prompt 措辞时请同步 mock 的解析规则。"
        )
    else:
        cols = max(1, width // CELL_SIZE)
        rows = max(1, height // CELL_SIZE)

    frames = frames or cols * rows
    return (cols, rows, min(frames, cols * rows))


def _palette(rng: random.Random) -> dict[str, tuple[int, int, int]]:
    hue = rng.random()

    def shade(offset: float, value: float) -> tuple[int, int, int]:
        h = (hue + offset) % 1.0
        i = int(h * 6)
        f = h * 6 - i
        p, q, t = 0.0, 1 - f, f
        table = [(1, t, p), (q, 1, p), (p, 1, t), (p, q, 1), (t, p, 1), (1, p, q)]
        r, g, b = table[i % 6]
        return (int(r * value * 255), int(g * value * 255), int(b * value * 255))

    return {
        "body": shade(0.0, 0.75),
        "body_dark": shade(0.0, 0.45),
        "head": shade(0.08, 0.9),
        "outline": (24, 20, 32),
    }


def _draw_pose(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    phase: float,
    colors: dict[str, tuple[int, int, int]],
) -> None:
    """在 ``box`` 内画一个人形，姿势随 ``phase``（0~1）连续变化。

    连续变化是刻意的：帧序连续性检查靠"相邻帧差异应大致均匀"来发现乱序，
    如果 Mock 每帧随机画，这个检查在 Mock 数据上就永远是误报。
    """
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    margin_x, margin_y = int(width * _MARGIN_RATIO), int(height * _MARGIN_RATIO)

    inner_left, inner_right = left + margin_x, right - margin_x
    inner_top, inner_bottom = top + margin_y, bottom - margin_y

    # 人形的所有尺寸都以**短边**为基准。以宽为基准的话，格子一旦扁一点
    # （补间按关键帧格子形状定网格后就会出现），躯干顶就跑到躯干底下面，
    # Pillow 直接抛 "y1 must be greater than or equal to y0"。
    inner_w = min(inner_right - inner_left, inner_bottom - inner_top)

    # 脚底统一贴在 inner_bottom —— 锚点漂移检查才有基线可比。
    baseline = inner_bottom
    center_x = (inner_left + inner_right) // 2

    swing = math.sin(phase * 2 * math.pi)
    bob = int(abs(math.cos(phase * math.pi)) * inner_w * 0.04)

    head_r = int(inner_w * 0.18)
    head_cy = inner_top + head_r + bob
    body_top = head_cy + head_r
    body_bottom = baseline - int(inner_w * 0.28)
    body_half = int(inner_w * 0.18)

    # 腿
    leg_offset = int(swing * inner_w * 0.16)
    leg_w = max(2, int(inner_w * 0.09))
    for side in (-1, 1):
        foot_x = center_x + side * (int(inner_w * 0.10) + side * leg_offset)
        draw.line(
            [(center_x + side * int(inner_w * 0.08), body_bottom), (foot_x, baseline)],
            fill=colors["body_dark"],
            width=leg_w,
        )

    # 躯干
    draw.rectangle(
        [center_x - body_half, body_top, center_x + body_half, body_bottom],
        fill=colors["body"],
        outline=colors["outline"],
        width=max(1, int(inner_w * 0.02)),
    )

    # 手臂（与腿反相，像真实步态）
    arm_offset = int(-swing * inner_w * 0.14)
    arm_w = max(2, int(inner_w * 0.07))
    for side in (-1, 1):
        draw.line(
            [
                (center_x + side * body_half, body_top + int(inner_w * 0.06)),
                (center_x + side * (body_half + int(inner_w * 0.10)) + side * arm_offset,
                 body_top + int(inner_w * 0.30)),
            ],
            fill=colors["body_dark"],
            width=arm_w,
        )

    # 头
    draw.ellipse(
        [center_x - head_r, head_cy - head_r, center_x + head_r, head_cy + head_r],
        fill=colors["head"],
        outline=colors["outline"],
        width=max(1, int(inner_w * 0.02)),
    )


_TILE_RE = re.compile(r"seamless repeating ground texture", re.IGNORECASE)


def _render_tile(prompt: str, size: tuple[int, int], salt: str) -> bytes:
    """合成一张**真的可平铺**的地面纹理。

    Mock 是一等公民而不是桩（ADR-002）：合成图必须能驱动下游逻辑。tile 这条链的
    闸门是无缝检查，所以这里不能沿用网格图那套"键控色底 + 居中主体 + 四周留白"
    —— 那对 tile 恰恰是"带边框"，实测 border_deviation 130（阈值 2），
    整套必然判失败，离线就走不完这条链了。

    均匀噪声天然可平铺：接缝处的相邻差异与内部同分布，四周也不存在系统性的
    明暗落差。这与"好模型该交出什么"是一致的，不是为了绕过检查。
    """
    width, height = size
    rng = random.Random(hash_bytes((prompt + salt).encode("utf-8"))[:16])
    base = _palette(rng)["body"]
    canvas = Image.new("RGB", (width, height))
    pixels = canvas.load()
    assert pixels is not None
    # 按块铺色而不是逐像素：逐像素在 1024² 上又慢又会被下采样抹平，
    # 块状颗粒才经得起 32× 的缩小。
    block = max(1, width // 64)
    for by in range(0, height, block):
        for bx in range(0, width, block):
            shade = rng.randint(-28, 28)
            color = tuple(max(0, min(255, channel + shade)) for channel in base)
            for y in range(by, min(by + block, height)):
                for x in range(bx, min(bx + block, width)):
                    pixels[x, y] = color

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue()


def render_mock_image(prompt: str, size: tuple[int, int], *, salt: str = "") -> bytes:
    """确定性地合成一张网格图。同样的 ``(prompt, size, salt)`` 必然产出同样的字节。"""
    if _TILE_RE.search(prompt):
        return _render_tile(prompt, size, salt)

    width, height = size
    cols, rows, frames = _parse_layout(prompt, size)
    key_color = _parse_key_color(prompt)

    rng = random.Random(hash_bytes((prompt + salt).encode("utf-8"))[:16])
    colors = _palette(rng)

    canvas = Image.new("RGB", (width, height), key_color)
    draw = ImageDraw.Draw(canvas)

    cell_w, cell_h = width // cols, height // rows
    for index in range(frames):
        col, row = index % cols, index // cols
        box = (col * cell_w, row * cell_h, (col + 1) * cell_w, (row + 1) * cell_h)
        _draw_pose(draw, box, index / max(1, frames), colors)

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue()


class MockImageProvider(ImageProvider):
    """离线 Provider。

    ``fail_times`` / ``fail_with`` 用于驱动错误路径测试：
    验证瞬态错误会退避重试、永久错误不会被重试。
    """

    name = "mock"

    def __init__(
        self,
        model: str = "mock-image",
        *,
        fail_times: int = 0,
        fail_with: type[Exception] = TransientProviderError,
        **kwargs: object,
    ) -> None:
        super().__init__(model, **kwargs)  # type: ignore[arg-type]
        self.fail_times = fail_times
        self.fail_with = fail_with
        self.calls: list[dict[str, object]] = []

    def _maybe_fail(self, prompt: str) -> None:
        lowered = prompt.lower()
        if any(word in lowered for word in _BLOCKED_WORDS):
            raise ModerationBlockedError(
                "内容被审核拦截。请改写描述，避免暴力/血腥表述后重新提交。"
            )
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.fail_with(f"mock 注入失败，剩余 {self.fail_times} 次")

    def _generate(
        self, prompt: str, size: tuple[int, int], model: str
    ) -> tuple[bytes, str | None]:
        self._maybe_fail(prompt)
        self.calls.append({"operation": "generate", "size": size, "model": model})
        request_id = f"mock_gen_{hash_bytes(prompt.encode('utf-8'))[:12]}"
        return render_mock_image(prompt, size), request_id

    def _edit(
        self,
        prompt: str,
        base_image: bytes,
        references: Sequence[ReferenceImage],
        size: tuple[int, int],
        model: str,
    ) -> tuple[bytes, str | None]:
        self._maybe_fail(prompt)
        self.calls.append(
            {"operation": "edit", "size": size, "model": model, "references": len(references)}
        )
        # 参考图哈希参与合成 —— 换了 seed 就该得到不同的图，否则缓存语义是假的。
        salt = "".join(r.content_hash[:8] for r in references) + hash_bytes(base_image)[:8]
        request_id = f"mock_edit_{hash_bytes((prompt + salt).encode('utf-8'))[:12]}"
        return render_mock_image(prompt, size, salt=salt), request_id

    def probe(self) -> dict[str, object]:
        return {"provider": self.name, "model": self.model, "reachable": True, "offline": True}
