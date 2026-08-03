# Tileset 规则

一套地面 tile 从生成到进引擎，四段链路各有一条只在这条链上成立的判据。
这份文档是给 Agent 看的**判据与话术**，不是 CLI 手册 —— 命令怎么敲见 `SKILL.md`。

---

## 1. 写 request：三条会被直接拒收或产出废图的

```yaml
asset_type: tileset
tileset:
  tile_size: [32, 32]
  tiles:
    - tile_id: grass_base
      description: >-
        Dense short green grass covering the whole square evenly, with a few
        scattered small grey stones. No border.
```

- **不写 `background`。** tile 满幅不透明，去背景那一步根本不会执行；写了会被拒收。
- **`tile_size` 不是 `style.target_size`。** 前者是"每块 tile 精确多大"，
  后者是静态资产那套"内容占画布多少比例"。两者不是一回事。
- **每条 `description` 都要写 "no border" / "fills the whole square"。**
  模型最常见的失败是把 tile 画成一张带边框的方形贴图 —— 平铺后是满屏网格线。

## 2. 无缝：两条判据，都是 fatal

| 判据 | 抓什么 | 平铺后看到什么 |
|---|---|---|
| `tile_seam` | 对边接不上（如整幅左右渐变） | 每隔一格一道突变 |
| `tile_border` | 带边框 / 暗角 | 一片规则网格线 |

**第二条不能省。** 带边框的 tile 接缝处是"边框接边框"，两边一样暗，
`tile_seam` 对它**恒判通过** —— 而它恰恰是最常见的失败形态。

报失败时**不要建议用户忽略**。让他改 `description` 再
`create-tileset --regenerate`。阈值尚未用真实 tile 校准，但两条判据的形状是稳的。

导出后让用户看 `previews/contact-sheet.png`：判据只算数值，
平铺起来像不像、有没有肉眼可见的重复图案，只有人能判。

## 3. 邻接表：对角矩阵是**正确答案**，不是失败

`create-tileset` 顺带从像素推出"哪块能挨着哪块"，写进 Manifest，
随 `generic-json` 导出。**不额外调用 API。**

判据同样是两条，第二条的理由与上面同源 —— 接缝比的分母是纹理颗粒度，
而**噪声一大它就会把"材质换了"这件事稀释掉**（实测草接水在颗粒度 60 时接缝比
只有 1.88，阈值是 3）。所以另一条比的是两侧边缘的**均值之差**，不做任何归一化。

**用户看到"三种材质各自只能接自己"时，不要说这是 bug。** 草、土、水本来就不能
直接挨着，中间需要**过渡 tile**——而那类 tile 目前这条链还产不出来。
这张表如实说出了"想把草和水放一起，你还缺一类 tile"。

`validate` 的 `tile_adjacency` 会拿当前像素重算比对：产出之后有人换过 tile 图，
它会喊停。

## 4. 地图：单色地图也是正确答案

```bash
pixel-asset create-map outputs/grass_field --width 24 --height 16 --seed 42
```

WFC 的 Simple Tiled Model，吃的就是上面那张邻接表。**不调用 API**，
同 seed + 同邻接表 + 同尺寸 → 同一张地图，逐格相等。

**同一个道理再说一次**：邻接表是对角矩阵时，铺出来的地图必然只有一种材质 ——
地图网格是连通的，每一步都要求两边相容。缺的是过渡 tile，不是更好的求解器。
CLI 自己会提示这一点，**别把它当故障报给用户**。

撞上矛盾时求解器换 seed 重试，仍然不行就报错 —— 绝不交一张有非法接缝的半成品。
`validate` 的 `map_adjacency` 逐对相邻格核对（水平与垂直分开查）。

## 5. 导出：三个目标，各自的欠账不同

| 目标 | 产出 | 真机验证 |
|---|---|---|
| `godot` | `TileSet` `.tres` + 图集 + `set_cell()` 片段 | ✅ Godot 4.7.1 headless 四层验过 |
| `tiled` | `.tsx` / `.tsj` + 每张地图的 `.tmx` / `.tmj` | ❌ 本机没有 Tiled，**未验证** |
| `generic-json` | 图集坐标 + 四方向邻接 + 地图 `rows` | — |

**Godot 那条不产原生 `.tscn`**：`TileMapLayer` 把地图存成打包字节数组，
凭记忆拼二进制不如给一段 `set_cell()` 的 GDScript ——读一遍就能确认对错。

**Tiled 那条要如实告诉用户没验过。** 保证止于"结构符合文档所述、GID 能往回解回
原 tile"，不等于 Tiled 一定能正常渲染。`firstgid` 差 1、行列主序搞反、CSV 按列
输出，这三种写错法 Tiled 都会**正常打开**、然后渲染出一张全错的地图 ——
所以"能打开"不是判据，用户回报打开正常也不代表这条链验过了。

---

## 一句话版本

`tileset` 走单个 request 不走 pack；无缝两条判据都是 fatal；
邻接表出对角矩阵和地图出单色**都是正确答案**，说明缺过渡 tile；
Godot 导出验过、Tiled 导出没验过，后者要如实说。
