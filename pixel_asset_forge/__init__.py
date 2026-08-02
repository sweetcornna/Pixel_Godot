"""Pixel Asset Forge —— Manifest 驱动的像素游戏资产编译器。

分层职责见 docs/PLAN.md §2.1：AI 只生成视觉原料，一切需要精确性的操作
（切帧、抠图、对齐、量化、校验）由本地确定性代码完成。
"""

__version__ = "0.1.0"

# 三份 schema **各自独立**版本化（PLAN §5.4）。
#
# 初版用了一个共享的 SCHEMA_VERSION，Sprint 0 之后立刻暴露了问题：
# Manifest 因 fallback_stage 语义变更必须升 MAJOR，而 Request 与 Report 一个字段都没动。
# 共享版本号会逼着后两者跟着跳版本，用户还得为此跑一次没有任何内容的迁移。
REQUEST_SCHEMA_VERSION = "1.1"
PACK_SCHEMA_VERSION = "1.0"
MANIFEST_SCHEMA_VERSION = "2.1"
REPORT_SCHEMA_VERSION = "1.1"

# 产出资产的代码版本，写入 manifest.pipeline_version 用于回归排查。
PIPELINE_VERSION = __version__

__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "PACK_SCHEMA_VERSION",
    "PIPELINE_VERSION",
    "REPORT_SCHEMA_VERSION",
    "REQUEST_SCHEMA_VERSION",
    "__version__",
]
