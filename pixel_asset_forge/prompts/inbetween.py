"""补间 prompt（PLAN §8 Sprint 6.8.3）。

与动作 prompt 的根本区别：**姿势不是描述出来的，是两张参考图夹出来的。**

动作 prompt 要逐格写死姿势，因为"一个完整循环"这种整体描述模型不会自己拆解
（Sprint 0 / A-2）。补间不存在这个问题 —— 起止姿势都在参考图里摆着，
要说清楚的只有"第几格离哪头更近"。

反过来，补间有动作 prompt 没有的风险：模型会**顺手改造**中间帧，
把它当成一次重新创作而不是过渡。所以这里的负面清单比别处更硬。
"""

from __future__ import annotations

from ..models.request import AssetRequest
from ..planning.grid_layout import GridLayout
from .compiler import (
    PROMPT_MARGIN_PERCENT,
    CompiledPrompt,
    _background_block,
    _style_block,
)
from .negative_rules import negative_block


def _progress_line(index: int, total: int, cols: int) -> str:
    """第 index 格（0 起）的位置描述。

    百分比写死而不是说"逐渐过渡"：实测模型对"逐渐"的理解是把所有中间帧
    画成同一个中间姿势，等分点必须逐格点名。
    """
    percent = round((index + 1) * 100 / (total + 1))
    col, row = index % cols + 1, index // cols + 1
    return (
        f"Cell {index + 1} (row {row}, column {col}): exactly {percent}% of the way "
        f"from the FIRST reference image to the SECOND reference image"
    )


def compile_inbetween_prompt(
    request: AssetRequest,
    *,
    action: str,
    direction: str | None,
    frames: int,
    layout: GridLayout,
    key_color: str,
) -> CompiledPrompt:
    """编译一个间隔的补间 prompt。

    ``frames`` 是这个间隔要补的中间帧数，**不含**两头的关键帧。
    """
    progression = "\n".join(
        _progress_line(index, frames, layout.cols) for index in range(frames)
    )

    text = "\n\n".join(
        [
            f"Pixel art in-between frames for a {action} animation.",
            (
                "You are given two reference images: the FIRST is the pose at the start "
                "of this segment, the SECOND is the pose at the end. Draw ONLY the "
                "frames that go BETWEEN them. Neither reference pose may be reproduced "
                "as-is in any cell — those two frames already exist."
            ),
            (
                "The image you are editing is a template: every cell already contains "
                "the starting pose at the correct size and position. Move it toward the "
                "second reference. Keep each cell's character size, body-root position, "
                "foot-contact line and padding exactly as the template has them. "
                "Never zoom or resize a pose to fill its cell."
            ),
            (
                f"Layout: exactly {layout.cols} columns x {layout.rows} rows of "
                f"{layout.capacity} equally sized cells, read left to right"
                + (" then top to bottom" if layout.rows > 1 else "")
                + f".\nDraw exactly {frames} frames, one per cell, evenly spaced in time:"
                + f"\n{progression}"
            ),
            (
                "Interpolation constraints — this is a transition, NOT a redesign:\n"
                "- the character is the SAME in every cell: same face, hair, outfit, "
                "weapon in the same hand, same body proportions, same overall size\n"
                "- use ONLY colours that already appear in the two reference images; "
                "do not introduce new hues, highlights, glows or shading tones\n"
                "- change only what actually differs between the two reference poses; "
                "everything that is identical in both must stay pixel-identical\n"
                "- the motion between cells must be even — do not bunch several cells "
                "near one end and leave a jump at the other\n"
                "- do not add, remove or restyle any accessory, prop or effect"
            ),
            (
                "Placement constraints:\n"
                "- draw each frame entirely inside its own cell, never touching or "
                "crossing a cell boundary\n"
                f"- leave at least {PROMPT_MARGIN_PERCENT}% empty background margin on "
                "all four sides of every frame\n"
                "- the full body must be visible in every cell\n"
                "- the feet of every frame must rest on the same horizontal baseline\n"
                "- no cell may be empty and no two cells may be identical"
            ),
            _style_block(request),
            _background_block(key_color),
            "Do not include any of the following:\n"
            + negative_block(
                character=request.asset_type == "character", action=action
            ),
        ]
    )
    return CompiledPrompt(text=text, key_color=key_color, size=layout.size)
