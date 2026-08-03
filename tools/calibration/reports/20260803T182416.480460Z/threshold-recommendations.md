# 阈值校准建议报告

- UTC：`2026-08-03T18:24:16.480478Z`
- Provider：`mock` / `mock-image`
- 样本：9 个资产，16 个动作
- 调用对账：规划 25，完成 25
- 结论边界：本报告只产出证据与建议，未修改 `constants.py`。

## 判读策略

只有人工确认合格的真实样本越过现阈值，才构成放宽证据；样本远低于现阈值不构成
收紧依据。`anchor_drift` 是与轮廓无关的绝对像素量，可作为收紧例外单独审计。

> 本次是 mock 演练。数值只能证明生成、处理、量测、聚合和报告链路可运行，
> 不能用于调整真实模型阈值或 `up` 修正系数。

## 按动作聚合

| 动作 | n | 指标 | min | max | mean | 当前阈值 | 建议 |
|---|---:|---|---:|---:|---:|---:|---|
| `cast` | 6 | `height_variation` | 0.0159 | 0.0212 | 0.0203 | 0.3 | mock 只证明链路；不支持阈值决策 |
| `cast` | 6 | `silhouette_variation` | 0.0837 | 0.1122 | 0.0902 | 0.45 | mock 只证明链路；不支持阈值决策 |
| `cast` | 6 | `anchor_drift` | 0.5000 | 0.5000 | 0.5000 | 3 | mock 只证明链路；不支持阈值决策 |
| `impact` | 3 | `height_variation` | 0.0159 | 0.0164 | 0.0163 | 豁免 | mock 只证明链路；不支持阈值决策 |
| `impact` | 3 | `silhouette_variation` | 0.1122 | 0.1182 | 0.1162 | 豁免 | mock 只证明链路；不支持阈值决策 |
| `impact` | 3 | `anchor_drift` | 0.5000 | 0.5000 | 0.5000 | 豁免 | mock 只证明链路；不支持阈值决策 |
| `loop` | 2 | `height_variation` | 0.0320 | 0.0325 | 0.0323 | 0.2 | mock 只证明链路；不支持阈值决策 |
| `loop` | 2 | `silhouette_variation` | 0.0897 | 0.1070 | 0.0983 | 0.3 | mock 只证明链路；不支持阈值决策 |
| `loop` | 2 | `anchor_drift` | 0.4567 | 0.4701 | 0.4634 | 2 | mock 只证明链路；不支持阈值决策 |
| `travel` | 2 | `height_variation` | 0.0320 | 0.0320 | 0.0320 | 0.4 | mock 只证明链路；不支持阈值决策 |
| `travel` | 2 | `silhouette_variation` | 0.1070 | 0.1070 | 0.1070 | 0.6 | mock 只证明链路；不支持阈值决策 |
| `travel` | 2 | `anchor_drift` | 0.4567 | 0.4567 | 0.4567 | 4 | mock 只证明链路；不支持阈值决策 |
| `walk` | 3 | `height_variation` | 0.0434 | 0.0434 | 0.0434 | 0.12 | mock 只证明链路；不支持阈值决策 |
| `walk` | 3 | `silhouette_variation` | 0.1237 | 0.1237 | 0.1237 | 0.2 | mock 只证明链路；不支持阈值决策 |
| `walk` | 3 | `anchor_drift` | 0.5000 | 0.5000 | 0.5000 | 1 | mock 只证明链路；不支持阈值决策 |

## `up` ×1.3 证据

| 样本 | 指标 | 实测 | down 基准阈值 | up ×1.3 阈值 | 判读 |
|---|---|---:|---:|---:|---|
| `cal_knight/walk_up` | `height_variation` | 0.0434 | 0.12 | 0.156 | mock 不支持系数决策 |
| `cal_knight/walk_up` | `silhouette_variation` | 0.1237 | 0.2 | 0.26 | mock 不支持系数决策 |
| `cal_archer/walk_up` | `height_variation` | 0.0434 | 0.12 | 0.156 | mock 不支持系数决策 |
| `cal_archer/walk_up` | `silhouette_variation` | 0.1237 | 0.2 | 0.26 | mock 不支持系数决策 |
| `cal_mage/walk_up` | `height_variation` | 0.0434 | 0.12 | 0.156 | mock 不支持系数决策 |
| `cal_mage/walk_up` | `silhouette_variation` | 0.1237 | 0.2 | 0.26 | mock 不支持系数决策 |

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
