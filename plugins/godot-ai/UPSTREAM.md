# 上游：hi-godot/godot-ai

| | |
|---|---|
| 仓库 | https://github.com/hi-godot/godot-ai |
| 许可 | MIT（`LICENSE` 原样保留） |
| Fork 自 | `d602a78322c66a24969f37f261e0364465cff464` |
| Fork 日期 | 2026-07-29 |
| 上游版本 | 3.0.7 |

## 内置了什么

- `addons/godot_ai/` —— Godot 编辑器插件，原样复制自上游 `plugin/addons/`
- `LICENSE` —— 原样复制

## 没有内置什么，以及为什么

**Python MCP server 不在这里。** 它是一个带 `fastmcp` 依赖的 PyPI 包
（`godot-ai`），有自己的 CI 与发版节奏，复制进来等于分叉，还要替它背 bug。

更根本的原因是**架构**：godot-ai 的 MCP 是 **HTTP 服务**
（`http://127.0.0.1:8000/mcp`），由 Godot 编辑器里的 addon 拉起 ——
不是能被 `.mcp.json` 直接 spawn 的 stdio 命令。所以运行时永远是
"用户在 Godot 里启动、我们连上去"，这一条不是选择。

`.mcp.json` 声明的就是这个 HTTP 连接。

## 我们改了什么

- **新增** `.claude-plugin/plugin.json` 与 `.mcp.json`

`addons/` 下的文件**一个字都没动**。

## 用户要做什么

1. 装 [uv](https://docs.astral.sh/uv/)（Python server 用）
2. 把 `addons/godot_ai/` 复制进自己的 Godot 项目的 `addons/`
3. 在 Godot 里启用插件，它会拉起 MCP server
4. Claude Code 通过本 plugin 的 `.mcp.json` 连上去

需要 Godot 4.5+（推荐 4.7+）。

## 同步上游

```bash
git clone --depth 1 https://github.com/hi-godot/godot-ai.git /tmp/ga
rm -rf plugins/godot-ai/addons
cp -r /tmp/ga/plugin/addons plugins/godot-ai/addons
cp /tmp/ga/LICENSE plugins/godot-ai/LICENSE
# 然后更新本文件的 commit 与日期
```
