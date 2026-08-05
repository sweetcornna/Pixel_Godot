# 阈值校准 harness

这个工具把 README“未达标项 1”的取样、量测、聚合和人工审图材料固化成一条命令。
它固定生成 6 个角色的 `cast_down`、3 个已校准动作的 `up` 样本，以及 3 种形态差异
较大的特效动画。每次运行使用新的隔离目录并强制关闭缓存。

工具**只产出证据，不修改阈值**。`pixel_asset_forge/constants.py` 在运行前后会做
SHA-256 对账；任何阈值或 `up` 系数的变化都必须经过人工审图和代码审计。

## 开跑前的契约自检：花钱之前先静态判一遍

打印预算之后、**创建运行目录与发出第一次调用之前**，harness 会检查一件事：
有没有哪个动作**既被 prompt 命令改变整体尺寸、又被尺寸阈值管着**。有就直接报错退出，
一次调用都不发。

这条检查来自实测教训。2026-08-05 的 live 运行花掉 25 次调用，结论是"样本不合格"；
而真正的根因是 `loop` 的节拍逐字写着"从最小变到最大"，它的 `height_variation` /
`silhouette_variation` 阈值恰恰禁止尺寸变化 —— **两份规格互相矛盾，这件事在发出请求
之前就完全可判定**。那 25 次调用本可以省下。

判据是**成对**的：`impact` 同样命令扩张，但它两项尺寸阈值都是豁免（爆开消散正该
如此），不算矛盾。只看命令词会把正确设计误判成冲突。

报错会指名到具体的拍与命中词，并给出两条正确出路 —— 改节拍或把该动作的尺寸阈值改成
豁免。**它明确禁止第三条：靠放宽阈值让越线样本通过。**

## 跑法

先用 mock 演练完整链路。不联网、不需要 API Key：

```bash
PIXEL_ASSET_PROVIDER=mock \
PIXEL_ASSET_MODEL=mock-image \
UV_CACHE_DIR=/tmp/uv-cache \
uv run python tools/calibration/run_calibration.py
```

真实校准使用 OpenAI provider。API Key 只从 `OPENAI_API_KEY`、
`PIXEL_ASSET_API_KEY` 或项目 `.env` 读取；脚本没有 `--api-key` 参数：

```bash
# 先用本机密钥管理方式设置 Key，或使用交互式 init 已写好的项目 .env
PIXEL_ASSET_PROVIDER=openai \
PIXEL_ASSET_MODEL=gpt-image-2 \
UV_CACHE_DIR=/tmp/uv-cache \
uv run python tools/calibration/run_calibration.py --max-calls 25
```

`--max-calls` 默认是 25。小于 25 会在创建运行目录和调用 provider 前拒绝；大于 25
也不会增加样本，固定矩阵仍只规划 25 次。缓存关闭后，脚本要求实际完成数必须等于
计划数，否则以错误退出并保留运行目录供排查。

## 固定预算

一次“调用”指一次 seed 或一次完整动作网格的 provider 调用，不按帧数计费。

| 资产单元 | 动作样本 | seed | 动作 | 小计 |
|---|---|---:|---:|---:|
| 骑士 `cal_knight` | `cast_down`, `walk_up` | 1 | 2 | 3 |
| 弓手 `cal_archer` | `cast_down`, `walk_up` | 1 | 2 | 3 |
| 法师 `cal_mage` | `cast_down`, `walk_up` | 1 | 2 | 3 |
| 石魔 `cal_golem` | `cast_down` | 1 | 1 | 2 |
| 小恶魔 `cal_imp` | `cast_down` | 1 | 1 | 2 |
| 史莱姆 `cal_slime` | `cast_down` | 1 | 1 | 2 |
| 火球 `cal_fireball` | `travel_down`, `impact` | 1 | 2 | 3 |
| 闪电链 `cal_lightning_chain` | `travel_down`, `impact`, `loop` | 1 | 3 | 4 |
| 治疗光环 `cal_healing_aura` | `impact`, `loop` | 1 | 2 | 3 |
| **合计** | **16 个动作样本** | **9** | **16** | **25** |

每次成功运行在 `tools/calibration/runs/<UTC时间戳>/` 产生（`runs/` 不入库；
下表前五个量化文件会镜像一份到 `tools/calibration/reports/<UTC时间戳>/`
接受版本审查，与 live-gate 同一约定）：

| 文件 | 用途 |
|---|---|
| `threshold-calibration-metrics.json` | 与旧校准记录同构的逐样本原始量测 |
| `threshold-calibration-aggregates.json` | 按动作、按动作+方向聚合的 min/max/mean |
| `threshold-recommendations.md` | 不对称策略下的建议和 `up` ×1.3 判读表 |
| `preview-paths.json` | 每个资产的 contact sheet 与 GIF 路径清单 |
| `run-manifest.json` | provider、固定矩阵、预算对账、缓存状态和 constants 哈希 |
| `assets/` | pipeline 的原图、成品帧、spritesheet、contact sheet 与 GIF |
| `requests/` | 本次实际提交给 pipeline 的请求快照 |

## 判读方法

先看 `preview-paths.json` 列出的 contact sheet 和 GIF。只有人工确认动作语义、方向、
身份和逐帧质量都合格的**真实 provider**样本，才能进入阈值证据集。mock 数值只证明
harness 全链可运行，不能用于调整阈值。

沿用 [既定的不对称策略](../../docs/threshold-calibration.md#调整策略是不对称的)：

1. 合格样本越过现阈值时，才有证据支持放宽；候选值不得低于合格样本最大值。
2. 实测远低于现阈值不代表可以收紧。少量样本无法证明下一个轮廓也会落在同一范围。
3. `anchor_drift` 是与角色轮廓无关的绝对像素量，可以作为收紧例外单独审计。
4. `impact` 当前豁免。即使有了分布，也要先判断该指标对爆炸类动作是否有阻断意义。
5. `walk_up` 单列比较 down 基准阈值和 ×1.3 后阈值；不因一次运行自动改系数。

## 人工落回

1. 完成 live 运行并逐图审核，在审计记录中列出合格与剔除的样本。
2. 从原始 JSON 复算仅含合格样本的按动作 min/max/mean，核对 harness 聚合。
3. 根据不对称策略形成具体提案，人工修改 `ACTION_THRESHOLDS` 或
   `DIRECTION_MULTIPLIER`；harness 不执行这一步。
4. 用同一批合格帧复验新阈值，确认不会打回自己的校准集。
5. 在 `docs/threshold-calibration.md` 追加日期、provider/model、样本量、人工审核结论、
   原值/新值和证据路径；再由审计者决定是否提交 `constants.py`。

## 2026-08-03 mock 完整演练

执行命令：

```bash
PIXEL_ASSET_PROVIDER=mock PIXEL_ASSET_MODEL=mock-image \
UV_CACHE_DIR=/tmp/uv-cache uv run python tools/calibration/run_calibration.py
```

- 日期：2026-08-03
- 运行目录：`tools/calibration/runs/20260803T182416.480460Z/`（大件产物，本地保留、不入库）
- 矩阵：9 个资产单元、16 个动作样本
- 对账：计划 25 次，完成 25 次，缓存关闭
- 结论：完整离线链路通过；这是演练，不是阈值校准证据
- 量化报告（入库）：`tools/calibration/reports/20260803T182416.480460Z/` ——
  原始量测、聚合结果、建议报告、审图路径与运行清单五件

真实校准仍欠一次 live 运行，需要可用 API Key、25 次固定调用预算和逐样本人工审核。
