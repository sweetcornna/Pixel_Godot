# 阈值校准建议报告

- UTC：`2026-08-06T00:56:27.008500Z`
- Provider：`openai` / `gpt-image-2`
- 样本：9 个资产，16 个动作
- 调用对账：规划 25，完成 25
- 结论边界：本报告只产出证据与建议，未修改 `constants.py`。

## 判读策略

只有人工确认合格的真实样本越过现阈值，才构成放宽证据；样本远低于现阈值不构成
收紧依据。`anchor_drift` 是与轮廓无关的绝对像素量，可作为收紧例外单独审计。

## 按动作聚合

| 动作 | n | 指标 | min | max | mean | 当前阈值 | 建议 |
|---|---:|---|---:|---:|---:|---:|---|
| `cast` | 6 | `height_variation` | 0.0000 | 0.4737 | 0.1591 | 0.3 | 若全部样本人工判定合格，才可考虑放宽；候选下限 0.48 |
| `cast` | 6 | `silhouette_variation` | 0.0248 | 0.4154 | 0.1724 | 0.45 | 实测未越线；按不对称策略不据此收紧 |
| `cast` | 6 | `anchor_drift` | 0.3734 | 0.4925 | 0.4294 | 3 | 绝对像素量例外：人工审核后可评估收紧；脚本不执行 |
| `impact` | 3 | `height_variation` | 0.2321 | 0.7059 | 0.4578 | 豁免 | 当前豁免；先人工确认该指标是否适合作为阻断项 |
| `impact` | 3 | `silhouette_variation` | 0.8413 | 1.7155 | 1.3219 | 豁免 | 当前豁免；先人工确认该指标是否适合作为阻断项 |
| `impact` | 3 | `anchor_drift` | 0.1875 | 1.0000 | 0.5277 | 豁免 | 当前豁免；先人工确认该指标是否适合作为阻断项 |
| `loop` | 2 | `height_variation` | 0.0480 | 0.0968 | 0.0724 | 0.2 | 实测未越线；按不对称策略不据此收紧 |
| `loop` | 2 | `silhouette_variation` | 0.0431 | 0.0772 | 0.0602 | 0.3 | 实测未越线；按不对称策略不据此收紧 |
| `loop` | 2 | `anchor_drift` | 0.1384 | 0.3727 | 0.2555 | 2 | 绝对像素量例外：人工审核后可评估收紧；脚本不执行 |
| `travel` | 2 | `height_variation` | 0.0000 | 0.0316 | 0.0158 | 0.4 | 实测未越线；按不对称策略不据此收紧 |
| `travel` | 2 | `silhouette_variation` | 0.0082 | 0.1534 | 0.0808 | 0.6 | 实测未越线；按不对称策略不据此收紧 |
| `travel` | 2 | `anchor_drift` | 0.2198 | 0.4713 | 0.3456 | 4 | 绝对像素量例外：人工审核后可评估收紧；脚本不执行 |
| `walk` | 3 | `height_variation` | 0.0000 | 0.0210 | 0.0140 | 0.12 | 实测未越线；按不对称策略不据此收紧 |
| `walk` | 3 | `silhouette_variation` | 0.0245 | 0.0579 | 0.0455 | 0.2 | 实测未越线；按不对称策略不据此收紧 |
| `walk` | 3 | `anchor_drift` | 0.4922 | 0.4952 | 0.4942 | 1 | 绝对像素量例外：人工审核后可评估收紧；脚本不执行 |

## `up` ×1.3 证据

| 样本 | 指标 | 实测 | down 基准阈值 | up ×1.3 阈值 | 判读 |
|---|---|---:|---:|---:|---|
| `cal_knight/walk_up` | `height_variation` | 0.0210 | 0.12 | 0.156 | 该样本未显示需要放宽 |
| `cal_knight/walk_up` | `silhouette_variation` | 0.0245 | 0.2 | 0.26 | 该样本未显示需要放宽 |
| `cal_archer/walk_up` | `height_variation` | 0.0210 | 0.12 | 0.156 | 该样本未显示需要放宽 |
| `cal_archer/walk_up` | `silhouette_variation` | 0.0579 | 0.2 | 0.26 | 该样本未显示需要放宽 |
| `cal_mage/walk_up` | `height_variation` | 0.0000 | 0.12 | 0.156 | 该样本未显示需要放宽 |
| `cal_mage/walk_up` | `silhouette_variation` | 0.0540 | 0.2 | 0.26 | 该样本未显示需要放宽 |

## 人工审图

先逐个查看 contact sheet 的轮廓、动作语义和方向，再播放 GIF 检查帧序。任何不合格
样本都必须从阈值证据中排除，不能靠放宽阈值让它通过。

- `cal_knight` contact sheet：`assets/cal_knight/previews/contact-sheet.png`
- `cal_knight` GIF：`assets/cal_knight/previews/cast_down.gif`
- `cal_knight` GIF：`assets/cal_knight/previews/walk_up.gif`
- `cal_archer` contact sheet：`assets/cal_archer/previews/contact-sheet.png`
- `cal_archer` GIF：`assets/cal_archer/previews/cast_down.gif`
- `cal_archer` GIF：`assets/cal_archer/previews/walk_up.gif`
- `cal_mage` contact sheet：`assets/cal_mage/previews/contact-sheet.png`
- `cal_mage` GIF：`assets/cal_mage/previews/cast_down.gif`
- `cal_mage` GIF：`assets/cal_mage/previews/walk_up.gif`
- `cal_golem` contact sheet：`assets/cal_golem/previews/contact-sheet.png`
- `cal_golem` GIF：`assets/cal_golem/previews/cast_down.gif`
- `cal_imp` contact sheet：`assets/cal_imp/previews/contact-sheet.png`
- `cal_imp` GIF：`assets/cal_imp/previews/cast_down.gif`
- `cal_slime` contact sheet：`assets/cal_slime/previews/contact-sheet.png`
- `cal_slime` GIF：`assets/cal_slime/previews/cast_down.gif`
- `cal_fireball` contact sheet：`assets/cal_fireball/previews/contact-sheet.png`
- `cal_fireball` GIF：`assets/cal_fireball/previews/impact.gif`
- `cal_fireball` GIF：`assets/cal_fireball/previews/travel_down.gif`
- `cal_lightning_chain` contact sheet：`assets/cal_lightning_chain/previews/contact-sheet.png`
- `cal_lightning_chain` GIF：`assets/cal_lightning_chain/previews/impact.gif`
- `cal_lightning_chain` GIF：`assets/cal_lightning_chain/previews/loop.gif`
- `cal_lightning_chain` GIF：`assets/cal_lightning_chain/previews/travel_down.gif`
- `cal_healing_aura` contact sheet：`assets/cal_healing_aura/previews/contact-sheet.png`
- `cal_healing_aura` GIF：`assets/cal_healing_aura/previews/impact.gif`
- `cal_healing_aura` GIF：`assets/cal_healing_aura/previews/loop.gif`

## 人工落回流程

1. 只保留人工判定合格的 live 样本，复算对应动作的 min/max/mean。
2. 按上述不对称策略形成阈值变更提案；`up` 系数单独比较基准阈值与 ×1.3。
3. 人工修改 `pixel_asset_forge/constants.py`，再用同一批合格帧复验。
4. 将日期、模型、样本量、人工审核结论和改动依据追加到 `docs/threshold-calibration.md`。
