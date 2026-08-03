# Pixel Asset Forge

[![CI](https://github.com/sweetcornna/Pixel_Godot/actions/workflows/ci.yml/badge.svg)](https://github.com/sweetcornna/Pixel_Godot/actions/workflows/ci.yml)

把自然语言描述编译为可直接导入游戏引擎的像素资产。

不是 GPT Image API 的包装器，而是 **Manifest 驱动的像素资产编译器**：
AI 只生成视觉原料，一切需要精确性的操作（切帧、抠图、对齐、量化、校验）
由本地确定性代码完成。

完整设计见 [`docs/PLAN.md`](docs/PLAN.md)，关键决策见 [`docs/adr/`](docs/adr/)。

---

## 当前进度

| Sprint | 内容 | 状态 |
|---|---|---|
| 0 | 垂直切片与技术验证（7 条假设 A-1 ~ A-7） | ✅ 完成 —— 见 [验证报告](docs/sprint-0-report.md) |
| 1 | 项目骨架 · Schema · 状态机 · 配置 · Mock Provider · `init`/`plan`/`doctor` | ✅ 完成 |
| 2 | OpenAI Provider · 缓存 · 并发与退避 | ✅ 完成 |
| 3 | 确定性图像处理（切帧/键控/对齐/量化）· `process` | ✅ 完成 |
| 4 | 种子图与动画网格流水线 · Prompt Compiler · 人工闸门 | ✅ 完成 |
| 5 | 验证引擎与 Repair Planner · `validate`/`repair` | ⚠️ 见下 |
| 6 | MVP：四方向 × idle/walk · Godot 导出 · Contact Sheet | ⚠️ 见下 |
| 7 | 道具、特效与批量任务 · 五种资产包 | ✅ 完成 —— 静态三种 + 动画 `spell_bundle` / `combat_bundle`，五条总退出门槛按 pack 类型逐格复核 |
| 8 | Tileset 与地图 | 🚧 进行中 —— 基础地面 tile、邻接表推导、WFC 地图生成、Tiled 导出已完成；过渡 tile、Godot terrain 未开工，Godot / Tiled 均欠一次真机验证 |
| 9 | Skill、MCP、CI 与发布 | ✅ 完成 —— CI 三平台、Skill 防漂移、MCP 6 工具、live gate（真实模型 FAIL→校准→PASS）、发布纵切（twine 双 PASSED + 全新环境装机实证）；六条总退出门槛全部达成 |
| 10 | Godot 工作站 | ✂️ 范围裁剪（owner 决策 2026-08-03）—— 本项目只做资产生成与自动化；已交付：三 plugin 结构、许可声明、衔接层四条真机背书、`examples/godot-demo/`（真实资产，13 项 VERIFY-OK）；"AI 驱动编辑器开发游戏"不做 |

### 未达标项

**1. per-action 阈值只完成了五个角色动作的校准。** `idle` / `walk` / `attack` /
`hurt` / `death` 已用 6 个角色、30 个动作复验；`cast` / `travel` / `impact` / `loop`
仍无样本，`up` 方向修正系数也未验证。详见
[阈值校准记录](docs/threshold-calibration.md)。

**2. 帧序被打乱无法自动检测。** PLAN §9.2 称这是"捕捉静默失败的唯一自动手段"，
但实测四种判据在正序与随机乱序上完全重叠，其中"局部离群"判据**方向相反**
（正序反而更高）—— 照原设计实现会把正确产出判为失败。
该检查已降为 low 且永不阻断，只报告统计量。
**现阶段唯一的防线是人眼看 `previews/*.gif`。**
理由与实测数据见 `pixel_asset_forge/validation/frame_order.py` 的模块文档。
`export` 产出的 `previews/contact-sheet.png` 就是为这条缺口准备的。

**3. Tiled 产物尚未真机验证（Godot 那一半已补上）。**

- **Godot ✅** —— `SpriteFrames`（角色动画）2026-07-29 在 Godot 4.3 验过；
  Sprint 8 的 `TileSet` `.tres` 与地图已于 **2026-08-02 在 Godot 4.7.1 headless
  验过**，四层全部通过（加载 → 纹理衔接 → **图集那一格里装的确实是那块 tile 的像素**
  → 地图逐格 `set_cell`/`get_cell_atlas_coords` 读回）。门槛与"改坏再跑"的实测
  记录见 [`tools/godot-gate/`](tools/godot-gate/)。
- **Tiled ⚠️** —— 已有 **pytmx 第三方互证**（2026-08-03）：用 Python 生态的
  TMX 参考实现独立解析我们的 .tmx/.tsx，28 格逐格图集坐标与源地图一致，
  firstgid/columns/tilecount 对上；两条篡改反例（GID+1、firstgid 偏移）都被
  独立解码抓出。见 `tests/integration/test_tiled_pytmx.py` —— 它**不引用**我们
  自己的 GID 函数，独立性是全部意义。
  仍欠 Tiled **GUI** 真机打开验证 —— pytmx 互证 ≠ 官方渲染器行为。

Sprint 8 总门槛第五条"Godot 与 Tiled 均可打开"因此**只完成了 Godot 那一半，
继续不打勾**。

---

## 安装

需要 Python 3.12+。发布包可用 [uv](https://docs.astral.sh/uv/) 安装为独立工具：

```bash
uv tool install pixel-asset-forge         # CLI
uv tool install 'pixel-asset-forge[mcp]'  # CLI + MCP（二选一）
```

从源码开发或验证本仓库时：

```bash
uv sync --all-extras
./scripts/install-skill.sh  # 安装 Claude Code Skill；已有 ~/.codex 时也安装 Codex Skill
```

卸载 Skill 使用 `./scripts/install-skill.sh --uninstall`。

API Key 只从环境变量读取，**永远不要写进配置文件或请求文件**：

```bash
export PIXEL_ASSET_API_KEY=...     # 或 OPENAI_API_KEY
```

## 快速开始

```bash
uv run pixel-asset doctor                    # 检查环境（不需要 Key）
uv run pixel-asset init                      # 生成项目配置与目录
uv run pixel-asset plan examples/knight.yaml # 看清楚要生成什么、要花多少次调用

uv run pixel-asset create-character examples/knight.yaml   # 产出 canonical seed
#   ↑ 停在【人工闸门】：先看 outputs/knight_01/seed-pixel.png
uv run pixel-asset create-animation --asset knight_01 \
    --action walk --direction down --approve-seed          # 放行并生成动作网格
uv run pixel-asset process outputs/knight_01               # 调参时离线重跑，不花钱
uv run pixel-asset validate outputs/knight_01              # 失败时退出码为 3
uv run pixel-asset export  outputs/knight_01               # Godot + Generic JSON + Contact Sheet
```

**人工闸门不要跳过。** seed 是所有动画的身份基准，它不对则后续动画全部作废重来 ——
所以 `create-animation` 在 seed 未获批准时会直接拒绝执行。

### 资产 Pack

一批共享约束的资产使用 pack YAML：条目之间共享 `style` / `background` / `export`
和显式 `palette.colors`。五种 pack **共用同一份**
[`schemas/asset-pack.schema.json`](schemas/asset-pack.schema.json)，
展开成哪种资产类型由 `pack_type` 映射表决定：

| `pack_type` | 展开的资产类型 | 动画 | 示例 |
|---|---|:---:|---|
| `potion_pack` | `pickup` | — | [`examples/potion_pack.yaml`](examples/potion_pack.yaml) |
| `weapon_pack` | `weapon` | — | [`examples/weapon_pack.yaml`](examples/weapon_pack.yaml) |
| `environment_pack` | `environment_object` | — | [`examples/environment_pack.yaml`](examples/environment_pack.yaml) |
| `spell_bundle` | `spell` | ✅ | [`examples/spell_bundle.yaml`](examples/spell_bundle.yaml) |
| `combat_bundle` | `character` | ✅ | [`examples/combat_bundle.yaml`](examples/combat_bundle.yaml) |

加一种 pack 只需在映射表里加一行。`shared.animations` 是两类 pack 的分界：
动画 bundle **必须**声明它（整包共用同一组动作），静态 pack **必须**省略。
`combat_bundle` 的动作可以只写 `name`，帧数 / fps / loop 走内置动作缺省值。

pack 中不要写 `model`；pack 不选择模型；运行时使用 Config 解析后的有效 `model`
（当前默认 `gpt-image-2`），仍可按既有配置优先级覆盖。单资产失败不会取消其余资产，
同一 pack 可恢复续跑；完成后按 `asset_id` 逐项审核和导出。

```bash
uv run pixel-asset plan examples/potion_pack.yaml --save        # 自动识别 pack，核对批次计划并落盘
uv run pixel-asset create-asset-pack examples/potion_pack.yaml  # 静态 pack：一条命令跑完
uv run pixel-asset create-asset-pack examples/potion_pack.yaml --retry-failed   # 只重试失败的资产
uv run pixel-asset export outputs/health_potion -t godot        # 按资产目录导出
uv run pixel-asset export mana_potion -t godot                  # 或按 asset_id 导出
```

**动画 bundle 要跑两遍 —— 这不是缺陷，是 seed 人工闸门。** 第一遍只生成各资产的
canonical seed 并停在 `awaiting_approval`（同时写好 contact sheet 供你看图），
逐个批准后**重跑同一条命令**即续跑进动画：

```bash
uv run pixel-asset plan examples/combat_bundle.yaml --save
uv run pixel-asset create-asset-pack examples/combat_bundle.yaml     # 第一遍：停在 seed 闸门
uv run pixel-asset create-animation --asset knight_01 \
    --action attack --direction down --approve-seed                  # 看完图再批准
uv run pixel-asset create-asset-pack examples/combat_bundle.yaml     # 第二遍：跑完全部动作
```

等待批准**不算失败**，也不消耗动画调用；`plan` 会把 seed 与动画的调用数分列，
因为动画 bundle 不再是"资产数 = 调用数"（一个角色 × 3 个动作 × 4 个方向 = 13 次）。

跨动作缩放基准由协调器负责收敛：增量生成看不到未来的动作，基准只能边走边顶替，
批量跑完若发生过顶替，协调器会自动重跑一次本地 `process` 把全部动作统一到新基准
（**零 API 调用**），并在 `pack-summary` 里记一笔"因基准顶替重跑了处理"。

**批量执行前必须先 `plan --save`。** `create-asset-pack` 会逐个核对规划指纹，
缺少已保存任务表、或指纹与当前请求不一致时直接拒绝执行 ——
不会在用户不知情的情况下开始计费。中断后重跑同一条命令即断点续跑，
只想重跑失败项时加 `--retry-failed`。

单个静态资产不必包成 pack：

```bash
uv run pixel-asset create-asset requests/rusty_key.yaml   # 生成 → 处理 → 验证 → 导出
```

`create-asset` 只接受**不带 `animations`** 的静态资产类型（`pickup` / `weapon` /
`prop` / `ui_icon` / `environment_object`）；单资产一次 API 调用，不设 plan 前置闸门
（与 `create-character` 同口径）。动画请求会被拒收并指向 `create-character`。

> `requests/rusty_key.yaml` 是你自己写的单资产 request（`init` 会建好 `requests/` 目录）；
> `examples/` 下目前有角色示例、五份 pack 示例与一份 tileset 示例。

### 地面 Tileset

一套可平铺的地面 tile 走 `tileset` 请求，**不是 pack** —— pack 的产物是 N 个各自
独立导出的资产，而 Godot TileSet 与 Tiled 要的是一张图集加一份网格定义，
N 块 tile 属于同一个资产。

```bash
uv run pixel-asset plan examples/grass_field.yaml           # 每块 tile 各一次调用
uv run pixel-asset create-tileset examples/grass_field.yaml # 逐块生成 → 整套统一处理
uv run pixel-asset validate outputs/grass_field             # 查无缝平铺
uv run pixel-asset export   outputs/grass_field             # TileSet .tres + 图集 + JSON
```

tile 与其它资产有两处不同，照抄示例时容易踩空：

- **不写 `background`。** tile 是满幅不透明的，去背景那一步根本不会执行，
  写了会被直接拒收。
- **`tile_size` 独立于 `style.target_size`。** 前者是"每块 tile 精确多大"，
  后者是静态资产那套"内容占画布多少"，不是一回事。

`validate` 对 tile 跑两条判据，各抓一种平铺失败 —— 它们是这条链存在的意义：

| 判据 | 抓什么 | 平铺后看到什么 |
|---|---|---|
| `tile_seam` | 对边接不上（如整幅左右渐变） | 每隔一格一道突变 |
| `tile_border` | 带边框 / 暗角 | 一片规则网格线 |

第二条不能省：带边框的 tile 接缝处是"边框接边框"，两边一样暗，**接缝判据对它
恒判通过**，而它恰恰是模型最常见的失败形态。两条都是 fatal —— 拼不起来等于整套
不可用。`tile_border` 阈值已用第一批真实样本修正过一次（2.0→4.0，n=3 仍不算
校准完成，见[校准记录](docs/threshold-calibration.md)）。

`export` 产出的 contact sheet 会把每块 tile 铺成 3×3：判据只算数值，
平铺起来像不像、有没有肉眼可见的重复图案，还得人看。

#### 邻接表：哪块能挨着哪块

`create-tileset` 顺带从像素推出一张邻接表，写进 Manifest 并随 `generic-json`
导出，给地图生成当输入。**不额外调用 API**。

判据同样是两条，理由与上面那对同源 —— 接缝比的分母是纹理颗粒度，而**噪声一大它
就会把"材质换了"这件事稀释掉**：实测草接水在颗粒度 60 时接缝比只有 1.88（阈值 3），
单靠它会判成相容。所以第二条 `edge_color_gap` 比的是两侧边缘的**均值之差**，
不做任何归一化，噪声抬不高它。

```
adjacency.right = {"grass_base": ["grass_base"], "dirt_path": ["dirt_path"], ...}
```

`examples/grass_field.yaml` 推出来是**对角矩阵**，这是正确答案不是退化：草、土、水
本来就不能直接挨着，中间需要过渡 tile —— 而那类 tile 目前还没有。这张表如实说出了
"想把草和水放一起，你还缺一类 tile"。

Manifest 只存 `right` / `down` 两个方向（`A 右接 B` 与 `B 右接 A` 是两件事，
只有 `A 右接 B ⟺ B 左接 A` 才是同一件事），导出的 JSON 给全四个方向省得每个
消费者各写一遍转置。`validate` 的 `tile_adjacency` 会拿当前像素重算比对 ——
产出之后有人换过 tile 图，它会喊停。

> Godot 的 terrain / peering bits **还没做**：那要求 tileset 里本来就有
> edge / corner / transition 那几类 tile，眼下一块都没有，硬填只能靠猜。

#### 铺一张地图

```bash
uv run pixel-asset create-map outputs/grass_field --width 24 --height 16 --seed 42
```

WFC 的 Simple Tiled Model，吃的就是上面那张邻接表。**不调用 API**，
同 seed + 同邻接表 + 同尺寸 → 同一张地图，逐格相等。

`validate` 的 `map_adjacency` 会逐对相邻格核对合法性（水平与垂直分开查——
只查一个方向的检查对另一半失败恒判通过）。撞上矛盾时求解器**换 seed 重试，
仍然不行就报错**，绝不交一张有非法接缝的半成品。

> **对现在这套 tile，铺出来的地图只有一种材质，而这是正确结果。** 邻接表是
> 对角矩阵（材质之间接不上），而地图网格是连通的——每一步都要求两边相容，
> 于是整张图必然同一种材质。缺的是**过渡 tile**，不是更好的求解器。
> 多材质求解能力由 `tests/unit/test_wfc.py` 用合成邻接表验证。

地图随 `generic-json` 导出（`maps[*].rows` 是逐行 tile_id，查 `tiles[*]` 即得图集
坐标）。Godot 那边给的是一段 `set_cell()` 的 GDScript——**没有产原生 `.tscn`**：
`TileMapLayer` 把地图存成打包字节数组，本机无 Godot 可验，凭记忆拼二进制不如给
一段读一遍就能确认对错的代码。

#### Tiled 导出

```bash
uv run pixel-asset export outputs/grass_field -t tiled
```

产出 `.tsx` / `.tsj`（外部 tileset）与每张地图的 `.tmx` / `.tmj`。

**"能打开"不是判据。** Tiled 的地图是一串 GID（`firstgid + 行主序局部 id`，
`0` 表示空格），而这条链上每一步都能悄悄写错、**写错的文件照样能打开**：

| 写错什么 | Tiled 打开时 | 实际后果 |
|---|---|---|
| `firstgid` 差 1 | 正常打开 | 整张地图错位一格 |
| 行主序写成列主序 | 正常打开 | 非方形图集上 tile 全乱 |
| CSV 按列输出 | 正常打开 | 地图被转置 |

所以判据是**往回解**：把写出去的文件读回来，GID → 局部 id → 图集格坐标 → tile_id，
逐格与源地图比对。测试里还各配了一个反例，证明这条判据真的抓得住那两种错。

地图数据用 `csv` 编码而非 base64+zlib——本机没有 Tiled 可验，压缩过的字节流写错了
肉眼看不出，CSV 读一眼就知道对不对。

> **这些文件没有被 Tiled 打开验证过。** 保证止于"结构符合文档所述、GID 能往回解
> 回原 tile"，不等于 Tiled 一定能正常渲染。与 Godot TileSet 那笔欠账并列。

`plan` 完全离线：它自动识别单资产 request 或 pack，输出任务 DAG、预计 API 调用次数、
键控色冲突预检结果与风险告警，不生成任何图。**大批量生成之前先跑它。**

```
共 9 个任务 · 预计 API 调用 7 次 · 镜像派生 2 个（不计费）
! 键控色由 #FF00FF 降级为 #00FF00（alt_key_color；冲突词：magenta、violet）
```

## 命令

| 命令 | 用途 | 调用 API | 状态 |
|---|---|:---:|:---:|
| `init` | 初始化配置与目录 | ❌ | ✅ |
| `doctor` | 检测配置、依赖、Key、网格档位 | 仅探测 | ✅ |
| `plan <input.yaml>` | 自动识别单资产或 pack，输出 DAG 与调用预算 | ❌ | ✅ |
| `process <outputs/A>` | 仅重跑本地处理链 | ❌ | ✅ |
| `create-character <request.yaml>` | 生成 canonical seed | ✅ | ✅ |
| `create-animation --asset A --action X --direction D` | 生成完整动作网格 | ✅ | ✅ |
| `create-asset <request.yaml>` | 单个静态资产完整链：生成 → 处理 → 验证 → 导出 | ✅ | ✅ |
| `create-asset-pack <pack.yaml>` | 批量生成共享约束的一组资产（静态三种 + 动画 `spell_bundle` / `combat_bundle`） | ✅ | ✅ |
| `create-tileset <request.yaml>` | 生成一整套地面 tile：逐块生成 → 整套统一处理 | ✅ | ✅ |
| `create-map <outputs/A> --width W --height H --seed N` | 按邻接表铺一张地图（WFC） | ❌ | ✅ |
| `import <request.yaml> <source> --as seed\|keyframes` | 导入已有素材 | ❌ | ✅ |
| `interpolate <outputs/A> --key X --target-fps N` | 生成式补间 | ✅ | ✅ |
| `validate <outputs/A>` | 运行验证引擎 | ❌ | ✅ |
| `repair <outputs/A>` | 执行修复计划 | 视类型 | ✅ |
| `export <asset-dir-or-id> -t godot\|generic-json\|tiled` | 按目录或 `asset_id` 导出 + Contact Sheet | ❌ | ✅ |

命令面按完整业务动作演进，不机械固定数量；MCP 仍保持少量高层语义工具。

### MCP

装了 `mcp` 可选依赖后（`uv sync --extra mcp`）可作为 MCP server 跑：

```bash
python -m pixel_asset_forge.mcp_server     # stdio
```

**只暴露 6 个工具**：`create_character` · `create_animation` · `create_asset_pack` ·
`validate_asset` · `repair_asset` · `export_asset`。

这不是"至少这些"，是**恰好这些**，且有测试钉着（加第 7 个会红）。理由见
[ADR-005](docs/adr/ADR-005-cli-core-mcp-adapter.md)：工具越多，上下文开销越大、
选错概率越高，而最关键的是**顺序错误**——像素处理有严格顺序依赖（despill 必须在
量化前），让模型编排这个顺序等于把确定性流程交给不确定性组件，那会在架构层面
推翻整个项目的立论。

`init` / `doctor` / `plan` / `import` / `process` 刻意**不**暴露：它们是开发者
工作流入口，对模型没有语义价值。

工具返回的是**摘要**（asset_id、状态、产物路径、检查项统计）而不是 Manifest 全文
——MCP 的返回直接进模型上下文，一个带 24×16 地图的 tileset Manifest 有几千个
tile id。返回体积有明确上界并有测试守着。

`plan`、`process`、`validate`、`export` 等离线入口让调试与迭代尽量不重复调用 API。

`--model` 可在命令行覆盖有效生成模型（`plan` / `create-asset` / `create-asset-pack`），
优先级为**命令行覆盖 > 环境变量 > 项目级 YAML > 用户级 YAML > 内置默认值**。
规划指纹包含有效模型：`plan --save` 与 `create-asset-pack` 必须用同一个 `--model`，
否则会因指纹不一致被拒绝执行。`create-asset-pack --retry-failed` 把任务表中处于
failed 的资产复位到最近一个可确认检查点后重跑，其余资产不受影响。

## 示例请求

[`examples/`](examples/) 里的三个角色/特效用例各覆盖一类风险（pack 示例见上）：

| 文件 | 覆盖风险 |
|---|---|
| `knight.yaml` | 持剑非对称 → 不可镜像，四方向独立生成 |
| `slime.yaml` | 粉紫色角色与默认键控色撞色 → 自动降级到 `#00FF00` |
| `fireball.yaml` | 非角色资产 · 无方向动作 · 非循环动作 |

## 开发

```bash
uv run pytest          # 全部离线，不触网
uv run ruff check .
uv run mypy pixel_asset_forge
```

真实 API 测试默认关闭，需显式 `RUN_LIVE_IMAGE_TESTS=1` 开启。

### 已实现的关键不变量

这些是有测试覆盖、不允许退化的性质：

- **任务 ID 确定性** —— 同一请求重复 `plan` 不创建重复任务，已完成的任务不被打回。
- **原始生成图永不覆盖** —— 重生成前必须先 `archive_source`，否则报错。
  这是 `process` 能离线重跑的前提。
- **非法状态转移必须报错** —— 例如不能跳过 `validate` 直接 `export`。
- **`passed` 由检查项推导** —— 验证报告不接受外部把结论直接设为 True。
- **API Key 结构性隔离** —— `Config` 里没有放 Key 的字段；配置文件出现 Key 直接报错；
  所有日志与错误信息强制经过脱敏。
- **网格档位合规** —— 5 个帧数档位逐条校验 gpt-image-2 的四条尺寸约束。
- **按连通域抽帧，不按格线硬切** —— 帧数已知时，问题是"定位 N 个已知目标"而非
  "推断有几帧"。格线硬切会把一张**完全正常**的产出判成 fatal
  （[ADR-003 修订 2](docs/adr/ADR-003-fixed-grid.md)）。
- **共用视口按网格行计算** —— 跨行取公共上下界会让视口高达两行之和，
  角色缩到 32×32 后只占画布 47%，白白丢掉一半分辨率。
- **重生成必须绕开缓存** —— 「重试失败的调用」时缓存是朋友，
  「因产出不合格而重生成」时命中缓存会原样返回那张不合格的图，修复永远不可能成功。
- **验证失败绝不放行** —— `validate` 存在 fatal/high 失败时退出码为 3。
- **Manifest 1.x 可自动迁移到 2.0** —— `fallback_stage` 由序号改具名后，
  旧文件读取时就地升级，不会被静默误读。
- **`process` 幂等** —— 同一份 `source/` 重跑任意次，产出字节完全一致。
  这要求色键阈值**逐图**记录：按资产存一个会让除第一张外的所有图在重跑时改变结果。
- **最近邻缩放不引入中间色** —— 插值会把 24 色调色板打成上千色。
- **跨动作缩放一致** —— 逐动作各自填满画布会把真实的姿势尺寸差异 normalize 掉
  （实测源图差 40% 体型、输出一样高）。参考动作确立无量纲基准，其余动作复用。
- **暖色不被 despill 摧毁** —— 只有键控色的所有满值通道同时超标才算溢色；
  只看单通道会把褐色/肤色/红色全压成橄榄绿，而所有数值检查照样通过。

### Golden image 测试

```bash
uv run pytest tests/golden                       # 回归检查
REGENERATE_GOLDEN=1 uv run pytest tests/golden   # 重新生成基准（需人工复核）
```

几何链（纯整数运算）断言字节级一致；含 Pillow 量化的完整链按 PLAN §10.2 用容差 ——
严格相等会在依赖升级时大面积假失败。**golden 只覆盖处理层，覆盖不了生成层。**

## 许可

MIT
