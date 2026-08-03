# 真实资产质量闸门

此前的 mock 验收证明了生成、切帧、处理和验证这条链能走通，但证明不了真实模型
画出来的图能不能用。Live gate 固定拿一组最小资产跑真实端点，把“看起来还行”收成
验证引擎里的数字；它是人工发布门槛，不进 CI。

## 跑

真实模式只从环境变量读配置和 Key。脚本没有、也不会提供 `--api-key`：命令行参数会
进入 shell history 和进程列表，比环境变量更容易留下明文。

```bash
# 先通过本机的密钥管理方式设置 OPENAI_API_KEY 或 PIXEL_ASSET_API_KEY
PIXEL_ASSET_PROVIDER=openai \
PIXEL_ASSET_MODEL=gpt-image-2 \
uv run python tools/live-gate/run_live_gate.py
```

先用 mock 验证本机链路，不联网、不需要 Key：

```bash
PIXEL_ASSET_PROVIDER=mock \
PIXEL_ASSET_MODEL=mock-image \
uv run python tools/live-gate/run_live_gate.py
```

脚本开跑前会先打印固定预算：

| 质量单元 | 调用数 |
|---|---:|
| `grass_field` tileset（3 块 tile） | 3 |
| `knight_01` canonical seed | 1 |
| `knight_01` `walk_down` | 1 |
| **合计** | **5** |

`--max-calls` 默认就是 5。设成更小的数会在创建运行目录、调用 provider 之前拒绝；
设成更大也不会扩大资产集。每次运行使用新的隔离目录并关闭生成缓存，因此一趟完整
运行的“规划 5 次”和“实际完成 5 次”可以直接对账，不会被旧缓存悄悄改成 0 次。

请求文件从 `examples/grass_field.yaml` 与 `examples/knight.yaml` 复制到
`tools/live-gate/runs/<UTC时间戳>/requests/` 后再交给流水线。资产写到同一运行目录下，
不会修改 `examples/` 或 `outputs/`。

## 三态结果

| 结果 | 退出码 | 含义 |
|---|---:|---|
| `PASS` | 0 | 三个质量单元与全部硬阈值都通过 |
| `FAIL` | 1 | 真实产物已生成、已测量，但 fatal/high、资产级或量化硬阈值不合格 |
| `ERROR` | 2 | Key 缺失、端点不通、生成中断、预算拒绝，或 gate 拿不到必需指标 |

这三态不能合并。网络断开不是“模型画坏了”；把它记成 `FAIL` 会把基础设施故障混进
阈值校准样本，之后再据此调阈值，得到的结论没有意义。

## 验什么

| 通过线 | 数据来源 |
|---|---|
| fatal / high 失败数 = 0 | `ValidationReport.blocking_checks` |
| 质量单元 3/3 | `ValidationReport.passed` + 本表其余硬阈值 |
| 每块 `tile_seam <= 3.0` | 检查项 id `tile_seam` |
| 每块 `tile_border <= 2.0` | 检查项 id `tile_border` |
| `walk_down` 的 `anchor_drift <= walk` 阈值 | 检查项 id `anchor_drift` 自带的 per-action threshold |
| 每个质量单元的颜色数 `<= max_colors` | 请求值，沿用检查项 id `palette_overflow` |

Live gate 不重算接缝、边框或锚点。它读取验证引擎已经给出的 `measured`，只按 §9.4
收紧通过线。调色板也不另造 `live_palette_count` 之类的名字，而是把颜色数与请求上限
明确标成既有的 `palette_overflow` id。`report.json` 的 `metrics[]` 每一项都有
`check_id`，可以从报告一路追到验证引擎。

medium / low 的失败不阻断，但不会被汇总表吞掉。两份原始 `ValidationReport` 会完整
写入 `report.json.validation_reports`，包括所有 PASS / FAIL / WARN / SKIP，供后续阈值
校准使用。

### Seed 的边界

上游目前没有“只验证 canonical seed”的 `run_validation` 入口：seed 在
`create_character` 内完成处理后停在人工批准状态，验证引擎只会在动画完成后检查
`walk_down`。所以这里不伪造一份 seed `ValidationReport`；seed 质量单元要求
`create_character` 完成，并用 `palette_overflow` id 检查颜色数。报告里的
`validation_source` 会如实写出这一点。生成或处理 seed 的任何异常仍然是 `ERROR`，
不会变成产物质量 `FAIL`。

## 报告与 Key

每次实际执行都会写：

```text
tools/live-gate/reports/<UTC时间戳>/report.json
tools/live-gate/reports/<UTC时间戳>/report.md
```

JSON 保存机器可读的预算、三态、三个质量单元、全部量化指标和原始验证检查；Markdown
是给人审阅的汇总表。`reports/` 不忽略，真实实测数字可以进入版本审查；体积大的
`runs/` 被 `.gitignore` 排除。

脚本在输出异常、日志和报告前先按环境里的 Key 值脱敏。两份报告写完后还会重新读回，
并扫描运行目录内的 JSON、JSONL、YAML、Markdown、文本和日志文件；一旦发现任一 Key
明文，两份报告立即删除，本次结果改为 `ERROR`。这是最后一道防线，不是允许上游随便
记录秘密的理由。

## 这个门槛抓得住什么

| 反例 | 结果 |
|---|---|
| 报告里放一个 fatal `frame_count` 失败 | 抓住：总结果为 `FAIL`，退出码 1 |
| 报告里放环境 Key 的明文 | 抓住：JSON 与 Markdown 一起删除，退出码 2 |
| `--max-calls 4` | 抓住：打印 5 次预算后拒绝，provider 不执行，退出码 2 |
| medium `palette_overflow` 失败 | 不阻断，但原始检查完整落盘 |

前三条反例都在写入单测前先用本地探针确认确实被拒绝，避免“反例本身没有破坏任何
东西、测试却看起来验过了”的假安全感。对应离线测试在
`tests/unit/test_live_gate.py`。

