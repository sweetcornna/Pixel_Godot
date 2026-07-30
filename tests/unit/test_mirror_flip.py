"""镜像翻转检测。

用户反复反馈"走着走着突然翻面"。这在播放时极刺眼，静态看单帧却完全正常。

**判据的可信度有限，测试要如实反映这一点** —— 既锁住它抓得住真翻面，
也锁住它在低手性角色上不该乱报。
"""

from __future__ import annotations

import numpy as np

from pixel_asset_forge.validation.mirror_flip import (
    FLIP_THRESHOLD,
    detect_mirror_flips,
)


def chiral_sprite(*, flipped: bool = False, size: int = 64) -> np.ndarray:
    """一个强手性的"角色"：右手边有一把"剑"。"""
    frame = np.zeros((size, size, 4), dtype=np.uint8)
    cx = size // 2
    frame[16:52, cx - 7 : cx + 7] = (139, 90, 43, 255)   # 躯干
    frame[20:46, cx + 9 : cx + 13] = (220, 220, 200, 255)  # 剑
    return frame[:, ::-1].copy() if flipped else frame


def round_sprite(size: int = 64) -> np.ndarray:
    """一个左右对称的"史莱姆" —— 手性判据对它无效。"""
    frame = np.zeros((size, size, 4), dtype=np.uint8)
    yy, xx = np.mgrid[0:size, 0:size]
    blob = (xx - size // 2) ** 2 + (yy - size // 2) ** 2 < (size // 3) ** 2
    frame[blob] = (60, 140, 220, 255)
    return frame


def test_a_flipped_frame_is_caught() -> None:
    frames = [chiral_sprite(), chiral_sprite(), chiral_sprite(flipped=True)]
    report = detect_mirror_flips(frames)
    assert report.applicable
    assert report.flipped == (2,)
    assert "翻面" in report.summary()


def test_a_consistent_sequence_passes() -> None:
    report = detect_mirror_flips([chiral_sprite() for _ in range(4)])
    assert report.applicable and not report.flipped


def test_a_symmetric_subject_is_not_judged() -> None:
    """石魔方正、史莱姆浑圆，自身与镜像的重叠率就接近 1，
    手性信号只有零点零几，与噪声同量级。对它们判翻面纯属乱报。
    """
    report = detect_mirror_flips([round_sprite() for _ in range(4)])
    assert not report.applicable
    assert "对称" in report.summary()


def test_mismatched_sizes_skip_instead_of_crashing() -> None:
    """畸形输入自有 ``frame_size`` 那条致命项去拦。这里再抛一次异常
    只会让整份报告生不出来 —— 用户连"哪里坏了"都看不到。
    """
    frames = [chiral_sprite(size=64), chiral_sprite(size=48)]
    report = detect_mirror_flips(frames)
    assert not report.applicable


def test_the_threshold_leaves_room_for_the_measured_false_positives() -> None:
    """逐图核对过的实测值：

        真翻面   -0.10 / -0.10 / -0.06 / -0.03
        误报     -0.024 / -0.020（看图是正常的受击后仰与倒地）

    阈值必须落在两组之间。余量只有 0.006 —— 正因如此这条检查只到 MEDIUM，
    不阻断放行。
    """
    assert -0.03 <= FLIP_THRESHOLD <= -0.025, (
        "阈值离实测的误报太近（-0.024）或把最弱的真阳性（-0.03）漏掉了"
    )
