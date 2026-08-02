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

### 质量防线类

- **Q1. 静态验证 7 项里 6 项结构性恒真。** `palette_membership` / 
  `transparent_rgb_residue` / `frame_size` / `content_bounds` 分别被上游的
  `snap_to_palette` / `zero_transparent_rgb`+`save_png` / `place_on_canvas` /
  `inner_size=target-2` 构造保证；唯一有判别力的是 `blank_frame`。四个缺陷
  注入探针（半透明 alpha、孤立像素、256 色爆表、键控残留）**全部漏过**。
  「验证通过」目前只等于「文件在、哈希对、不是全透明」。
- **Q2. `max_colors` 静态路径全程无人校验。** `palette_overflow` 只在动画路径跑；
  `palette_overflow_ratio`（PLAN §9.2 判据）生产代码零调用。
- **Q3. 「主体被切掉」无自动判据。** `cell_overflow` 依赖 grid+source_image，
  静态资产两者皆无；`detect_overflow` 结果在 `create_static_asset` 里被丢弃。
  这类错误恰是「本地补不回、必须重生成」的最该拦的一类。
- **Q4. skip 理由机制未落地静态路径。** 19 个 CheckId 静态只出现 7 个，
  另 12 个静默消失，`summary.skipped: 0` 误导消费方。动画路径遵守了
  「防线缺失要显式记录」，静态路径没有。
- **Q5. 处理告警不落盘，批量路径连终端都不打。** 「94% 细节丢失」「网格未吸附」
  只进 stdout；`create-asset-pack` 的 JSON/表格无 warnings 字段。

### 交付类

- **Q6. Contact sheet 背景 `#22222C` 与示例调色板描边色 `#211A2C` 距离 8.06**，
  wooden_barrel 42% / quest_marker 46% 的像素在审核图上不可见 ——
  而 contact sheet 是 README 定位的「唯一人工防线」。
- **Q7. Godot 交付知识只存在于终端文本。** 无 `.import` 文件（Filter=Nearest
  无法随目录交付，默认线性过滤会糊掉像素图）；notes 只有 `export` 子命令打印。
- **Q8. `.tres` 的 `ext_resource` 是项目根绝对路径**（`res://<asset_id>.png`），
  与导出器 note「整目录复制进项目」矛盾 —— 放进子目录即 Parse Error。
  静态资产暂不产 `.tres` 所以未踩，动画路径会踩。PLAN §10.4 的
  「整目录复制即可用」当前不成立。

### 执行语义类（codex）

- **X1. 重叠 pack 并发无跨进程互斥**：任务表「读-改-写」无 CAS，两进程同跑同
  `asset_id` 会重复计费、互相覆盖状态（asset_pack.py:482 / cache.py:37）。
- **X2. 规划指纹不含 `base_url`**：换端点不换指纹，闸门放行；`PIPELINE_VERSION`
  固定 0.1.0 也使代码变更不失效旧计划（models/pack.py:152 / config.py:62）。
- **X3. `create_static_asset` 先 `save_request_copy` 后查指纹冲突**：冲突时
  request.yaml 已被新请求覆盖，溯源包自相矛盾（static_asset.py:158）。
- **X4. `validated/exported` 状态不与产物哈希绑定**：验证后替换
  `frames/static.png` 再恢复，新文件未经验证即被导出；产物被删后 pack 仍报
  `skipped`（static_asset.py:93 / asset_pack.py:339）。
- **X5. 静态导出硬闸以 `static_image is not None` 为条件**：Manifest 缺该字段
  反而**绕过**硬闸，可产出无图元数据 JSON 并标记 exported（export.py:109）。
- **X6. Provider 成功但图不可解析 → job 永卡 `generating`**：Pillow 异常不是
  ProviderError,不写失败态、无检查点,`--retry-failed` 与续跑都救不回,
  且可能已计费（static_asset.py:197 / providers/base.py:314）。

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
- **L2.** `catch_warnings` 非线程安全,pack 线程池下 NumPy RuntimeWarning
  泄漏不确定(pixel_grid.py:214)。
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
