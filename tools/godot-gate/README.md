# Godot 加载门槛

`.tres` 的结构断言（`tests/integration/test_export.py`）证明不了「Godot 能加载」。
这个目录是真机判据。

## 跑

```bash
# 1. 下载 Godot 4（本仓库不附带二进制）
curl -L -o godot.zip https://github.com/godotengine/godot/releases/download/4.3-stable/Godot_v4.3-stable_linux.x86_64.zip
python3 -c "import zipfile; zipfile.ZipFile('godot.zip').extractall('.')"
chmod +x Godot_v4.3-stable_linux.x86_64

# 2. 导出资产并把产物放进本目录
pixel-asset export <资产目录> -t godot
cp <资产目录>/exports/godot/* .

# 3. 写 expected.json（动画名 → frames / fps / loop / w / h），见下
# 4. 跑
./Godot_v4.3-stable_linux.x86_64 --headless --import
./Godot_v4.3-stable_linux.x86_64 --headless --script verify.gd
```

`GATE-OK 全部通过` 才算过。脚本里 `knight_01` 是硬编码的资产名，换资产时改掉。

`expected.json` 直接从 Manifest 生成：

```python
import json
m = json.load(open("<资产目录>/asset-manifest.json"))
w, h = m["canvas"]["width"], m["canvas"]["height"]
json.dump({
    k: {"frames": len(a["frames"]), "fps": a["fps"], "loop": a["loop"], "w": w, "h": h}
    for k, a in m["animations"].items() if a.get("frames")
}, open("expected.json", "w"))
```

## 验什么

**A. 资源本身** —— 动画名、帧数、fps、loop、每帧纹理尺寸。

**B. 衔接层的四条必设项**（见 `skills/pixel-asset-forge/references/godot-handoff.md`）。
这四条是我们实测出来、而 godot-ai 无从知道的事 —— 不设则接进去的节点
"看着能用、实际是坏的"：

1. 项目默认纹理过滤是 Nearest（线性过滤会把像素全糊掉）
2. `offset` 把 bottom-center 锚点对到节点原点
3. 一次性动作标成一次性、循环动作标成循环（决定要不要连 `animation_finished`）
4. `ext_resource` 指向的纹理真的在（`.tres` 与 png 必须整目录复制）

## 这个门槛抓得住什么

一个永远通过的门槛没有价值。实测验证过（改坏再跑）：

| 改动 | 结果 |
|---|---|
| 帧数少一帧 | ✓ 抓住 —— `GATE-FAIL attack_down 帧数 3 ≠ 4` |
| 纹理路径指向不存在的文件 | ✓ 抓住 —— Godot 直接 Parse Error |
| 把 png 移走 | ✓ 抓住 |
| `default_texture_filter` 0 → 1 | ✓ 抓住 —— `项目默认纹理过滤是 1，不是 Nearest(0)` |
| `load_steps` 22 → 3 | ✗ **抓不住，Godot 照样加载** |

最后一条是实测更正：`load_steps` 是给加载进度条用的提示，不是硬校验。
仓库里原本写着"数不对会让 Godot 加载失败"，那句话是错的。

## 为什么不进 CI

Godot 二进制 110 MB，且这条门槛只在导出器改动时才有意义。
改 `exporters/godot.py` 时手动跑一次。
