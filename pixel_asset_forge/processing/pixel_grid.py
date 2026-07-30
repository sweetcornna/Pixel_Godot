"""吸附到模型自己的像素网格 —— 产出"干净网格上的真像素画"。

## 问题

`gpt-image-2` 画的**不是**网格对齐的像素画，而是**"块边缘发虚的像素画"**：
它确实按块作画（实测块边长约 8.5px），但每个块的边缘带渐变过渡，
整张 1254×1254 的图有 6 万种颜色。

初版流水线直接用最近邻把它采到 32×32。每个采样点落在块的软边缘上，
采出来的是相邻两块的混合色 —— 结果是一团读不出轮廓的泥。
角色的脸、剑、斗篷的形状全部糊掉。

## 做法

先探测块周期，再**按块取中位色**还原出块级图像：

```text
1254×1254 · 60,131 色   →   148×148 · 2,265 色
```

148×148 才是模型真正画出来的逻辑分辨率，角色在其中约 55 像素高。
这一步之后再缩放到目标尺寸，缩放比例小得多，细节保得住。

两个实现细节，反过来做就没用了：

- **取中位而非平均。** 平均会把相邻块的颜色混出原图里不存在的新色，
  正是要消除的那类脏东西；中位保住原有色调。
- **只取块中心 60% 的区域。** 块的外圈就是软边缘，采到它等于没做。

## 顺序：必须在键控**之后**

吸附放在键控之前会把键控色带进角色内部：跨在角色边缘的块，其中位色可能是背景的
洋红，而这个块一旦落在轮廓内侧，"只删与画布外缘连通的背景"这条规则就删不掉它 ——
成品上会出现刺眼的洋红斑点（真实发生过，出现在骑士的腰带上）。

正确顺序是 **键控 → 吸附**，且按块取色时**只统计不透明像素**，
alpha 按块内多数决。这样背景色在结构上就没有机会污染前景。

## 目标尺寸的连带结论

模型的原生逻辑分辨率决定了目标尺寸的上限。角色原生约 55px 高时：

- 48×48 / 64×64 —— 缩放比接近 1:1，细节完整（CraftPix 资产包典型 48×48，
  SpriteCook 用 66×66）
- 32×32 —— 还要再压掉四成，脸与武器的可读性明显下降

所以 32×32 不是"更省资源的选择"，而是**丢掉一半模型产出**的选择。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image
from scipy.signal import find_peaks

from ..logging_utils import get_logger

logger = get_logger("processing.pixel_grid")

MIN_BLOCK = 3
MAX_BLOCK = 32

#: 块中心的采样比例。外圈是软边缘，必须避开。
CORE_RATIO = 0.6

#: 判定"探测成功"的最低色数压缩比。真的对上了网格，色数会掉一个量级以上。
MIN_COLOR_REDUCTION = 4.0

#: 还原误差上限（0~255）。超过就判定探测到的块是臆想的，原样返回。
#:
#: 色数压缩比拦不住这种情况：**本来就是 1:1 的干净像素画**。
#: 检测器的搜索范围从 MIN_BLOCK=3 起步，结构上就答不出"块是 1"，
#: 于是必然报一个 ≥3 的假块；而把 8 个真实像素糊成 1 个，色数当然也掉得很多，
#: 压缩比这一关照样放行。
#:
#: 实测：用户上传的 256×256 1:1 像素画被判成 8.5px 块，直接压成 30×30 ——
#: 资产当场被毁。加上还原误差这一关就拦住了：
#:
#:     用户 1:1 素材，按检测值还原    46 ~ 63
#:     gpt-image-2 真实块状产出       6.7
#:
#: 两类之间差了近十倍，阈值取 20 有充分余量。
MAX_RECONSTRUCTION_ERROR = 20.0

#: 键控色残留占前景的比例，超过才告警。
#:
#: 实测健康资产（cal_archer 的 seed，1024×1536）删掉 34317 个像素 ——
#: 听着吓人，其实大头是被角色围住的背景区域（两腿之间、弓的弯里），
#: 只占前景的百分之几。按绝对数告警会在每个正常资产上都刷屏。
KEY_RESIDUE_WARN_RATIO = 0.05


@dataclass(frozen=True, slots=True)
class GridSnap:
    """吸附结果。"""

    image: np.ndarray
    block_size: float
    logical_size: tuple[int, int]
    colors_before: int
    colors_after: int
    applied: bool

    @property
    def color_reduction(self) -> float:
        return self.colors_before / max(1, self.colors_after)

    def summary(self) -> str:
        if not self.applied:
            if self.block_size <= 1.0:
                return "未吸附（本来就是 1:1 像素画，没有需要还原的块）"
            return f"未吸附（探测块大小 {self.block_size:.1f}px，不可信）"
        return (
            f"块 {self.block_size:.1f}px → {self.logical_size[0]}×{self.logical_size[1]} "
            f"逻辑像素，色数 {self.colors_before:,} → {self.colors_after:,}"
        )


def detect_block_size(rgb: np.ndarray) -> float:
    """用边缘能量的自相关找块周期。

    像素块的边界在梯度图上是规律的峰。把梯度沿一个方向投影成一维信号后自相关，
    第一个显著峰的位置就是块周期。
    """
    gray = rgb.astype(np.float64).mean(axis=2)
    horizontal = np.abs(np.diff(gray, axis=1)).sum(axis=0)
    vertical = np.abs(np.diff(gray, axis=0)).sum(axis=1)

    found: list[int] = []
    for signal in (horizontal, vertical):
        if signal.size <= MAX_BLOCK * 2:
            continue
        centred = signal - signal.mean()
        auto = np.correlate(centred, centred, mode="full")[centred.size - 1 :]
        if auto[0] <= 0:
            continue
        auto = auto / auto[0]
        window = auto[MIN_BLOCK : MAX_BLOCK + 1]
        peaks, _ = find_peaks(window)
        if peaks.size:
            found.append(MIN_BLOCK + int(peaks[int(np.argmax(window[peaks]))]))

    return float(np.mean(found)) if found else 1.0


def _fit_to_blocks(image: np.ndarray, cols: int, rows: int) -> tuple[np.ndarray, int, int]:
    """把图像调到正好 ``cols × rows`` 个等大块，供 reshape 向量化。

    用最近邻微调几个像素而不是逐块处理不等宽的格子：块尺寸本来就有 ±1px 的
    取整误差，为它写逐块循环会让 6 万个块各跑一次 numpy 调用 —— 实测把
    整个测试套件拖到超时。
    """
    from PIL import Image as PILImage

    height, width = image.shape[:2]
    bw, bh = max(1, round(width / cols)), max(1, round(height / rows))
    target = (cols * bw, rows * bh)

    if (width, height) != target:
        mode = "RGBA" if image.shape[2] == 4 else "RGB"
        image = np.array(
            PILImage.fromarray(image, mode).resize(target, PILImage.Resampling.NEAREST)
        )
    return image, bw, bh


def _median_blocks(rgb: np.ndarray, cols: int, rows: int) -> np.ndarray:
    """按块取中位色。全向量化。"""
    fitted, bw, bh = _fit_to_blocks(rgb, cols, rows)

    # 只取块中心：外圈是软边缘，采到它等于没做
    inset_y, inset_x = int(bh * (1 - CORE_RATIO) / 2), int(bw * (1 - CORE_RATIO) / 2)
    blocks = fitted.reshape(rows, bh, cols, bw, 3)
    core = blocks[:, inset_y : bh - inset_y or bh, :, inset_x : bw - inset_x or bw, :]
    return np.asarray(np.median(core, axis=(1, 3))).astype(np.uint8)


def _rgba_blocks(rgba: np.ndarray, cols: int, rows: int) -> np.ndarray:
    """alpha-aware 的块级还原：alpha 按多数决，RGB 只统计不透明像素。

    **吸附与验证必须共用这一份。** 用 RGB 版的 ``_median_blocks`` 去验证，
    跨角色边缘的块会把透明区的键控色也算进中位色，误差被边缘完全主导 ——
    本该吸附的产出会被判成"1:1 像素画"，整条还原被静默关掉。
    """
    import warnings as _warnings

    fitted, bw, bh = _fit_to_blocks(rgba, cols, rows)
    blocks = fitted.reshape(rows, bh, cols, bw, 4)

    opaque = blocks[:, :, :, :, 3] > 0
    keep = opaque.mean(axis=(1, 3)) > 0.5

    values = blocks[:, :, :, :, :3].astype(np.float32)
    values[~opaque] = np.nan
    with _warnings.catch_warnings():
        _warnings.filterwarnings("ignore", message="All-NaN slice encountered")
        median = np.nanmedian(values, axis=(1, 3))
    median = np.nan_to_num(median, nan=0.0)

    out = np.zeros((rows, cols, 4), dtype=np.uint8)
    out[keep, :3] = median[keep].astype(np.uint8)
    out[keep, 3] = 255
    return out


def reconstruction_error(rgba: np.ndarray, block: float) -> float:
    """按 ``block`` 做块级还原后的平均通道误差（仅统计不透明像素）。

    块是真的 → 块内本来就同色，还原几乎无损。
    块是臆想的 → 一个块糊掉好几种真实颜色，误差立刻上到几十。

    这是**探测结果的验证**，不是探测本身。探测给出候选，这里判它可不可信。

    **还原必须用与实际吸附同一种取色方式**（块中心中位色），不能用最近邻。
    最近邻取的是块的**角点**，而角点正是软边缘最重的地方 —— 那样量出来的是
    "角点采样有多差"，不是"块是不是真的"。实测同一张模型产出：
    最近邻还原误差 27.9（越过阈值 20，块被误判成臆想的），
    中心中位还原 5 左右 —— 整条像素网格还原因此被静默关掉过。
    """
    height, width = rgba.shape[:2]
    rows = max(1, round(height / block))
    cols = max(1, round(width / block))
    if rows < 2 or cols < 2:
        return 0.0

    if rgba.shape[2] == 4:
        reduced = _rgba_blocks(rgba, cols, rows)[:, :, :3]
        opaque = rgba[:, :, 3] > 0
    else:
        reduced = _median_blocks(rgba[:, :, :3], cols, rows)
        opaque = np.ones((height, width), bool)

    back = np.array(
        Image.fromarray(reduced).resize((width, height), Image.Resampling.NEAREST)
    )
    if not opaque.any():
        return 0.0
    delta = np.abs(back.astype(int) - rgba[:, :, :3].astype(int))
    return float(delta.max(axis=-1)[opaque].mean())


def count_colors(rgb: np.ndarray) -> int:
    return len(np.unique(rgb.reshape(-1, 3), axis=0))


def snap_to_pixel_grid(rgb: np.ndarray, *, block_size: float | None = None) -> GridSnap:
    """把"块边缘发虚的像素画"还原成块级干净像素画。

    探测不到可信的网格时**原样返回并标记 ``applied=False``** ——
    宁可不做，也不要把一张本来就干净的图强行降采样掉。

    ``block_size`` 显式给出时跳过探测。``process`` 离线重跑要靠它复现，
    否则重新探测可能得到不同周期，结果就不可复现了。
    """
    if rgb.ndim != 3 or rgb.shape[2] < 3:
        raise ValueError(f"snap_to_pixel_grid 需要 HxWx3，收到 {rgb.shape}")

    rgb = rgb[:, :, :3]
    height, width = rgb.shape[:2]
    before = count_colors(rgb)

    block = detect_block_size(rgb) if block_size is None else block_size
    if block < MIN_BLOCK:
        logger.info("未探测到像素块网格（块 %.1fpx），保持原图", block)
        return GridSnap(rgb, block, (width, height), before, before, applied=False)

    cols = max(1, round(width / block))
    rows = max(1, round(height / block))
    reduced = _median_blocks(rgb, cols, rows)
    after = count_colors(reduced)

    # 显式给了块大小就是复现路径，不再用压缩比二次判定 —— 那会让复现结果
    # 取决于当次的色数统计，破坏确定性。
    if block_size is None and before / max(1, after) < MIN_COLOR_REDUCTION:
        logger.info(
            "块网格探测不可信（色数 %d → %d，压缩比不足 %.1f×），保持原图",
            before, after, MIN_COLOR_REDUCTION,
        )
        return GridSnap(rgb, block, (width, height), before, before, applied=False)

    if block_size is None:
        error = reconstruction_error(rgb, block)
        if error > MAX_RECONSTRUCTION_ERROR:
            logger.info(
                "按 %.1fpx 块还原的误差 %.1f 超过 %.1f，判定块是臆想的，保持原图",
                block, error, MAX_RECONSTRUCTION_ERROR,
            )
            return GridSnap(rgb, 1.0, (width, height), before, before, applied=False)

    logger.info(
        "吸附到像素网格：块 %.1fpx，%d×%d → %d×%d，色数 %d → %d",
        block, width, height, cols, rows, before, after,
    )
    return GridSnap(reduced, block, (cols, rows), before, after, applied=True)


def snap_rgba_to_grid(rgba: np.ndarray, *, block_size: float | None = None) -> GridSnap:
    """键控之后的吸附：alpha 按多数决，RGB **只统计不透明像素**。

    只统计不透明像素是这个函数存在的理由 —— 把背景一起算进中位色，
    跨边缘的块就会取到键控色，成品上出现洋红斑点。
    """
    if rgba.ndim != 3 or rgba.shape[2] != 4:
        raise ValueError(f"snap_rgba_to_grid 需要 RGBA，收到 {rgba.shape}")

    height, width = rgba.shape[:2]
    opaque_rgb = rgba[rgba[:, :, 3] > 0][:, :3]
    before = len(np.unique(opaque_rgb, axis=0)) if opaque_rgb.size else 0

    block = detect_block_size(rgba[:, :, :3]) if block_size is None else block_size
    if block < MIN_BLOCK:
        return GridSnap(rgba, block, (width, height), before, before, applied=False)

    cols = max(1, round(width / block))
    rows = max(1, round(height / block))

    out = _rgba_blocks(rgba, cols, rows)

    after_rgb = out[out[:, :, 3] > 0][:, :3]
    after = len(np.unique(after_rgb, axis=0)) if after_rgb.size else 0

    if block_size is None and before / max(1, after) < MIN_COLOR_REDUCTION:
        logger.info("块网格探测不可信（前景色数 %d → %d），保持原图", before, after)
        return GridSnap(rgba, block, (width, height), before, before, applied=False)

    if block_size is None:
        error = reconstruction_error(rgba, block)
        if error > MAX_RECONSTRUCTION_ERROR:
            logger.info(
                "按 %.1fpx 块还原的误差 %.1f 超过 %.1f，判定块是臆想的，保持原图",
                block, error, MAX_RECONSTRUCTION_ERROR,
            )
            return GridSnap(rgba, 1.0, (width, height), before, before, applied=False)

    logger.info(
        "吸附到像素网格：块 %.1fpx，%d×%d → %d×%d，前景色数 %d → %d",
        block, width, height, cols, rows, before, after,
    )
    return GridSnap(out, block, (cols, rows), before, after, applied=True)


def strip_key_residue(
    rgba: np.ndarray, key: tuple[int, int, int], *, tolerance: float = 90.0
) -> tuple[np.ndarray, float]:
    """删掉仍然过近键控色的不透明像素。返回 ``(图, 删掉的比例)``。

    结构上已由"键控后再吸附"避免了大部分，这里是兜底：
    亮洋红出现在褐绿配色的角色身上极其刺眼，宁可打个洞也不能留。

    删掉的东西其实有两类，而且**大头是第二类**：

    1. 真正落在角色身上的洋红斑点 —— 要治的就是它
    2. 被角色围住的背景区域（两腿之间、弓的弯里）—— 色键的漫水填充只清
       与画布外缘连通的背景，这些封闭区域留了下来，本来就该在这里删掉

    所以返回**比例**而不是绝对个数。绝对数随分辨率线性增长，
    1024×1536 上删 34317 个听着吓人，其实只占前景的百分之几，是正常情形。
    一个在健康资产上报出五位数的告警，只会训练用户忽略告警。
    """
    opaque = rgba[:, :, 3] > 0
    total = int(opaque.sum())
    if not total:
        return rgba.copy(), 0.0

    diff = rgba[:, :, :3].astype(np.float64) - np.asarray(key, dtype=np.float64)
    close = np.sqrt((diff**2).sum(axis=-1)) <= tolerance
    offenders = opaque & close
    count = int(offenders.sum())
    if not count:
        return rgba.copy(), 0.0

    out = rgba.copy()
    out[offenders] = 0
    return out, count / total


def native_subject_height(rgba_alpha: np.ndarray) -> int:
    """前景在逻辑像素下的高度。用于判断目标尺寸是否把模型产出丢掉太多。"""
    ys = np.nonzero(rgba_alpha)[0]
    return int(ys.max() - ys.min() + 1) if ys.size else 0


def resolution_warning(native_height: int, target_height: int) -> str | None:
    """目标尺寸明显低于模型原生分辨率时的告警。

    这不是"图会糊"的模糊说法 —— 是在明确地说：**模型画出来的细节里，
    有多少比例被目标尺寸直接扔掉了**。
    """
    if native_height <= 0 or target_height <= 0:
        return None
    if target_height >= native_height * 0.85:
        return None
    lost = 1 - target_height / native_height
    return (
        f"模型原生画出的角色约 {native_height} 逻辑像素高，目标画布只有 "
        f"{target_height}px —— 约 {lost:.0%} 的细节会被丢掉，脸与武器的可读性会明显下降。"
        f"建议 target_size 提到 {min(96, _round_up_size(native_height))} 或更高。"
    )


def _round_up_size(value: int) -> int:
    from ..constants import LOGICAL_SIZES

    for size in LOGICAL_SIZES:
        if size >= value:
            return size
    return LOGICAL_SIZES[-1]
