# 资产 → Godot 场景

导出产出 `.tres` + png 之后，还要在 Godot 里建节点、设过滤、对锚点、连信号。
这一段用 **godot-ai** 的 MCP 工具完成（需要用户装了 `godot-ai` plugin
并在 Godot 里启用了 addon）。

下面四条是**我们实测出来、而 godot-ai 无从知道**的事。它不看资产的 Manifest，
也不知道我们的锚点约定 —— 不写死这四条，接进去的节点看着能用，实际是坏的。

---

## 1. 纹理 Filter 必须设 Nearest

Godot 4 的默认纹理过滤是线性的。像素资产用线性过滤会**整个糊掉** ——
这不是"稍微软一点"，是把我们花整条处理链还原出来的硬边缘全部抹平。

两种设法，任选其一：

- **项目级**（推荐）：`project.godot` 里
  `rendering/textures/canvas_textures/default_texture_filter=0`
- **节点级**：`CanvasItem.texture_filter = TEXTURE_FILTER_NEAREST`

项目级更保险：新加的节点自动继承，不会漏。

---

## 2. `AnimatedSprite2D.offset` 要设 `(0, -canvas_height/2)`

我们的锚点是 **bottom-center**：角色的脚踩在画布底边中点。
Godot 的 `AnimatedSprite2D` 默认把纹理**中心**对准节点原点。

不设 offset 的后果是角色整体上移半个画布高 —— 看着像悬空，
而且所有基于节点原点的碰撞体、Y-sort、脚底特效位置全错。

```gdscript
sprite.offset = Vector2(0, -manifest.canvas.height / 2.0)
```

`canvas.height` 从资产的 `asset-manifest.json` 读，不要硬编码。

---

## 3. 一次性动作要连 `animation_finished`，循环动作不要

Manifest 的每个动作都带 `loop` 字段：

- `loop: true`（idle / walk）—— 播完自动回到第一帧，**不要**连
  `animation_finished`，它根本不会触发
- `loop: false`（attack / hurt / death）—— 播完停在最后一帧，
  必须连 `animation_finished` 才能切回 idle，否则角色永远僵在收势那一帧

```gdscript
sprite.animation_finished.connect(_on_finished)

func _on_finished() -> void:
    if not sprite.sprite_frames.get_animation_loop(sprite.animation):
        sprite.play("idle_down")
```

---

## 4. `.tres` 与 png 必须整目录复制

`.tres` 里的 `ext_resource` 路径是 `res://` 相对的：

```
[ext_resource type="Texture2D" path="res://knight_01.png" id="1_atlas"]
```

只复制 `.tres` 不复制 png，Godot 直接 **Parse Error**（实测确认）。
两个文件要放在同一层，或者手改 `.tres` 里的路径。

推荐做法：把 `exports/godot/` 整个目录复制进 Godot 项目。

---

## 接入步骤

1. `pixel-asset export <资产目录> -t godot`
2. 把 `exports/godot/` 整个复制进 Godot 项目
3. 确认项目的 `default_texture_filter=0`（第 1 条）
4. 用 godot-ai 建 `AnimatedSprite2D` 节点，`sprite_frames` 指向 `.tres`
5. 设 `offset`（第 2 条）
6. 按 `loop` 决定要不要连信号（第 3 条）

---

## 验证

`tools/godot-gate/` 是**真机判据**：headless 载入 `.tres`、核对动画名与
帧数 / fps / loop / 每帧纹理尺寸，并真的挂到 `AnimatedSprite2D` 上播放。

单元测试只验证 `.tres` 的结构语法，**证明不了 Godot 能加载** ——
2026-07-29 实测把 `load_steps` 从 22 改成 3，Godot 4.3 照样加载成功，
可见结构断言的覆盖面是有限的。改导出器时手动跑一次门槛脚本。
