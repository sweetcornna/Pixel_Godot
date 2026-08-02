# Sprint 7 批量资产包 · 交接文档

- **日期**：2026-08-01
- **来源**：WSL 侧 Claude Code 会话（主模型编排 + codex 执行，共 5 轮派工），会话进程在
  第 5 轮验收途中退出，7.3 的产出留在工作区未提交。
- **本文档目的**：让接手的人不必读会话记录，就能知道「已经做完什么、当前工作区是什么状态、
  下一步按什么顺序做、哪些决策需要人来拍板」。
- **本文档里的验证数字，是在写文档时于本机重新跑出来的**，不是转述会话结论，见 §3.3。

---

## 1. 背景：这条线在做什么

Sprint 7 要交付「批量资产包」（`potion_pack` / `weapon_pack` / `spell_bundle` /
`combat_bundle` / `environment_pack`）。

采用的推进纪律是 **一个 pack 一个纵切**，每一切都遵守同一套流程：

1. 先在 `docs/PLAN.md` 把本次范围的**契约**（输入 / 命令 / 执行产物 / 退出门槛）写死；
2. 再实现；
3. 实现方（codex）报告完成后，**编排方在宿主机独立复跑**目标测试 + 全套件，并抽查关键 diff；
4. 用 mock provider 做**真实 CLI 全链路实测**（不只是跑测试）；
5. 打勾 PLAN 的退出门槛 → 提交。

**重要口径**（PLAN 里反复写明，请接手人继续遵守）：每一切只对**本次范围**主张完成，
`Sprint 7 总退出门槛`（PLAN.md:1241）那五条**继续保持不打勾**，直到全部 pack 类型落地。
不要因为某一切全绿就去勾总门槛。

---

## 2. 已完成并已提交的工作（3 个提交）

`main` 目前领先 `origin/main` **5 个提交**（含会话之前的 2 个），**均未推送**。
远端是 `https://github.com/sweetcornna/Pixel_Godot.git`。

| 提交 | 内容 | 规模 |
|---|---|---|
| `efdb1ac` | 7.1 `potion_pack` 首纵切（主体 + 两轮修复） | 55 文件 +3332/−138 |
| `cecd28a` | 7.1 收尾（`--model` 覆盖 + 审计盲区测试） | 8 文件 +430/−12 |
| `e1a1697` | 7.2 `weapon_pack` 第二纵切（pack 基建去 potion 化） | 21 文件 +356/−55 |

### 2.1 `efdb1ac` — 7.1 `potion_pack` 首纵切

接手时 7.1 主体实现已在工作区，但带着 **8 个既有测试回归**。分两轮修完：

**第一轮（回归根因）**：pack 需要的「验证通过后才可导出」硬闸被**全局**应用到所有资产，
且 Manifest 版本被全局升到 2.1，破坏了旧的 round-trip 契约。修复把闸门收窄到
带 `static_image` 的静态资产（`pipelines/export.py:108` 附近），旧动画路径固定 2.0。

**并行做的只读契约审计**：把 PLAN §7.1 的契约拆成 10 项逐条核对，结论是
6 项完整、4 项有实质缺口。挖出的三个必修问题值得记住，因为它们都是「文档写了、代码没做」
这一类：

1. `export <asset_id>` 走默认 contact sheet（即 README 文档化的用法）**必崩** ——
   相对化基准用错，抛裸 `ValueError`。唯一那条测试恰好加了 `--no-contact-sheet` 把它掩掉了。
2. 契约里的「批量执行前必须先 plan」**零强制**，直接跑 `create-asset-pack` 就开始花钱。
3. 契约里的「重跑处理不重新生成」更糟 —— 通用 `process` 没有 static 分支，
   会把单图原件当成一个叫 "static" 的动作按帧网格切坏。

**第二轮（缺口修复）**：上述三项 + worker 汇总落盘异常导致 `queue.join()` 永久死锁的修复
+ 新增 `--retry-failed` 复位入口 + 补齐暂停/恢复集成测试（此前「可断点续跑」零测试覆盖）。

验证：全套件 774 passed / 5 skipped / 0 failed；CLI 全链路实测通过。

### 2.2 `cecd28a` — 7.1 收尾

- **`--model` 命令行覆盖**：`plan` 与 `create-asset-pack` 新增 `--model`，接入
  `load_config` 的 overrides。契约写的优先级「命令行覆盖 > 环境变量 > 项目 YAML >
  用户 YAML > 内置默认」这一级此前**够不着**，现在真正可达，并有测试验证与 plan
  指纹闸门的联动（同 model 过闸、异 model 命中可读的指纹不一致错误）。
  `--provider` 没加 —— 现有 CLI 没有这个惯例。
- **11 个盲区测试**：shared 的 `background`/`export` 注入断言、指纹冲突拒绝复用、
  `outcome_unknown` 不重试、缓存对账拒绝提交、`validation_failed`/`cached`/`resumed`
  计数、中断退出码 `128+signum`、验证失败退出码。
- **两处一致性修正**：summary 缺失结果不再静默少算，改为显式 `outcome_missing`
  占位条目；`shared.background` 的 Pydantic 层改为必填，与 JSON Schema 口径对齐。

验证：全套件 785 passed / 5 skipped / 0 failed。

### 2.3 `e1a1697` — 7.2 `weapon_pack`

核心是**泛化**，让后续每种静态 pack 只需加一行映射：

- `PotionPack` → `StaticAssetPack`；资产类型由 `PACK_ASSET_TYPES` 映射表决定
  （`potion_pack→pickup`、`weapon_pack→weapon`）。
- 静态放行集合在 **request 校验、planner、静态流水线、validation 四处**统一为
  `STATIC_ASSET_TYPES`，拒绝消息列出允许类型。
- JSON Schema 的 `pack_type` 从 `const` 改为 `enum`，两种 pack 共用一份 schema。
- 提示词按类型分流：**pickup 措辞一字未动**（不惊动既有断言），weapon 用独立措辞
  并加游戏图标惯例的对角朝向（刀尖/枪口朝右上）。

验证：全套件 793 passed / 5 skipped / 0 failed；CLI 实测 `starter_weapons` 三件武器全链路。

---

## 3. 进行中：7.3「静态家族收官」—— 代码在工作区，**未提交**

> ⚠️ **本节已过期（2026-08-01 当天晚些时候）**：这些改动已作为 `96c0d84` 提交，
> 并经 PR #1（`9b25abb`）合入 `main`。收口验收的进展与 §3.2 的复核结论见 §7。

契约见 `docs/PLAN.md:1303`（7.3 节）。范围是：`environment_pack` + 静态单资产类型补全
（`prop` / `ui_icon` / `environment_object`）+ 新增单资产 CLI 入口 `create-asset`。
**动画类 pack（`spell_bundle`、`combat_bundle`）明确不在本次范围。**

### 3.1 工作区改动清单（12 改 + 3 新，+274/−36）

| 文件 | 改了什么 |
|---|---|
| `pixel_asset_forge/models/pack.py` | 映射表加 `environment_pack → environment_object`；`PackType` 扩为三值 |
| `pixel_asset_forge/models/request.py` | `STATIC_ASSET_TYPES` 扩为 `{pickup, weapon, prop, ui_icon, environment_object}` |
| `schemas/asset-pack.schema.json` | `pack_type` enum 扩为三值 |
| `pixel_asset_forge/prompts/compiler.py` | 新增 prop / ui_icon / environment_object 的主语措辞；`ui_icon` 额外加一句 UI 图标惯例（正面平视、无地面接触与投影、剪影可读）。**pickup 与 weapon 措辞一字未动** |
| `pixel_asset_forge/pipelines/static_asset.py` | **抽出 `validate_and_export_static_asset()`**（+ `StaticAssetCompletion`），供单资产入口与 pack 协调器共用 |
| `pixel_asset_forge/pipelines/asset_pack.py` | `_run_one` 里的「验证 → 导出」段落改为调用上面抽出的函数（复用而非复制，避免两处漂移） |
| `pixel_asset_forge/cli.py` | 新增 `create-asset <request.yaml>` 命令：单静态资产完整链（生成 → 处理 → 验证 → 导出），带 `--config`/`--model`，**不设 plan 前置闸门**（与 `create-character` 同口径），动画请求一律拒收并指向 `create-character` |
| `examples/environment_pack.yaml`（新） | 3 件环境物件（木桶 / 苔石 / 街灯），显式 12 色色板 |
| `tests/integration/test_create_asset.py`（新） | prop 与 ui_icon 各一条端到端（参数化）；动画请求拒收（character 与带 animations 的静态各一）；验证失败退出码 |
| `tests/integration/test_environment_pack.py`（新） | environment_pack 走完 plan → create → 逐资产 export |
| `tests/integration/test_cli.py` | `create-asset` 帮助文本说明成本与不设 plan 闸门 |
| `tests/integration/test_static_pickup.py` | 静态流水线接受其余静态类型 |
| `tests/unit/test_prompts.py` | 每种支持类型主语措辞不同；`ui_icon` 只多加 UI 惯例这一句 |
| `tests/unit/test_pack_request.py` | environment_pack 映射 |

### 3.2 抽取重构带来的两处语义变化 —— **接手第一件事是确认它们是有意的**

`asset_pack._run_one` 原来内联的逻辑与新抽出的 `validate_and_export_static_asset()`
在两个边界上行为不同：

1. **原来**只在状态为 `PROCESSED` / `VALIDATING` 时跑验证；**现在**把
   `VALIDATION_FAILED` 也纳入重验范围。影响面是**续跑**：上次验证失败的资产，
   续跑时会重新验证（此前会被跳过）。看起来是改进，但它改变了 `--retry-failed`
   与断点续跑的既有语义，值得确认并补一条测试。
2. **原来**状态不在期望集合里就静默跳过导出；**现在**抛 `ProcessingError`。
   更严格，但如果有别的路径会带着中间态进来，就会从「静默」变成「报错」。

另外 `stop_requested: object | None` 用 `getattr(..., "is_set")` 鸭子类型探测，
`mypy` 过得去但类型信息丢了，可以考虑收成 `threading.Event | None`。

### 3.3 当前验证状态（2026-08-01 在本机复跑，非转述）

- **全套件**：`815 tests · 0 failures · 0 errors · 5 skipped`，退出码 0（junit 计数）。
  相比 7.2 提交时的 798 条，本次净增 17 条测试。
- **`ruff check .`**：All checks passed
- **`mypy pixel_asset_forge`**：Success，74 个源文件无问题
- **未做**：`environment_pack` 与 `create-asset` 的**真实 CLI 全链路实测**
  （前几切都做了，这次会话在这一步之前中断）。
- **未做**：PLAN 7.3 的三条退出门槛仍是 ⬜，7.3 标题仍是 🚧；改动未提交。

---

## 4. 接手后的任务清单（建议按此顺序）

### 4.1 P0 — 把 7.3 收口

1. **确认 §3.2 的两处语义变化**是有意为之；如果是，补测试固化；如果不是，改回原语义。
   这是唯一需要判断的地方，其余都是机械收尾。
2. **补做 CLI 全链路实测**（前几切的既定动作，别跳过 —— 7.1 的三个必修 bug
   里有两个是只跑测试发现不了的）。用 mock provider 配置，走：
   - `plan examples/environment_pack.yaml`（未规划执行应被拒）→ `plan --save`
     → `create-asset-pack` 三件全 exported → `export wooden_barrel`
     （**默认 contact sheet，别加 `--no-contact-sheet`**）→ `process` 静态重跑零 API
   - `create-asset` 各跑一次 `prop` 与 `ui_icon` 请求
   参照 `tests/integration/test_environment_pack.py` 与 `test_create_asset.py`
   里 mock provider 的配置写法。
3. **打勾 PLAN 7.3 三条退出门槛 + 补写「7.3 完成记录」**（格式照 7.1/7.2），
   把 7.3 标题从 🚧 改为 ✅。**Sprint 7 总门槛继续不打勾。**
4. **提交**（仓库惯例：中文提交信息，只提交不推送，等人确认再推）。

### 4.2 P1 — 文档已经落后于代码

以下都是本次会话没顾上的、确定存在的陈旧点：

- `README.md:118` 的命令表：`create-asset-pack` 仍写着「批量生成共享约束的静态
  **pickup**」、状态仍是「`potion_pack` 首纵切 🚧」；`export` 行的 `asset_id`
  仍标 🚧；`plan` 行同理。7.1/7.2 都已完成，这些标记该更新。
- `README.md` 与 `docs/PLAN.md:638` 的命令表里**都没有 `create-asset`**。
- `--model` 与 `--retry-failed` 两个选项**从未进过 README**。
- README §pack 段落的措辞仍只讲 `potion_pack`，可顺带提 `weapon_pack` /
  `environment_pack` 与共用 schema。

### 4.3 P2 — 推送

`main` 领先 `origin/main` 5 个提交且从未推送。推送前请确认这是期望的
（远端仓库名是 `Pixel_Godot`，与本项目名不同，别推错分支）。

---

## 5. 需要人拍板的设计决策：`spell_bundle`

静态家族收完之后，Sprint 7 只剩 `spell_bundle` 与 `combat_bundle`，
它们是**动画资产进 pack**。这里有一个真实冲突，不适合让实现方单方面决定：

> 动画资产有 **canonical seed 的人工批准闸门**，而 pack 的语义是**批量自动执行**。

候选路线（都需要产品口径确认）：

1. **批量跑到 seed 就暂停**，等人逐个批准后再恢复 —— 保留人审，但 pack 不再是
   「一条命令跑完」，且与现有 `PackRunControl` 的协作式暂停如何复用要设计。
2. **预批准**：pack 文件里显式声明 seed 免审（或先单独 `create-character`
   批准 seed，pack 只做动画扩展）—— 一条命令跑得完，但把人审责任前移了。
3. **动画 pack 不做**，Sprint 7 以静态家族收官，动画批量推到后续 Sprint。

在这个决定做出之前，不建议开工 `spell_bundle`。

---

## 6. 环境与协作方式（给接手人的实操须知）

- **仓库**：`D:\project\pixel_skill`（WSL 侧路径 `/mnt/d/project/pixel_skill`）。
- **虚拟环境只有 Linux 版**（`.venv/bin/`，Python 3.12.13，pytest 9.1.1），
  **Windows 侧直接跑不了测试**，必须进 WSL：

  ```powershell
  wsl.exe -d Ubuntu --cd /mnt/d/project/pixel_skill -- .venv/bin/python -m pytest -q
  ```

  全套件约 5 分钟（315s）。注意在 PowerShell 里 `$?`、`$@` 会被宿主 shell 抢先展开，
  取退出码时要转义或写进 bash 脚本。
- **WSL 稳定性**：本次会话期间 WSL 出现过 `Wsl/Service/E_UNEXPECTED`，
  所有 wsl 调用连续失败；`wsl.exe --shutdown` 后重新进入即恢复。遇到同样报错先想到这个。
- **git 身份**是**仓库级**配置的（不是全局），按历史提交作者对齐，别改成全局。
- **`.git/index.lock`**：7 月 30 日曾遗留一个陈旧锁挡住提交，已清理。再遇到时
  先确认没有 git 进程在跑再删。
- **换行符**：工作区是 CRLF、仓库是 LF，`git diff` 会刷一堆
  `LF will be replaced by CRLF` 警告，属正常噪音。
- **协作纪律**（沿用会话里的做法，事后看是有效的）：
  - 契约先写进 PLAN.md，再派实现；实现方按契约落点干活，不即兴扩范围。
  - 实现方报告完成 **不等于** 完成 —— 一定在宿主机独立复跑目标测试与全套件，
    并抽查关键 diff。7.1 那三个 bug 都是这一步捞出来的。
  - **不并行改同一批文件**。会话里审计代理已经报出 `cli.py` 的缺口，
    但因为 codex 正在改 `cli.py`，第二轮是等第一轮落地后才派的。

---

## 7. 收口更新（2026-08-01，macOS 侧接手）

接手环境已从 WSL 换到 macOS（`/Users/cornna/project/Pixel_Godot`），§6 的 WSL
实操须知不再适用。7.3 的工作区改动已作为 `96c0d84` 提交、经 PR #1（`9b25abb`）
合入 `main` 并推送 —— §2 开头「5 个提交均未推送」与 §4.3 也随之过期。

### 7.1 §3.2 两处语义变化的复核结论（独立代码分析，非转述）

**第 1 条的影响描述与代码事实不符。** 「上次验证失败的资产，续跑时会重新验证」
不会发生：pack 协调器 `_run_one` 在进入新函数**之前**就把 `VALIDATION_FAILED`
拦下直接返回（`asset_pack.py:353-365`，抽取前后一字未改），`--retry-failed`
也只复位 `FAILED` 不含 `VALIDATION_FAILED`（`asset_pack.py:187-188`）；
`create-asset` 路径则更早死在 `static_asset.py:269-272`。所以两个调用方都
带不进这个状态，**续跑语义没有变**。新集合与下游 `run_validation` 的候选集
（`pipelines/validation.py:26-33`）及状态机合法边（`models/job.py:139-141`）
一致，是共享函数的正确闭包，予以保留。

**第 2 条保留抛错语义。** 旧内联代码不只是静默跳过导出 —— `return` 在 `if` 外，
状态不符时会照样上报 `outcome="exported"`，是写进 `pack-summary.json` 的假成功。
新的 `ProcessingError` 会被 `_run_one` 兜底转成 `processing_failed` 并记录真实
job 状态。改回旧语义等于恢复一个假阳性上报点。

**第 3 条采纳。** `stop_requested` 实际传入方只有 `threading.Event`
（`asset_pack.py:100,414,429`）与 `None`（CLI 路径），无任何鸭子类型使用者，
收成 `threading.Event | None`。

两处新语义此前**零测试覆盖**（唯一触及处 `test_create_asset.py:161-164`
还把整个函数打了桩），固化测试列入收口批次补齐。

### 7.2 复核中顺带发现的问题（记入 backlog / 待拍板）

1. **`repair` 对静态资产是坏的（潜在 bug，非 7.3 引入）**：静态检查的 target
   是 `"static"`（`validation/engine.py:418`），`repair/executor.py:124` 用
   `JobKind.ANIMATION` 拼出的 job id 恰好等于静态 job 的 `asset_id:static`，
   `_advance_job` 会绕过状态机把静态 job 硬写成 `VALIDATION_FAILED`
   （`repair/executor.py:130-131`）；`--allow-api` 的 REGENERATE_GRID 分支
   还会对静态资产调 `create_animation`。
2. **`create-asset` 不可重入，与 pack 口径不一致（待拍板）**：对已 `exported`
   的资产再跑一次 `create-asset` 报「不能进入静态处理」，pack 对同样情况是
   `skipped`。
3. **pack 级 `validation_failed` 资产没有自动重验入口（待拍板，产品口径）**：
   `process` 修复后 job 仍是 `VALIDATION_FAILED`，pack 重跑与 `--retry-failed`
   都不放行；目前只能走单资产 `validate` → `export` 收尾。若要放行，改
   `asset_pack.py:353` 与 `_reset_failed_static_job`，**不要动 `static_asset.py`**。

CLI 全链路实测（7/7 通过，2026-08-01）另报三点非阻断观察，一并记录：

4. **静态 `process` 重跑后状态层不一致**：manifest 状态从 `exported` 回到
   `processed`，但 job-table 仍是 `exported`。产物与 `static_image` 均正确，
   不影响 7.3 结论，但与上面第 3 条同族，拍板时一起定口径。
5. **`pixel_grid.py:217` 的 NumPy `All-NaN slice` RuntimeWarning 泄漏到 stderr**
   （块网格探测遇全透明块时），pytest 警告汇总里也有同一条。
6. **处理层警告文案对静态资产不适配**：`environment_object` / `prop` / `ui_icon`
   的警告仍用「角色」「脸与武器」等措辞。
