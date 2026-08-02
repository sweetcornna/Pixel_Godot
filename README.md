# Pixel Asset Forge

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
| 7 | 道具、特效与批量任务 | 🚧 静态家族已收官（`potion_pack` / `weapon_pack` / `environment_pack` + `create-asset`）；动画类 pack 未开工，Sprint 7 未完成 |

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

**3. Godot 加载未经真实工程验证。** PLAN §8 Sprint 6 的门槛写的是"用真实 Godot 工程验证，
不是理论上兼容" —— 我没有 Godot 环境。`.tres` 的结构（`load_steps` 计数、
`&"name"` StringName、`speed` 语义、`Rect2` 区域）都有测试覆盖，
但**真人在 Godot 里拖一次**仍是必需的。

---

## 安装

需要 Python 3.12+ 与 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync --all-extras
```

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

### 静态资产 Pack

一批共享约束的静态资产使用 pack YAML：每个条目都是独立的无动画静态资产，共享
`style` / `background` / `export` 和显式 `palette.colors`。三种 pack **共用同一份**
[`schemas/asset-pack.schema.json`](schemas/asset-pack.schema.json)，
展开成哪种资产类型由 `pack_type` 映射表决定：

| `pack_type` | 展开的资产类型 | 示例 |
|---|---|---|
| `potion_pack` | `pickup` | [`examples/potion_pack.yaml`](examples/potion_pack.yaml) |
| `weapon_pack` | `weapon` | [`examples/weapon_pack.yaml`](examples/weapon_pack.yaml) |
| `environment_pack` | `environment_object` | [`examples/environment_pack.yaml`](examples/environment_pack.yaml) |

加一种静态 pack 只需在映射表里加一行。pack 中不要写 `model`；pack 不选择模型；
运行时使用 Config 解析后的有效 `model`（当前默认 `gpt-image-2`），
仍可按既有配置优先级覆盖。单资产失败不会取消其余资产，同一 pack 可恢复续跑；
完成后按 `asset_id` 逐项审核和导出。

```bash
uv run pixel-asset plan examples/potion_pack.yaml --save        # 自动识别 pack，核对批次计划并落盘
uv run pixel-asset create-asset-pack examples/potion_pack.yaml  # 执行整包，无 seed/动画批准闸门
uv run pixel-asset create-asset-pack examples/potion_pack.yaml --retry-failed   # 只重试失败的资产
uv run pixel-asset export outputs/health_potion -t godot        # 按资产目录导出
uv run pixel-asset export mana_potion -t godot                  # 或按 asset_id 导出
```

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
> `examples/` 下目前只有角色示例与三份 pack 示例。

> `potion_pack` 与 `weapon_pack` 已完成，`environment_pack` 已实现、正在收口验收。
> 动画类 pack（`spell_bundle` / `combat_bundle`）尚未开工，整个 Sprint 7 未完成。

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
| `create-asset-pack <pack.yaml>` | 批量生成共享约束的静态资产（`pickup` / `weapon` / `environment_object`） | ✅ | ✅ |
| `import <request.yaml> <source> --as seed\|keyframes` | 导入已有素材 | ❌ | ✅ |
| `interpolate <outputs/A> --key X --target-fps N` | 生成式补间 | ✅ | ✅ |
| `validate <outputs/A>` | 运行验证引擎 | ❌ | ✅ |
| `repair <outputs/A>` | 执行修复计划 | 视类型 | ✅ |
| `export <asset-dir-or-id> -t godot` | 按目录或 `asset_id` 导出 + Contact Sheet | ❌ | ✅ |

命令面按完整业务动作演进，不机械固定数量；MCP 仍保持少量高层语义工具。
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
