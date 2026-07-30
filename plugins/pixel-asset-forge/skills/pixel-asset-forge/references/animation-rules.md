# 动作规则

每一条都来自一次实测失败，不是风格偏好。改动前先读对应的"踩过的坑"。

---

## 1. 步态必须分视角

**正面 / 背面**（`down` / `up`）与**侧面**（`left` / `right`）用两套完全不同的节拍。

| 视角 | 节拍 | 可读线索 |
|---|---|---|
| 正面 / 背面 | NEUTRAL → STEP → STRIDE → RECOVER | 哪只脚在前、双脚是否并拢 |
| 侧面 | CONTACT → DOWN → PASSING → UP | 身体的上下起伏、蹬地 |

**踩过的坑**：两个视角共用侧视的 CONTACT/DOWN/PASSING/UP。那套描述讲的是
"身体降到最低点""用脚尖蹬地"——只有从侧面才看得见。正面朝向镜头时模型读不出来，
只能瞎猜，产出的动作用户一眼就说"很奇怪"。

正面那套取自 agent-sprite-forge 的俯视 RPG 四帧步：并拢 → 左脚前 → 并拢 → 右脚前。

实现见 `PoseCycle.frontal_beats`。

---

## 2. 帧数不多于节拍数时只挑不插，且挑法用 `ceil`

插值的既定用途是"帧数**多于**节拍数"。帧数更少时还插值，写出的是：

```
about 33% of the way from DOWN to PASSING:
starting from (…) and moving towards (…)
```

图像模型据此画不出确定姿势，实测被平均成站姿，六格里腿完全不动。

挑的时候三种取整方式差别很实在（4 拍取 3 帧）：

```
int   → CONTACT · DOWN · PASSING   没有最高点，只沉不起
round → CONTACT · DOWN · UP        丢掉了 PASSING
ceil  → CONTACT · PASSING · UP     交叉腿在，且是上升的
```

**PASSING 是双腿交叉那一拍，行走最强的可读线索——宁可丢一个极值也不能丢它。**
`ceil` 还顺带让 4 拍取 2 帧得到 CONTACT·PASSING，正是经典的两帧半周期走法。

一次性动作（attack / hurt / death）另算：首尾必取，丢了起手或收势就不成立了。

---

## 3. 半周期的每一拍都必须带方位线索

步态靠"左右互换"生成后半周期。描述里一个 `left`/`right` 都没有的拍子，
互换后与原文一字不差——与 prompt 里 `no two cells may be identical` 直接冲突，
编译期会抛 `PlanError`。

所以"双脚并拢"这一拍要写成"**右脚**刚与左脚并齐"。

---

## 4. 左右腿对调不是镜像

后半周期的描述会说 `the RIGHT leg strides far forward`。模型常把这个对调
理解成把整个角色镜像过来——剑换到另一只手、斗篷翻到另一边。

两道防线：

- prompt 里点名：`ONLY the legs and arms swap roles — this is NOT a mirror.
  The weapon stays in the same hand, the cloak stays on the same shoulder…`
- **anchor sheet 底图**（下条），这条才是真正管用的。

---

## 5. base image 用 anchor sheet，不用空白画布

把已批准的 seed 按固定缩放与脚线平铺进每一格，作为 `images.edit` 的底图。
模型要做的从"照描述画 N 个角色"变成"把这 N 个角色摆成不同姿势"。

同时钉住四件文字说不清的事：持械手、体型、脚线、构图。

构图比例（取自 agent-sprite-forge 的 `make_anchor_layout.py` 默认值）：

```
角色高 = 格子高 × 0.66
角色宽 = 格子宽 × 0.72
脚线   = 格子高 × 0.82
```

配套的 prompt 措辞必须告诉模型底图不是空白：
`The image you are editing is a template … Change ONLY the pose in each cell …
Never zoom or resize a pose to fill its cell.`

实现见 `pipelines/common.py::anchor_sheet`。

---

## 6. 一张图只放一条完整的帧序列

分成两行时模型倾向于把每行当作独立的一组，行与行之间朝向、体型、基线各走各的。

单行 N 格的整幅长宽比是 `N × 格子宽高比`，而 API 限制长短边比 ≤ 3，
所以格子宽高比必须 ≤ 3/N：

| 帧数 | 上限 | 结论 |
|---|---|---|
| 4 | 0.75 | 宽松 |
| 6 | 0.50 | 刚好 |
| 8 | 0.375 | 装不下带跨步和佩剑的角色 |

**这个比例与图放多大无关**，放大整幅图并不能松绑，所以 8 帧单行无解。
`layout_for_frames` 会自动退回 4×2，并在 prompt 里明说"两行是一条序列，不是两条"。

实测端点认单行请求：1920×640 → 返回 2172×724，3:1 完整保留。

---

## 7. 锁的是朝向，不是动作

朝向锁死能治摇摆，但锁过头模型会交出一排几乎一样的站姿。同一段约束里必须有反向配重：

```
What is LOCKED is the orientation, NOT the motion. The limbs must move a lot:
- the pose difference between neighbouring cells must be obvious at a glance
- in the cells where the legs are described as striding, the gap between the
  two feet must be at least as wide as the character's shoulders
- do not draw a row of near-identical standing poses with only tiny differences
```

Sprint 0 踩过一次这个坑，加连续性约束后又踩了一次。

---

## 8. attack / cast 一律 body-only

禁止脱体的挥砍弧、武器拖尾、枪口火光、冲击爆点、扬尘。

这些会把包围盒撑到远大于 idle，脚线锚点与跨动作缩放基准双双失准——
角色在 attack 与 idle 之间忽大忽小、脚还离地。特效该单独出一张 fx 表，在引擎里叠加。

只对 attack / cast 生效，走路不背这条约束。见 `SWING_ACTION_NEGATIVES`。

---

## 9. "3/4" 这三个字符不许出现

游戏开发口径里"3/4 俯视"说的是**相机俯角**；角色美术口径里"3/4 视角"说的是
**角色绕自身竖轴转了 45°**。模型按后者理解，于是每个朝向都被额外叠了一次转身——
背面走路能看见半张脸和斜着的肩线。

相机说相机、转身说转身，两段分开写。有测试守着这个词，
除负面清单里的 `no three-quarter turn` 外任何 prompt 出现即失败。
