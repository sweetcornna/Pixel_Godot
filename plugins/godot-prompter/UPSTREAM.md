# 上游：jame581/GodotPrompter

| | |
|---|---|
| 仓库 | https://github.com/jame581/GodotPrompter |
| 许可 | MIT（`LICENSE` 原样保留） |
| Fork 自 | `4aec8bdf3fbfce6b19181cab1bbee75fefc85914` |
| Fork 日期 | 2026-07-28 |
| 上游版本 | 1.13.0 |

## 内置了什么

- `skills/` —— 55 个 Godot 4.x 领域 Skill，原样复制
- `agents/` —— 9 个专职 Agent，原样复制
- `LICENSE` —— 原样复制

## 我们改了什么

- **新增** `.claude-plugin/plugin.json`。上游用的是 Antigravity 的
  `plugin.json` schema（`$schema: antigravity.google/...`、`contextFileName: GEMINI.md`），
  Claude Code 认不了，所以另写一份，**没有改动上游那份**。

`skills/` 与 `agents/` 下的文件**一个字都没动**。

## 同步上游

改动只做加法，不改上游既有文件 —— 所以同步就是重新复制：

```bash
git clone --depth 1 https://github.com/jame581/GodotPrompter.git /tmp/gp
rm -rf plugins/godot-prompter/skills plugins/godot-prompter/agents
cp -r /tmp/gp/skills /tmp/gp/agents plugins/godot-prompter/
cp /tmp/gp/LICENSE plugins/godot-prompter/LICENSE
# 然后更新本文件的 commit 与日期
```
