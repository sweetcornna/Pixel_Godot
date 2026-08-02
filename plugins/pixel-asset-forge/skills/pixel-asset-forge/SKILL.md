---
name: pixel-asset-forge
description: 生成游戏用像素资产 —— 角色标准图、四方向动画（idle/walk/attack/hurt/death）、道具、武器、技能特效与环境物件，并自动完成去背景、切帧、缩放、脚底对齐、调色板量化、质量验证与引擎导出（Godot / Generic JSON）。当用户要求"做一个像素角色"、"生成 walk 动画"、"帮我出一套 sprite"、"导出到 Godot"、"资产有问题帮我修"时使用。需要用户自己的 OpenAI API Key。
---

# Pixel Asset Forge

把自然语言描述编译为可直接导入游戏引擎的像素资产。

**你的职责**：理解用户意图 → 补全参数 → 生成 Asset Request → 调用稳定的 CLI 命令 → 解读结果。

**不是你的职责**：编排像素处理步骤、自己写图像处理代码、自由组合底层脚本。

---

## 安装与前置条件

本 Skill 是 `pixel-asset` CLI 的薄适配层（ADR-005）。**先确认 CLI 可用再往下走：**

```bash
pixel-asset doctor
```

`doctor` 会逐项报告配置、API Key、网格档位与阈值校准状态。命令不存在时：

```bash
uv sync            # 或 pip install -e .
```

**API Key 只从环境变量读取**，绝不写进配置文件、request、Manifest 或日志：

```bash
export PIXEL_ASSET_API_KEY="…"      # 也接受 OPENAI_API_KEY
```

用户没设 Key 时**直接告诉用户去设**，不要试图从别处找、更不要写进任何文件。

---

## 不可违背的规则

这些规则是系统正确性的前提，任何情况下都不要绕过：

1. **绝不逐帧生成动画。** 永远用 `create-animation` 一次生成完整动作网格。
   逐帧生成会导致服装、武器、比例和朝向漂移。

2. **动画必须基于已存在的 canonical seed。** 没有 seed 就先跑 `create-character`。
   不要试图跳过种子图直接生成动画。

3. **API 返回成功 ≠ 资产合格。** 生成之后**必须**跑 `validate`。
   模型经常产出帧数错误、姿势跨格、身份漂移的图，这些只有验证器能发现。

4. **大批量生成之前先跑 `plan`。** 它会自动识别单资产 request 或 pack，输出任务 DAG
   与预计调用次数，让用户在执行前看清楚要生成什么。不要在用户不知情的情况下发起批量生成。

5. **pack 中绝不写 `model`。** 生成模型统一来自 Config；当前默认是 `gpt-image-2`，
   需要覆盖时沿用既有优先级「CLI > 环境变量 > 项目配置 > 用户配置 > 内置默认值」，
   不把运行配置混进业务输入。

6. **能离线解决的就离线解决。** 命令里有一半不调用 API。
   处理逻辑或验证阈值的问题用 `process` / `validate` 重跑，不要重新生成。
   重复请求会命中 prompt hash 缓存，所以重跑失败任务是安全的。

7. **用户上传素材时，先问清意图再动手。** 同一批文件对应两种完全不同的处理，
   光看文件数量分不出来。问法见下面「用户上传素材」一节。
   `import` 不给 `--as` 会直接报错 —— 那是最后一道防线，不是让你去试的。

---

## 意图 → 命令映射

| 用户说 | 执行 |
|---|---|
| "做一个像素骑士 / 生成角色" | 写 request YAML → `plan` → `create-character` |
| "给他加个走路动画" | `create-animation --asset X --action walk --direction down` |
| "四个方向都要" | 对每个 direction 各跑一次 `create-animation` |
| "五种动作都要" | idle / walk / attack / hurt / death 逐一执行 |
| "做一批药水 / 武器 / 场景物件" | 写对应静态 pack YAML（不写 `model`）→ `plan pack.yaml` → 用户确认 → `create-asset-pack pack.yaml` → 逐 `asset_id` 审核 / 导出 |
| "做一组法术特效" | 写 `spell_bundle` YAML → 同上，但要跑**两遍**：第一遍停 seed 闸门，逐个批准后重跑同一条命令 |
| "给这个角色做整套战斗动作" | 写 `combat_bundle` YAML（`attack` / `hurt` / `death`）→ 同 `spell_bundle` 的两遍流程 |
| "续跑刚才失败的药水" | 用同一份 pack YAML 再跑 `create-asset-pack pack.yaml`；已完成资产去重，只续跑未完成/可重试资产 |
| "帮我看看有没有问题" | `validate outputs/X` |
| "颜色不对 / 背景没抠干净 / 位置歪了" | 先 `process`（离线重跑），再 `validate` |
| "这个动作重做一下" | `repair outputs/X/<action>_<direction>` |
| "导出到 Godot" | `export outputs/X --target godot` |
| "帮我把它接进场景 / 在 Godot 里用起来" | 先 `export -t godot`，再照 `references/godot-handoff.md` 用 godot-ai 的 MCP 建节点 |
| "补帧 / 补到正常帧率" | `import ... --as keyframes` → `interpolate --key X --target-fps N` |
| "先看看要生成哪些 / 有多少任务" | `plan request.yaml` |
| "环境配好了吗 / 报错了" | `doctor` |
| **用户上传了图片** | **先问意图**，见下节 |

---

## 标准工作流

```text
1. plan             ← 先看清楚要跑哪些任务
2. create-character ← 产出 canonical seed
3. 【人工闸门】      ← 让用户看 seed，确认再往下
4. create-animation ← 逐个 (action, direction) 生成
5. validate         ← 必做
6. repair           ← 仅在验证失败时
7. export           ← 交付
```

**第 3 步的人工闸门不要自作主张跳过。** seed 是所有动画的身份基准，
seed 不对则后续生成的全部动画都要作废重来。把 seed 图展示给用户，等确认。

**批量生成多个角色时，先完整跑通一个。** 确认质量达标后再批量执行 ——
风格与调色板的问题在第一个角色上就会暴露，不要等到二十个角色都生成完才发现。

### 批量资产：五种 pack

用户要「一批 X」时，不要逐个写单资产 request，也不要用 shell 循环拼装批次。
写一份 pack YAML，`pack_type` 决定展开成哪种资产：

| `pack_type` | 展开成 | 动画 |
|---|---|:---:|
| `potion_pack` | `pickup` | — |
| `weapon_pack` | `weapon` | — |
| `environment_pack` | `environment_object` | — |
| `spell_bundle` | `spell` | ✅ |
| `combat_bundle` | `character`（同一角色的多个战斗动作） | ✅ |

无论哪种：

- `shared` 中统一写 `style`、`background`、`export` 与显式 `palette.colors`
- `assets` 中每项只写唯一 `asset_id` 与具体描述
- **不要写 `model`**；模型统一从 Config 读取
- 动画 bundle 还要写 `shared.animations`（整包共用的动作集）；静态 pack **必须省略**它

静态 pack 一条命令跑完：

```bash
pixel-asset plan potions.yaml --save
pixel-asset create-asset-pack potions.yaml
pixel-asset export outputs/health_potion --target godot
pixel-asset export mana_potion --target godot
```

**动画 bundle 要跑两遍 —— 不要以为第一遍没跑完是出错了。** 第一遍只生成各资产的
canonical seed 并停在 `awaiting_approval`，这正是第 3 步的人工闸门：把 seed 图
（或 contact sheet）展示给用户，逐个 `--approve-seed` 批准后**重跑同一条命令**续跑：

```bash
pixel-asset plan combat.yaml --save
pixel-asset create-asset-pack combat.yaml                      # 停在 seed 闸门
pixel-asset create-animation --asset knight_01 \
    --action attack --direction down --approve-seed            # 用户看过图再批准
pixel-asset create-asset-pack combat.yaml                      # 跑完全部动作
```

等待批准**不是失败**，不要当成错误去重试或改 `asset_id`。`plan` 会分列 seed 与
动画的调用数 —— 动画 bundle 不是「资产数 = 调用数」，报成本时按分列的数字说。

`plan` 之后先把资产列表、预计调用次数和共享色板告诉用户，得到批量执行确认再运行。
生成结束后解读 `pack-summary`，列出成功、失败、跳过/去重的 `asset_id`；再按每个
`asset_id` 查看其独立 Manifest / artifacts，逐项给用户审核并按目录或 `asset_id` 导出。
不要把「整包命令完成」当成「每件资产都已审核」。

批次中的单资产失败与其余资产隔离。失败或协作暂停后，使用**同一份 pack YAML**
重跑 `create-asset-pack`：输入去重会保留已完成资产，只续跑未完成或可重试资产；
不要拆掉 pack 逐个重建，也不要为了续跑修改 `asset_id`。

动画 bundle 跑完后，若日志或 `pack-summary` 里出现「因基准顶替重跑了处理」，
那是协调器自动做的**本地**统一处理（零 API 调用），不是错误，也不需要重跑生成 ——
如实告诉用户图被重新处理过即可。

---

## 接进 Godot

本 plugin 只负责"资产从哪来"。用户拿到 `.tres` 之后还要建场景、挂节点、
连信号 —— 那一段用 **godot-ai** 的 MCP 工具做（需要用户也装了 `godot-ai`
plugin 并在 Godot 里启用 addon）。

**四条必设项写在 `references/godot-handoff.md`**，每一条不设都会让接进去的
节点"看着能用、实际是坏的"：纹理 Filter 设 Nearest、`offset` 设
`(0, -canvas_height/2)`、按 `loop` 决定是否连 `animation_finished`、
`.tres` 与 png 整目录复制。

godot-ai 不看我们的 Manifest，也不知道我们的锚点约定 —— 这四条只能由这一侧提供。

---

## 用户上传素材

用户丢过来图片时，**先问，不要猜**。同一批文件对应两种意图：

| 意图 | 产出 | 命令 |
|---|---|---|
| **A. 静态图 → 动态资产** | 用它当身份基准，生成整套动作 | `import ... --as seed` |
| **B. 关键帧 → 补到正常帧率** | 原帧原样保留，补出中间帧 | `import ... --as keyframes` |

### 为什么必须问

**文件数量分不出来。** 三张图可能是「笑 / 平 / 晕」三个独立状态（意图 A 的变体），
也可能是一段动作的三个关键帧（意图 B）。

**猜错的代价不对称：**

- 把关键帧当 seed → 白花整套动作的生成调用，而且用户原本要保留的帧全丢了
- 把变体当关键帧 → 补出一堆「半笑半晕」的中间帧，全是废的

### 怎么问

给用户看你对这批图的观察，再给两个选项。**不要只抛问题，先说你看到了什么** ——
用户往往一眼就能纠正你的误解：

> 收到 3 张图，都是同一个角色（银发精灵，坐姿），表情分别是笑 / 平静 / 晕。
> 想确认一下要做哪种：
>
> **A. 当角色基准，生成整套动作** —— 我拿其中一张当身份基准，
> 生成 idle / walk / attack 等动作。你的原图不进最终帧序列。
>
> **B. 当关键帧，补到正常帧率** —— 这三张原样保留，我在它们之间补出中间帧。
> 需要你告诉我原本的帧率和想补到多少（比如 3fps → 9fps）。

判断倾向（**只是倾向，仍然要问**）：

- 表情/装备/配色不同，姿势基本一致 → 多半是变体，倾向 A
- 姿势/肢体位置有连续变化，其余一致 → 多半是关键帧，倾向 B
- 只有一张 → 只能是 A，但仍要确认用户想要哪些动作

### 意图 B 还要问什么

补间预算要这三个数才算得出来，缺哪个就问哪个：

- **原帧率**（`--source-fps`）：这几张关键帧本身是按多少帧每秒画的
- **目标帧率**（`--target-fps`）或**目标帧数**（`--target-frames`）
- **是否循环**（`--loop` / `--one-shot`）：走路是循环，倒地不是

循环与否会改变间隔数：循环动作末帧要接回首帧，间隔数等于关键帧数；
一次性动作少一个。算错就是多花或少花一次生成调用。

---

## 参数补全

用户很少会给全参数。按下表补默认值，并在回复中**明确告诉用户你补了什么**：

| 字段 | 默认值 | 何时需要问用户 |
|---|---|---|
| `style.target_size` | `[32, 32]` | 用户提到"高清"、"大一点"、"Boss" |
| `style.max_colors` | 24（角色）/ 12（特效） | 用户提到具体调色板 |
| `style.perspective` | `top_down_3_4` | 用户提到"横版"、"侧视"、"2.5D" |
| `style.lighting` | `fixed_top_left` | — |
| `background.color` | `#FF00FF` | **角色是粉/紫/洋红系时必须提醒**（见下） |
| `mirroring.enabled` | **必须显式判断，不要默认** | 见下 |
| `animations[].frames` | idle=4, walk=8, attack=6, hurt=4, death=8 | 用户指定帧数 |
| `animations[].fps` | idle=6, walk=10, attack=12, hurt=8, death=8 | — |
| `export.targets` | `[generic-json, godot]` | 用户提到其他引擎 |

`frames` 只能取 `4 / 6 / 8 / 9 / 12` —— 这些是能映射到合规网格布局的档位。
用户要 5 帧或 7 帧时，向上取到最近档位并说明原因。

### 两个需要你主动判断的字段

**`background.color` —— 撞色检查**

如果角色描述里出现粉色、紫色、洋红、品红、magenta、violet、pink 等词，
默认键控色会把角色本体一起抠掉。做法：在 request 里填 `conflict_hint`，
系统会自动降级到备用色。同时告诉用户你做了这个处理。

**`mirroring.enabled` —— 不要猜**

只有**同时**满足以下四条才可以设 `true`：

- 没有固定手持武器
- 没有盾牌
- 没有单侧饰品
- 服装左右基本对称

只要有一条不确定，就设 `false` 并说明理由。
误判的代价是角色被镜像成左撇子，**而且这个错误能通过全部自动验证项** ——
几何检查对左右手完全不敏感，只有人眼能发现。

启用镜像能保证左右方向的身份完全一致（同一张图翻转，不存在漂移），
但判断错了就是左撇子。不确定时问用户。

---

## 解读验证结果

`validate` 输出 `validation-report.json`。按 `severity` 决定下一步：

| severity | 含义 | 动作 |
|---|---|---|
| `fatal` | 资产不可用 | 必须修复，不要交付 |
| `high` | 严重问题 | 修复；确实无法修复时明确告知用户 |
| `medium` | 质量瑕疵 | 提醒用户，由用户决定 |
| `low` | 提示 | 记录即可 |

常见失败项与应对：

| 检查项 | 含义 | 应对 |
|---|---|---|
| `frame_count` | 模型没画出要求的帧数 | `repair` → 重生成网格 |
| `cell_overflow` | 姿势跨越格线，肢体被切断 | `repair` → 重生成网格。**本地无法修复** |
| `frame_order_continuity` | 帧序可能乱了 | `repair` → 重生成网格。这是"静默失败"，值得重视 |
| `transparent_rgb_residue` | 透明像素 RGB 未清零 | `process` 重跑即可（**本地**） |
| `palette_overflow` | 颜色数超限 | `process` 重跑即可（**本地**） |
| `anchor_drift` | 脚底没对齐 | `process` 重跑即可（**本地**） |

**先判断是不是本地能修的。** 上表后三项跑 `process` 就行，不需要重新生成。

> ⚠️ 若报告中 `thresholds_calibrated: false`，说明验证阈值尚未用真实数据校准，
> 中低严重度的告警可能是误报。此时以人眼判断为准。

---

## 错误处理

| 错误 | 含义 | 应对 |
|---|---|---|
| 429 / 5xx | 瞬态错误 | 系统已自动退避重试，不要手动重跑 |
| 参数错误 | 请求不合法 | 修正 request YAML，**不要重试原请求** |
| Moderation blocked | 内容被拦截 | 改写角色描述，避免暴力/血腥表述。告知用户原因 |
| 帧数/构图错误 | 生成质量问题 | 不是 API 错误。走 `validate` → `repair` |

API Key 从环境变量读取，**永远不要把 Key 写进 request 文件、日志或回复中**。
Key 相关问题一律用 `doctor` 排查。

---

## 不要做的事

- ❌ 不要自己写图像处理代码 —— 切帧、抠图、缩放、量化全部由 CLI 完成
- ❌ 不要逐帧调用生成接口
- ❌ 不要跳过 `validate` 直接交付
- ❌ 不要在 seed 未经确认时批量生成动画
- ❌ 不要跳过 `plan` 直接执行 pack
- ❌ 不要在 pack 中写 `model`
- ❌ 不要因单资产失败就重做整个 pack —— 用同一输入续跑
- ❌ 不要把动画 bundle 的 `awaiting_approval` 当成失败 —— 那是 seed 闸门，批准后重跑同一条命令
- ❌ 不要在参数错误时反复重试 —— 先修正请求
- ❌ 不要猜 `mirroring.enabled`
- ❌ 不要把生成失败当成 API 故障 —— 多数情况是模型画错了，走修复流程

---

## 参考

| 文件 | 内容 |
|---|---|
| `references/prompt-rules.md` | Prompt 编译规则与负面约束清单 |
| `references/animation-rules.md` | 动作节拍、视角差异、anchor sheet、单行条带 |
| `references/godot-handoff.md` | 导出之后怎么接进 Godot 场景（四条实测出来的必设项） |
| `docs/PLAN.md` | 完整系统设计 |
| `docs/adr/` | 关键架构决策及其理由 |
| `examples/` | knight（不可镜像）· slime（撞色）· fireball（非角色） |
