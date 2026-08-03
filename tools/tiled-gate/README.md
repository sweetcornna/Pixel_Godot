# Tiled 加载与渲染门槛

`.tmx` / `.tsx` 的结构断言和第三方解析互证，仍证明不了 Tiled 自己能加载并正确
渲染这些文件。本目录用 Tiled 随附的 `tmxrasterizer` 走官方 libtiled 加载与渲染
路径，再与独立合成的期望图逐像素比较。

## 运行

先让资产目录同时具备源 tile、地图逐格记录和 Tiled 导出物：

```text
<资产目录>/asset-manifest.json
<资产目录>/frames/tiles/*.png
<资产目录>/maps/<地图名>.json
<资产目录>/exports/tiled/<地图名>.tmx
<资产目录>/exports/tiled/<资产名>.tsx
<资产目录>/exports/tiled/<资产名>.png
```

安装 Tiled 后运行：

```bash
python3 tools/tiled-gate/verify.py <资产目录>

# tmxrasterizer 不在 PATH 时显式指定
python3 tools/tiled-gate/verify.py <资产目录> \
  --tmxrasterizer /usr/bin/tmxrasterizer

# Manifest 有多张地图时默认全验，也可只验一张
python3 tools/tiled-gate/verify.py <资产目录> --map overworld
```

脚本会给子进程注入 `QT_QPA_PLATFORM=offscreen`，不需要显示服务器。成功退出必须同时
满足三件事：

1. 基线 TMX 经 `tmxrasterizer` 渲染出的 PNG，与本地期望 PNG 每个 RGBA 像素完全相等。
2. 地图层所有 GID 整体 `+1` 后，官方渲染 PNG 必须与期望图不相等，判为预期 FAIL。
3. TMX 的 `firstgid` 从 `1` 偏移到 `0` 后，官方渲染 PNG 也必须与期望图不相等，
   判为预期 FAIL。

任一基线不相等、官方渲染失败，或任一反例意外相等，脚本都以非零状态退出。

## 判据为什么独立

官方侧只接收导出器产出的 `.tmx`，由 tmxrasterizer/libtiled 自己跟随外部 `.tsx`、
加载图集并解释 GID。期望侧只读取：

- `frames/tiles/*.png` 中每块源 tile 的像素；
- `maps/*.json` 中每一格原本记录的 `tile_id`。

期望侧按 `tile_id` 逐格贴源图，不读取 TMX 的 GID，不读取导出图集，也不 import
`pixel_asset_forge`。因此它绝不调用项目自己的 `gid_for` / `tile_id_for_gid`；两条
路径只在最后的 RGBA 像素上相遇。

## 改坏再跑实测

**2026-08-03，Tiled 1.11.90（Debian 包 `1.11.90-1`，`tmxrasterizer` 位于
`/usr/bin/tmxrasterizer`）**，用 `examples/grass_field.yaml` 经 mock provider 走完
`create-tileset` → `create-map 7x4` → `validate` → `export -t tiled`，再运行：

```bash
python3 tools/tiled-gate/verify.py \
  /tmp/pixel-skill-tiled-gate-20260803/outputs/grass_field \
  --tmxrasterizer /usr/bin/tmxrasterizer
```

真实输出结论：

| 输入 | 逐像素结果 | 门槛结论 |
|---|---|---|
| 原始导出物 | 28,672 个 RGBA 像素完全相等 | PASS |
| 地图层 28 格 GID 全体 `+1` | 28,672 个像素不同，首差异 `(0, 0)` | FAIL（符合预期） |
| `firstgid 1 -> 0` | 28,672 个像素不同，首差异 `(0, 0)` | FAIL（符合预期） |

两条反例都是先实际生成篡改文件、再由同一个官方渲染器产出 PNG，最后被逐像素比对
判为 FAIL；不是只在测试里写一个预期描述。

## 验证边界

这条门槛证明的是：对当前导出的正交 tile map，Tiled 官方 libtiled 能跟随 TMX →
外部 TSX → 图集这条链加载并渲染，而且渲染结果逐像素等于源地图应有的画面。

它没有启动 Tiled GUI，没有验证窗口创建、鼠标/键盘操作、菜单、面板布局或在编辑器里
修改后保存。就“加载并渲染这份地图”而言，和真正打开 Tiled GUI 相比，剩下的是窗口
交互本身这一层；因此这里不声称做过人工 GUI 打开验证，也不把命令行渲染夸大成完整
编辑器交互测试。
