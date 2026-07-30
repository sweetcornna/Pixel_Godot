# Prompt 编译规则

Prompt 是**编译产物**，不是手写文本。`prompts/compiler.py` 是纯函数：
同样的 request 必然编译出同样的 prompt，所以这一层可以被完整断言。

不要手写 prompt 塞给模型。手写的 prompt 绕过了下面每一条约束，
而每一条都对应一次实测失败。

---

## 段落顺序

编译出的 prompt 按固定顺序拼接，顺序本身是有讲究的：

```
1. 一句话说清这是什么          Pixel art walk animation sprite sheet …
2. 底图是模板，不是空白        The image you are editing is a template …
3. 相机 + 朝向（独立成段）     Camera: … / Body orientation: …
4. 布局                       ONE single complete animation strip …
5. 逐格姿势                   Cell 1 (row 1, column 1): NEUTRAL — …
6. 身份约束                   these must be IDENTICAL in every single cell
7. 帧间连续性                 the body orientation is LOCKED across all cells
8. 摆放约束                   margin / baseline / same size / no empty cell
9. 风格
10. 背景（键控色）
11. 负面清单
```

**朝向必须独立成段。** 早先写成 `…, {facing}, {perspective}.` 这种夹在逗号
中间的修饰语，模型当修饰语处理，压不住多余的转身。

---

## 键控色不硬编码

背景色由 `resolve_key_color` 按角色描述决定，prompt 里填的是它的结论。

史莱姆是绿的，用洋红没问题；可绿色角色配绿背景就会被抠掉半个身子。
冲突检测按**词边界**匹配 ASCII、按子串匹配 CJK——
`slime` 里含 `lime` 曾让史莱姆被误判成绿色而降级，这是真实发生过的 bug。

---

## 边距写 12 判 8

实测模型把边距要求打约七折执行：写 8% 时实测最小边距 0.0%、真的跨了格线；
写 12% 时得到 7.9%、四条格线全干净。

所以 `PROMPT_MARGIN_PERCENT = 12`，而验证阈值按 8% 判。这个不对称是刻意的。

---

## 负面清单：写成"不要什么"

模型对"不要什么"比"要什么"更敏感。清单里每一条都对应一种会让资产**直接作废**
的产出，不是"最好别有"：

| 条目 | 后果 |
|---|---|
| 文字 / 标签 / 数字 | 模型很爱标 "1 2 3 4"，那些像素会被切进帧里 |
| 格线 / 边框 | 画出来的格线会被越界检测判为跨格连通域 |
| 落地阴影 | 紧贴脚底，键控抠不掉，变成角色身上多出来的一块 |
| 辉光 / 动态模糊 | 轮廓外围一圈半透明渐变，污染键控边缘又打爆调色板 |
| 背景渐变 / 纹理 | 直接摧毁色键的双峰分布前提（ADR-004） |
| 脱体特效（仅 attack/cast） | 包围盒远大于 idle，脚线锚点与缩放基准双双失准 |
| 多余的 45° 转身 | 见 [animation-rules.md](animation-rules.md) 第 9 条 |

---

## 逐帧姿势必须写死

初版只写 `one complete walk cycle` + `exactly 8 distinct poses`，
实测拿到**八张几乎一样的站立姿势**（相邻帧差异 8/24/26/8/3/30/26/7，毫无节奏）。

把每一格该画什么写死之后才拿到真正的循环（26/13/25/9/25/12/23/9，规整交替）。

**结论：整体描述模型不会自己拆解成具体姿势。** 找不到动作模板时要报错，
不要退回泛泛描述兜底——那等于把已知失败模式请回来。

节拍如何按帧数展开见 [animation-rules.md](animation-rules.md)。

---

## seed 与动作用不同的编译入口

- `compile_seed_prompt`：单幅立绘，身份细节要说满——后续每个动作网格都靠模型
  从这张图里读出这些细节。
- `compile_animation_prompt`：动作网格，身份靠参考图 + anchor sheet 承载，
  文字重点在姿势与连续性。

两者共用风格块、背景块与负面清单。
