# 静态资产家族审查报告 · 2026-08-01

- **审查对象**：`main @ 8cdf869`（Sprint 7.1–7.3 静态资产家族的终态代码与产物）。
- **审查标准**（用户口径）：以**最终生成的游戏资产质量**与**最终代码**为准，
  不以 diff 或文档主张为准。
- **两条线**：① 对抗性终态代码审查（codex，只读）；② 资产消费方立场的产物
  质量审查（opus，mock provider 全链实测 + PIL/NumPy 逐像素统计 + 缺陷注入探针）。
- 本机无 API Key，真实生成质量未审 —— mock 验证不了的 12 个环节见 §4，
  设 `PIXEL_ASSET_API_KEY` 后需补一轮小规模真实生成目测。

## 0. 结论速览

- 处理链与溯源链**扎实**：导出 PNG 全部达标（alpha 二值、透明 RGB 清零、调色板
  严格子集、最近邻无中间色）；「仅凭 Manifest + frames/ 重建导出」三重验证
  **逐字节成立**；跨会话产物哈希一致。
- 但**验证引擎对静态资产是一层假防线**（Q1），且存在一个**任意文件写入**级别的
  路径逃逸（C1，已修复）。
- 共 22 项：1 阻断（已修）· 10 高 · 5 中 · 6 低。

## 1. 阻断（已在本仓库修复）

### C1. Manifest 路径字段可逃逸资产目录 → 任意文件读写 ✅ 已修

`static_image.source_image` / `image`（以及 `animations.*.frames`、`sheets`）
是未约束字符串，消费方全部用 `资产根 / 路径` 拼接后直接读写；
`_process_static` 更是**先写盘（process.py:204）、后做 `relative_to` 检查
（:218）**。一份 `image: ../../x` 或绝对路径的 Manifest 会先覆盖目标文件，
后置检查拦不住。

**修复**：`models/manifest.py` 新增 `AssetRelativePath`（拒绝绝对路径、盘符、
`..` 段，POSIX 与 Windows 两套解析都过一遍），应用于全部四处路径字段；
`schemas/asset-manifest.schema.json` 同步加 `relative_path` $def（不加就是又一处
双入口口径分裂）。恶意 Manifest 现在**在加载即被拒**。回归测试
`tests/unit/test_manifest.py` 覆盖构造与加载两个入口 × 6 种逃逸形态。
schema 版本不升级：schema 描述本来就写着「相对 asset 根目录」，这是把既有
语义落成强制，不是改语义。

## 2. 高（修复队列，按优先级）

> **Q1–Q8 已于 2026-08-02 修复并独立复核。** 复核方式不是"测试通过"，
> 而是**重跑审查当初那四个全部漏过的缺陷注入探针**：
>
> | 探针 | 审查时 | 修复后 |
> |---|---|---|
> | 半透明 alpha（`antialiasing: false`） | passed | **fail** `partial_alpha` (40 px) |
> | 孤立像素 | passed | **fail** `isolated_pixel` (6) |
> | 256 色 / `max_colors=6` | passed | **fail** `palette_overflow` (1.0) |
> | 键控色残留 | passed | **fail** `key_color_residue` (0.1196) |
> | 干净产物对照 | — | pass，exit 0，无误报 |
>
> 复核中另得到一条 X4 的实测证据：篡改成品后 `validate` 因 job 处于
> `exported` 而**直接拒绝重跑**（「没有可验证任务」），系统压根不重验 ——
> 必须连同 job 状态一起复位才测得到。这正是 X4 描述的失效模式。

### 质量防线类

- **Q1. 静态验证 7 项里 6 项结构性恒真。** ✅ 已修
  <br>**问题**：`palette_membership` / `transparent_rgb_residue` / `frame_size` /
  `content_bounds` 分别被上游的 `snap_to_palette` / `zero_transparent_rgb`+`save_png` /
  `place_on_canvas` / `inner_size=target-2` 构造保证，唯一有判别力的是 `blank_frame`；
  四个缺陷注入探针全部漏过，「验证通过」只等于「文件在、哈希对、不是全透明」。
  <br>**修法**：新增三个对真实产出有判别力的检查项 ——
  `partial_alpha`（`antialiasing: false` 时不许有半透明像素，HIGH）、
  `isolated_pixel`（八邻域无邻居的噪点，MEDIUM）、
  `key_color_residue`（从原图重放键控，量化前后都查，>5% 即 FAIL，HIGH）。
  量化前也查是关键：有显式调色板时 `snap_to_palette` 会把洋红边**静默映射**成
  最近的调色板色，只查成品永远发现不了。
- **Q2. `max_colors` 静态路径全程无人校验。** ✅ 已修
  <br>**问题**：`palette_overflow` 只在动画路径跑，`palette_overflow_ratio`
  （PLAN §9.2 判据）生产代码零调用。
  <br>**修法**：静态路径接上该判据，越界像素率 ≤2% 且实际色数不得超过 `max_colors`。
- **Q3. 「主体被切掉」无自动判据。** ✅ 已修
  <br>**问题**：`cell_overflow` 依赖 grid+source_image，静态资产两者皆无；
  `detect_overflow` 结果在 `create_static_asset` 里被丢弃。这类错误恰是
  「本地补不回、必须重生成」的最该拦的一类。
  <br>**修法**：从静态原图重放量化前处理并调用 `detect_overflow`，
  原图主体触碰画布边缘即 FAIL（FATAL，触发重生成）。
- **Q4. skip 理由机制未落地静态路径。** ✅ 已修
  <br>**问题**：22 个 CheckId 静态只出现 7 个，其余静默消失，`summary.skipped: 0`
  会被消费方读成「没有跳过任何检查」—— 动画路径遵守了「防线缺失要显式记录」
  （PLAN §6.8.4），静态路径没有。
  <br>**修法**：静态报告覆盖全部 22 项，正常产物跑 12 项、显式跳过 10 项并标注
  `skip_reason`（动画专属用 `static_asset`，缺产物/溯源用 `dependency_failed`）。
- **Q5. 处理告警不落盘，批量路径连终端都不打。** ⬜ 未修
  <br>「94% 细节丢失」「网格未吸附」只进 stdout；`create-asset-pack` 的
  JSON/表格无 warnings 字段。

### 交付类

- **Q6. Contact sheet 背景与描边色撞色。** ✅ 已修
  <br>**问题**：背景 `#22222C` 与两个示例包的标准描边色 `#211A2C` 距离仅 8.06，
  wooden_barrel 42% / quest_marker 46% 的像素在审核图上不可见 ——
  而 contact sheet 是 README 定位的「唯一人工防线」。
  <br>**修法**：贴片区改棋盘格（`#96969E` / `#4A4A52`）。两色交替，任何单色都不可能与整片背景同时同色，
  顺带区分了「透明」与「深色实体」。实测同一张 wooden_barrel：那 42.3%
  的描边像素与两格距离变为 73.7 / 205.1。
- **Q7. Godot 交付知识只存在于终端文本。** ✅ 已修
  <br>**问题**：Filter=Nearest 等必设项只在终端打印，而 `create-asset` 与
  `create-asset-pack` 都不打印 exporter 的 notes，批量生产的人一次也看不到。
  <br>**修法**：导出目录内写 `GODOT-README.md`，四条必设项（纹理 Filter、
  `offset` 对齐脚底、一次性动作连 `animation_finished`、整目录复制）随目录交付。
  注：Godot 4 的纹理过滤由项目设置/节点属性决定，不再由 `.import` 控制，
  因此交付载体是说明文件而非 `.import`（审查原文的建议在这一点上不适用）。
- **Q8. `.tres` 的 `ext_resource` 是项目根绝对路径。** ✅ 已修
  <br>**问题**：`res://<asset_id>.png` 写死了「png 在项目根」，与导出器
  note「整目录复制进项目」矛盾 —— 照说明放进 `res://assets/` 即 Parse Error。
  <br>**修法**：改为相对 `.tres` 自身的路径（Godot 文本资源格式对相对路径同样
  合法），整目录放项目根或任意子目录都成立。**待 `tools/godot-gate/` 真机复验**
  （本机无 Godot 二进制）。

### 执行语义类（codex）

- **X1. 重叠 pack 并发无跨进程互斥**：任务表「读-改-写」无 CAS，两进程同跑同
  `asset_id` 会重复计费、互相覆盖状态（asset_pack.py:482 / cache.py:37）。
- **X2. 规划指纹不含 `base_url`**：换端点不换指纹，闸门放行；`PIPELINE_VERSION`
  固定 0.1.0 也使代码变更不失效旧计划（models/pack.py:152 / config.py:62）。
- **X3. `create_static_asset` 先 `save_request_copy` 后查指纹冲突**：冲突时
  request.yaml 已被新请求覆盖，溯源包自相矛盾（static_asset.py:158）。
- **X4. `validated/exported` 状态不与产物哈希绑定** ✅ 已修
  <br>**问题**：验证后替换 `frames/static.png` 再恢复，新文件未经验证即被导出；
  产物被删后 pack 仍报 `skipped`。
  <br>**修法**：job 新增 `validated_processed_hash`，把成功状态绑定到**实际验证过的**
  那份产物；跳过验证前同时核对文件存在、磁盘哈希、Manifest 哈希与验证哈希，
  对不上就重验。pack summary 用 `revalidated_exported` /
  `artifact_revalidation_failed` 显式反映。
  <br>**独立复核**：注入半透明缺陷并刷新 Manifest 哈希后 `export`，退出码 1、
  重验抓到 `partial_alpha`（修复前该命令因 job 是 exported 直接拒绝重验）。
- **X5. 静态导出硬闸以 `static_image is not None` 为条件** ✅ 已修
  <br>**问题**：Manifest 缺该字段反而**绕过**硬闸，可产出无图元数据 JSON 并标记 exported。
  <br>**修法**：硬闸改由 `asset_type` / `JobKind.STATIC` / 静态结构共同判定，
  静态资产缺 `static_image` 立即拒绝。
  <br>**独立复核**：复位到 validated 并删掉该字段后 `export` —— 明确报错、
  **不产出空元数据**、job 未被误标 exported（旧行为三条全反）。
- **X6. Provider 成功但图不可解析 → job 永卡 `generating`** ✅ 已修
  <br>**问题**：Pillow 异常不是 ProviderError，不写失败态、无检查点，
  `--retry-failed` 与续跑都救不回，且可能已计费。
  <br>**修法**：响应在 source/cache 落盘**前**先 `verify()`，坏字节转成带 request ID
  的 `provider_invalid_image` 非瞬态失败，job 写 `failed`，可由 `--retry-failed` 恢复。
  取舍：坏图不自动重试 —— 成功响应可能已计费，恢复权交给显式 `--retry-failed`。

## 3. 中 / 低（择要）

- **M1.** `--retry-failed` 绕过状态机直接赋值并清零 attempts/repair_rounds,
  无 JobEvent、无历史记录,修复预算可被反复清零（asset_pack.py:179）。
- **M2.** `pack_id` 复用会静默覆盖另一批次的 request 快照与 summary,
  旧展开文件残留,批次溯源无法区分（asset_pack.py:118,493）。
- **M3.** `export <裸asset_id>` 会被当前目录同名路径劫持——只用 `exists()`
  判定目录/ID（cli.py:1162）。
- **M4.** `process` 后 manifest=`processed` 而 job=`exported`,旧 exports/
  无过期标记（与交接文档 §7.2 第 4 条同源）。
- **M5.** generic-json / manifest 的 `palette` 是声明值非实际值(声明 12 实用 3),
  且 `max_colors` 不进交付格式,消费方无法校验色数。
- **L1.** pack schema 与 Pydantic 模型多处口径分裂(schema_version 必填性、
  colors/targets 唯一性、hex 校验、style 禁字段)。
- **L2.** ✅ 已修 —— `catch_warnings` 是**进程级**状态，pack 线程池下压制会失效，
  NumPy RuntimeWarning 漏进 stderr 且漏不漏取决于调度。改为**不产生**警告：
  只对 `keep` 留下的块求中位数（全透明块的结果本来就被滤掉、用不上）。
  `resize.py` 有同源的第二处，审查未点到，一并修了。回归测试用
  `simplefilter("error")` 钉「不产生」而非「被压住」，全套件警告数从每轮必现降到 0。
- **L3.** 静态资产被打动画告示(「请看 previews/*.gif」而 gif 不存在);
  pack 无汇总 contact sheet;`frame_size` 的 measured 报面积不报尺寸;
  `atomic_write` 产物权限 0600 与直写文件 0644 混杂;CLI「色数」列报声明值;
  `grid_block_size`=None 时「未吸附」与「未知」不可分;单资产
  `style.palette_colors` 与 pack `shared.palette.colors` 命名不一致。

## 4. mock 审不了、待真实生成复审的 12 项

主体语义正确性与包内风格一致性 · 真实量化压力（软边缘 6 万色 → 8/14 色）·
`snap_to_palette` 真实偏移 · **像素网格吸附链路（一次都没真正执行过）** ·
色键/despill/残留告警 · 背景冲突降级 · 连通域抽帧退化路径 · 分辨率告警真伪 ·
`blank_frame`（唯一有判别力项恰只有真实生成触发）· Godot 真机 ·
端点尺寸吸附 · 并发/退避/限流压测。

## 5. 好的方面（审查确认成立的主张)

- 导出 PNG 五资产全指标达标,alpha 严格二值,透明 RGB 全零,调色板零越界。
- 「仅凭 Manifest + frames/ 重建导出」**逐字节成立**(删 exports 重跑、
  最小文件集重跑、跨会话三组验证)。
- 离线 `process` 幂等:重跑哈希不变,manifest 除 status 外零字段漂移;
  `key_threshold` / `grid_block_size` 正确回灌。
- 静态 Godot 导出「无 .tres 只出纹理」与设计意图一致,锚点自洽。
- 静态类型集合、pack 映射、五类 prompt 措辞分支无集合漂移(codex 确认)。

---

## 6. 真实生成复审（2026-08-02，gpt-image-2）

§4 列的 12 项 mock 盲区，本轮用真实端点跑了 4 个静态资产（1 个 `ui_icon` +
`environment_pack` 三件），覆盖其中 5 项。**API Key 只存在于 session scratchpad，
未进仓库、未进任何提交。**

### 6.1 证实的行为

- **端点尺寸吸附是真的**：4/4 请求 `1024×1024`、实际返回 `1254×1254`。
  PLAN §2.3 的警告成立，Manifest 的 `requested_size`/`actual_size` 双字段
  确有必要。
- **像素网格吸附链路首次真正执行**（mock 下一次都没跑过）：4 个样本里
  2 个判定吸附、2 个判定"块是臆想的，保持原图"（如误差 30.3 > 阈值 20）。
  两条分支都走到了。
- **产出质量达标**：4/4 alpha 严格二值、透明 RGB 全零、用色 8~11 ≤ max_colors、
  显式色板资产零越界。目测三件环境物件风格统一（共享 style + palette 生效），
  木桶有金属箍与木板纹理、苔石有岩体与苔藓分层，是可直接用的游戏资产。

### 6.2 修掉的误报：`key_color_residue` 阈值

**真实生成第一发就被自己刚加的防线挡住了。** 一个**完全合格**的金钥匙图标
（成品洋红像素 0，视觉无瑕疵）被判 `key_color_residue` FAIL（量化前 6.4% > 5%），
未能导出。

根因：这个比例的大头是**被前景围住的封闭背景区** —— 钥匙的圆环孔。色键的
漫水填充只清与画布外缘连通的背景，孔洞留到 `strip_key_residue` 才删，
而那一步在量化**之前**，成品是干净的。`strip_key_residue` 的文档字符串写明了
这一点，常量名 `KEY_RESIDUE_WARN_RATIO` 写的就是 WARN，pipeline 里也一直
只当告警用 —— 是接进验证引擎时被误当成了 FAIL 判据。

改为两级：**成品**仍有过近键控色的像素 → FAIL（阈值 0，比原来更严，
这才是 despill 真失败）；**量化前**比例高 → WARN。

真实样本分布（key_color_residue）：`0.0 · 0.0 · 0.0 · 0.064`，
唯一超阈值的就是那个合格资产 —— 按比例判 FAIL 在真实分布上是误报为主。

### 6.3 新发现

- **细长资产的画布填充率极低**：`street_lantern` 283 个不透明像素
  （木桶 1272），48×48 画布填充率 12%，灯柱只剩 1~2 像素宽。
  统一正方形画布对细长物体不友好。**非流水线 bug，是资产设计约束**，
  但值得让用户知道：细长物件要么给更大 `target_size`，要么接受细节丢失。
- **单资产路径的指纹冲突抛裸 `ValueError` + 完整 traceback**（换模型名即触发）。
  pack 路径有可读消息（`_require_saved_plans`），单资产路径没有。可用性缺陷。
- **`ui_icon` 没有合适的 `perspective` 取值**：schema 只允许
  `top_down_3_4/top_down/side_view/isometric`，UI 图标是正面平视的。
  prompt 层已用措辞兜住，但字段语义对不上。

### 6.4 仍未覆盖的 mock 盲区

动画链路（seed → 动作网格 → 抽帧）全部未跑 —— 本轮只跑了静态资产。
真实量化压力、`snap_to_palette` 真实偏移、背景冲突降级、连通域抽帧退化路径、
`blank_frame`、Godot 真机、并发/退避压测，仍待覆盖。
