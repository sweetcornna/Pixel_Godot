# Live gate 报告

- 结果：**PASS**（退出码 0）
- UTC：`20260803T073426.242823Z`
- Provider：`openai` / `gpt-image-2`
- 调用：规划 5，完成 5
- 资产级：3/3
- fatal / high 失败：0

## 资产结论

| 质量单元 | 资产级验证来源 | 结果 |
|---|---|---|
| `grass_field` | ValidationReport.passed | **PASS** |
| `knight_01_seed` | create_character completed; upstream has no seed ValidationReport | **PASS** |
| `knight_01_walk_down` | ValidationReport.passed (target=walk_down) | **PASS** |

## 量化指标

| 资产 | 检查项 id | target | 实测 | 阈值 | 结果 |
|---|---|---|---:|---:|---|
| `grass_field` | `tile_seam` | `dirt_path` | 0.938 | <= 3.0 | PASS |
| `grass_field` | `tile_seam` | `grass_base` | 0.829 | <= 3.0 | PASS |
| `grass_field` | `tile_seam` | `shallow_water` | 0.58 | <= 3.0 | PASS |
| `grass_field` | `tile_border` | `dirt_path` | 0.4 | <= 4.0 | PASS |
| `grass_field` | `tile_border` | `grass_base` | 0.242 | <= 4.0 | PASS |
| `grass_field` | `tile_border` | `shallow_water` | 2.05 | <= 4.0 | PASS |
| `grass_field` | `palette_overflow` | `tileset_palette` | 16 | <= 16 | PASS |
| `knight_01_seed` | `palette_overflow` | `seed_palette` | 32 | <= 32 | PASS |
| `knight_01_walk_down` | `anchor_drift` | `walk_down` | 0.39 | <= 1.0 | PASS |
| `knight_01_walk_down` | `palette_overflow` | `walk_down_palette` | 32 | <= 32 | PASS |

## 验证检查汇总

| total | pass | fail | warn | skip |
|---:|---:|---:|---:|---:|
| 52 | 30 | 0 | 0 | 22 |

> medium / low 不阻断，但原始检查已完整保存在 report.json 的 `validation_reports`。
