"""Godot 4 SpriteFrames 导出。

产出一张总图 + 一份 ``.tres`` 资源，拖进 ``AnimatedSprite2D`` 即可用。

## 格式要点

Godot 4 的 `SpriteFrames` 资源结构：

```text
[gd_resource type="SpriteFrames" load_steps=N format=3]
[ext_resource type="Texture2D" path="res://…png" id="1_atlas"]
[sub_resource type="AtlasTexture" id="…"]     ← 每帧一个，指向总图上的矩形
[resource]
animations = [{ "frames": […], "loop": …, "name": &"walk_down", "speed": … }]
```

三处容易写错：

1. **动画名要用 `&"name"`（StringName）**，写成普通字符串 Godot 4 读不到。
2. **`speed` 是每秒帧数**，不是每帧时长；每帧的 `duration` 是**相对倍率**
   （1.0 = 按 speed 走），不是秒。
3. **`ext_resource` 的路径必须真的存在**，否则 Godot 直接 Parse Error。

`load_steps` 应当等于 `ext_resource` + `sub_resource` 的总数 + 1。
这里原本写着"数不对会导致资源加载失败"——**实测证伪**：改成 3（正确值 22）
之后 Godot 4.3 照样加载成功，它是给加载进度条用的提示，不是硬校验。
仍然写对它（零成本），但别把它当护栏。

## 验证状态

2026-07-29 已用 **Godot 4.3 真机验证**：载入 SpriteFrames、核对动画名与
帧数/fps/loop/每帧纹理尺寸，并挂到 `AnimatedSprite2D` 上播放通过。
PLAN §8 Sprint 6 的退出门槛"用真实 Godot 工程验证，不是理论上兼容"**已达成**。

重跑见 `tools/godot-gate/`。改动本模块时手动跑一次——单元测试只验证
`.tres` 的结构语法，证明不了 Godot 能加载。
"""

from __future__ import annotations

from pathlib import Path

from ..models.manifest import AssetManifest
from ..processing.spritesheet import save_png
from .base import (
    AnimationView,
    Exporter,
    ExportResult,
    animation_views,
    load_frames,
    load_static_image,
)
from .generic_json import build_atlas

#: Godot 4 的资源格式版本。
RESOURCE_FORMAT = 3


def _resource_id(prefix: str, index: int) -> str:
    return f"{prefix}_{index:03d}"


def build_spriteframes(
    manifest: AssetManifest,
    views: list[AnimationView],
    regions: dict[str, list[tuple[int, int, int, int]]],
    texture_path: str,
) -> str:
    """生成 ``.tres`` 文本。

    ``ext_resource`` 用**相对 ``.tres`` 自身**的路径而非 ``res://`` 绝对路径：
    绝对形式写死了"png 必须在项目根"，与我们"整目录复制进项目"的交付说法冲突 ——
    照说明放进 ``res://assets/`` 就会 Parse Error。相对路径两种放法都成立
    （Godot 的文本资源格式对二者都合法）。
    """
    atlas_id = "1_atlas"

    sub_resources: list[str] = []
    frame_ids: dict[str, list[str]] = {}
    counter = 0
    for view in views:
        ids = []
        for x, y, width, height in regions[view.key]:
            rid = _resource_id("AtlasTexture", counter)
            counter += 1
            ids.append(rid)
            sub_resources.append(
                f'[sub_resource type="AtlasTexture" id="{rid}"]\n'
                f'atlas = ExtResource("{atlas_id}")\n'
                f"region = Rect2({x}, {y}, {width}, {height})\n"
            )
        frame_ids[view.key] = ids

    animations = []
    for view in views:
        frames = ", ".join(
            f'{{\n"duration": 1.0,\n"texture": SubResource("{rid}")\n}}'
            for rid in frame_ids[view.key]
        )
        animations.append(
            "{\n"
            f'"frames": [{frames}],\n'
            f'"loop": {"true" if view.loop else "false"},\n'
            f'"name": &"{view.key}",\n'
            f'"speed": {float(view.fps)}\n'
            "}"
        )

    # load_steps = ext_resource 数 + sub_resource 数 + 1（资源本身）
    load_steps = 1 + len(sub_resources) + 1

    header = (
        f'[gd_resource type="SpriteFrames" load_steps={load_steps} '
        f'format={RESOURCE_FORMAT}]\n\n'
        f'[ext_resource type="Texture2D" path="{texture_path}" id="{atlas_id}"]\n\n'
    )
    body = "\n".join(sub_resources)
    tail = "\n[resource]\nanimations = [" + ", ".join(animations) + "]\n"
    return header + body + tail


HANDOFF_NAME = "GODOT-README.md"

_FILTER_NOTE = (
    "纹理 Filter 必须是 Nearest，否则 Godot 默认的线性过滤会把像素全糊掉。"
    "项目级设法（推荐）：`project.godot` 里 "
    "`rendering/textures/canvas_textures/default_texture_filter=0`；"
    "节点级设法：`CanvasItem.texture_filter = TEXTURE_FILTER_NEAREST`。"
)


def _handoff_doc(manifest: AssetManifest, lines: list[str]) -> str:
    """交付说明。**必须随目录落盘** —— 只打在终端的知识传不到下游。

    ``create-asset`` 与 ``create-asset-pack`` 都不打印 exporter 的 notes，
    批量生产的人因此一次也看不到 Filter/锚点这些"不设就是坏的"约定。
    """
    body = "\n".join(f"{i}. {line}" for i, line in enumerate(lines, 1))
    return (
        f"# {manifest.asset_id} → Godot\n\n"
        f"由 pixel-asset-forge {manifest.pipeline_version} 导出。"
        "以下每条都是实测出来的必设项，不设则接进去的节点「看着能用、实际是坏的」。\n\n"
        f"{body}\n"
    )


class GodotExporter(Exporter):
    target = "godot"

    def export(self, manifest: AssetManifest, root: Path, out_dir: Path) -> ExportResult:
        views = animation_views(manifest, root)
        result = ExportResult(target=self.target)
        if not views:
            if manifest.static_image is None:
                result.notes.append("没有任何可导出的静态图或动作")
                return result
            self.ensure_dir(out_dir)
            image = load_static_image(manifest, root)
            texture_name = f"{manifest.asset_id}.png"
            result.files.append(save_png(image, out_dir / texture_name))
            notes = [
                f"把 {texture_name} 复制进 Godot 项目并用于 Sprite2D；"
                "锚点为 center，Sprite2D 默认 centered=true 即可。",
                _FILTER_NOTE,
            ]
            result.notes.extend(notes)
            handoff = out_dir / HANDOFF_NAME
            handoff.write_text(_handoff_doc(manifest, notes), encoding="utf-8")
            result.files.append(handoff)
            return result

        self.ensure_dir(out_dir)
        frames_by_key = {v.key: load_frames(root, v) for v in views}
        atlas, regions = build_atlas(views, frames_by_key)

        texture_name = f"{manifest.asset_id}.png"
        result.files.append(save_png(atlas, out_dir / texture_name))

        tres = build_spriteframes(manifest, views, regions, texture_name)
        tres_path = out_dir / f"{manifest.asset_id}_frames.tres"
        tres_path.write_text(tres, encoding="utf-8")
        result.files.append(tres_path)

        oneshot = [v.key for v in views if not v.loop]
        notes = [
            f"把 {out_dir.name}/ 整个目录复制进 Godot 项目（放项目根或任意子目录都行，"
            f"`.tres` 用的是相对同目录的纹理路径），"
            f"再把 {tres_path.name} 拖到 AnimatedSprite2D 的 Sprite Frames 属性上。",
            "锚点是 bottom-center：AnimatedSprite2D 的 offset 需设为 "
            f"(0, -{manifest.canvas.height // 2}) 才能让脚底对齐节点原点。",
            _FILTER_NOTE,
            (
                "一次性动作要连 `animation_finished` 信号，循环动作不要。"
                + (f"本资产的一次性动作：{'、'.join(oneshot)}。" if oneshot else
                   "本资产全部动作都是循环的。")
            ),
        ]
        result.notes.extend(notes)
        handoff = out_dir / HANDOFF_NAME
        handoff.write_text(_handoff_doc(manifest, notes), encoding="utf-8")
        result.files.append(handoff)

        result.notes.append(
            "本格式已在 Godot 4.3 真机验证：载入 SpriteFrames、核对帧数/fps/loop/"
            "纹理尺寸，并挂到 AnimatedSprite2D 播放。重跑见 tools/godot-gate/。"
        )
        return result
