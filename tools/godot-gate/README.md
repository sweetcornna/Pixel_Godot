# Godot 加载门槛

`.tres` 的结构断言（`tests/integration/test_export.py`）证明不了「Godot 能加载」。
这个目录是真机判据。

## 跑

```bash
# 1. 下载 Godot 4（本仓库不附带二进制）
curl -L -o godot.zip https://github.com/godotengine/godot/releases/download/4.3-stable/Godot_v4.3-stable_linux.x86_64.zip
python3 -c "import zipfile; zipfile.ZipFile('godot.zip').extractall('.')"
chmod +x Godot_v4.3-stable_linux.x86_64

# 2. 导出资产并把产物放进本目录
pixel-asset export <资产目录> -t godot
cp <资产目录>/exports/godot/* .

# 3. 写 expected.json（动画名 → frames / fps / loop / w / h），见下
# 4. 跑
./Godot_v4.3-stable_linux.x86_64 --headless --import
./Godot_v4.3-stable_linux.x86_64 --headless --script verify.gd
```

`GATE-OK 全部通过` 才算过。脚本里 `knight_01` 是硬编码的资产名，换资产时改掉。

`expected.json` 直接从 Manifest 生成：

```python
import json
m = json.load(open("<资产目录>/asset-manifest.json"))
w, h = m["canvas"]["width"], m["canvas"]["height"]
json.dump({
    k: {"frames": len(a["frames"]), "fps": a["fps"], "loop": a["loop"], "w": w, "h": h}
    for k, a in m["animations"].items() if a.get("frames")
}, open("expected.json", "w"))
```

## 验什么

**A. 资源本身** —— 动画名、帧数、fps、loop、每帧纹理尺寸。

**B. 衔接层的四条必设项**（见 `skills/pixel-asset-forge/references/godot-handoff.md`）。
这四条是我们实测出来、而 godot-ai 无从知道的事 —— 不设则接进去的节点
"看着能用、实际是坏的"：

1. 项目默认纹理过滤是 Nearest（线性过滤会把像素全糊掉）
2. `offset` 把 bottom-center 锚点对到节点原点
3. 一次性动作标成一次性、循环动作标成循环（决定要不要连 `animation_finished`）
4. `ext_resource` 指向的纹理真的在（`.tres` 与 png 必须整目录复制）

## 这个门槛抓得住什么

一个永远通过的门槛没有价值。实测验证过（改坏再跑）：

| 改动 | 结果 |
|---|---|
| 帧数少一帧 | ✓ 抓住 —— `GATE-FAIL attack_down 帧数 3 ≠ 4` |
| 纹理路径指向不存在的文件 | ✓ 抓住 —— Godot 直接 Parse Error |
| 把 png 移走 | ✓ 抓住 |
| `default_texture_filter` 0 → 1 | ✓ 抓住 —— `项目默认纹理过滤是 1，不是 Nearest(0)` |
| `load_steps` 22 → 3 | ✗ **抓不住，Godot 照样加载** |

最后一条是实测更正：`load_steps` 是给加载进度条用的提示，不是硬校验。
仓库里原本写着"数不对会让 Godot 加载失败"，那句话是错的。

## 为什么不进 CI

Godot 二进制 110 MB，且这条门槛只在导出器改动时才有意义。
改 `exporters/godot.py` 时手动跑一次。

---

# TileSet 门槛（Sprint 8）

`verify_tileset.gd` 是 TileSet + 地图那条链的真机判据。与上面那条并列，
但验的东西不同 —— **「能加载」证明不了「每一格指向它该指向的 tile」**。

## 跑

```bash
# 1. 产出资产（tileset + 地图）并导出两种目标
pixel-asset create-tileset examples/grass_field.yaml
pixel-asset create-map     outputs/grass_field --width 6 --height 4 --seed 7
pixel-asset validate       outputs/grass_field
pixel-asset export         outputs/grass_field -t godot
pixel-asset export         outputs/grass_field -t generic-json   # 门槛要读它的图集坐标

# 2. 搭工程
mkdir -p gate/tiles && cd gate
cp ../tools/godot-gate/{project.godot,verify_tileset.gd} .
cp ../outputs/grass_field/exports/godot/*.{tres,png} .
cp ../outputs/grass_field/frames/tiles/*.png tiles/     # 第三层要逐块比像素
python3 ../tools/godot-gate/make_expected.py ../outputs/grass_field > expected.json

# 3. 跑
<godot> --headless --import
<godot> --headless --script verify_tileset.gd
```

`GATE-OK TileSet 四层全部通过` 才算过。`expected.json` **必须由脚本从产物生成**，
手抄会把"我以为导出的是什么"验成"导出的确实是什么"。

## 验什么

| 层 | 验什么 | 抓的是 |
|---|---|---|
| A | 加载成 `TileSet`、`tile_size`、每个格坐标 `has_tile` | 少写 `列:行/0 = 0` 那一行 → 图里有图、编辑器选不中 |
| B | `ext_resource` 的 png 真的在、项目过滤是 Nearest | 只复制 `.tres` 没复制 png；线性过滤把像素糊掉 |
| C | **图集那一格里装的确实是那块 tile 的像素** | 打包顺序错、坐标与图集对不上 —— A 层对它恒判通过 |
| D | 地图逐格 `set_cell` → `get_cell_atlas_coords` 读回 | 地图与 TileSet 接不上 |

C 层是被一次失败的反例逼出来的：起初只有 A/B/D，构造"格坐标整体转置"时发现
**抓不住** —— 而调查下来那个反例本身是无效的（3 块 tile 摆成 2×2，坐标集
`{(0,0),(1,0),(0,1)}` 转置后等于自己，改出来的文件与原文件逐行等价，
没有东西可抓）。用 5 块 tile（3×2，非对称）重做才是真反例，A 层当场抓住。

C 层因此不是为转置加的，而是为**另一类**错误：坐标声明对、图集打包顺序错。
那一类 A/B/D 全都判通过。

## 这个门槛抓得住什么

**实测记录（2026-08-02，Godot 4.7.1 headless，改坏再跑）：**

| 改动 | 结果 |
|---|---|
| 删掉一行 `列:行/0 = 0` | ✓ 抓住 —— `grass_base 的格 (1, 0) 在 TileSet 里不存在` |
| `tile_size` 改成 16 | ✓ 抓住 —— `tile_size (16, 16) ≠ (32, 32)` |
| `texture_region_size` 改成 16 | ✓ 抓住 |
| 删掉图集 png | ✓ 抓住 —— TileSet 加载返回 null |
| `default_texture_filter` 0 → 1 | ✓ 抓住 |
| 图集里两块 tile 对调 | ✓ 抓住 —— `图集格 (1,0) 里装的不是 grass_base`（**只有 C 层抓得住**） |
| `列:行` 写成 `行:列`（5 块 tile，非对称布局） | ✓ 抓住 —— `plain_c 的格 (2, 0) 在 TileSet 里不存在` |
| `列:行` 写成 `行:列`（3 块 tile，2×2） | — **无效反例**：坐标集转置后等于自己，文件逐行等价 |

## 实测结论

`grass_field`（3 块 tile + 6×4 地图）与一套 5 块 tile 的合成 tileset，
均在 **Godot 4.7.1 stable headless 上四层全部通过**。

> Tiled 那条链**仍未验证** —— 本机没有 Tiled 也没有 TMX 解析库。
> Sprint 8 总门槛第五条"Godot 与 Tiled 均可打开"因此只完成了 Godot 那一半。
