# Godot 4.7 骑士动画示例

这是一个可直接运行的最小 Godot 4.7 工程，使用仓库已有、由
`gpt-image-2` 生成并通过真机门槛的 `knight_01` 资产。

## 打开与运行

1. 启动 Godot 4.7 编辑器，点击“导入”，选择本目录中的 `project.godot`。
2. 打开工程后运行项目（F6/F5 均可；主场景已配置）。
3. 骑士会先播放一次 `attack_down`，结束后自动切回并持续循环
   `walk_down`。骑士的脚底锚点位于 `640×360` 逻辑视口中央。

命令行验证可在仓库根目录执行：

```bash
/mnt/data/project/godot/Godot_v4.7.1-stable_linux.x86_64 --headless --path examples/godot-demo --import
/mnt/data/project/godot/Godot_v4.7.1-stable_linux.x86_64 --headless --path examples/godot-demo --script res://verify.gd
```

## 四条必设项

以下原始表述均来自
`plugins/pixel-asset-forge/skills/pixel-asset-forge/references/godot-handoff.md`。

1. **“纹理 Filter 必须设 Nearest”**：`project.godot` 第 22 行的
   `textures/canvas_textures/default_texture_filter=0` 项目级字段已设置为
   Nearest。
2. **“AnimatedSprite2D.offset 要设 (0, -canvas_height/2)”**：
   `asset-manifest.json` 的真实 `canvas.height` 为 96，复制后的 `.tres`
   每帧区域也为 `96×96`；`main.tscn` 第 14 行据此设置
   `offset = Vector2(0, -48)`。
3. **“一次性动作要连 animation_finished，循环动作不要”**：`main.gd`
   第 7 行连接信号，第 8 行播放非循环的 `attack_down`，第 11-13 行的
   回调仅在该动画结束时切回循环的 `walk_down`。
4. **“.tres 与 png 必须整目录复制”**：`knight_01_frames.tres` 与
   `knight_01.png` 位于本目录同一层；前者第 3 行的相对字段
   `path="knight_01.png"` 因而可直接解析。`main.tscn` 第 4 行通过
   `res://knight_01_frames.tres` 引用该资源。

此外，`main.tscn` 第 10 行把节点放在 `(640 / 2, 360 / 2)`，第 13 行将
`autoplay` 明确设为 `walk_down`。`main.gd` 启动后接管播放一次
`attack_down`，用于实际演示一次性动作的信号回退。
