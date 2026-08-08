# Jeeves 改进路线图

> 基于 2026-08 对 7 个知名 Agent 项目（Pi / OpenCode / Claude Code / Aider / Goose / Codex CLI / Hermes）的源码级调研。
> 详细调研见 `docs/research/harness/`。

## 已完成（本轮）

| 项目 | 说明 |
|------|------|
| ✅ 钩子系统 | `ToolRegistry.hooks` — BEFORE_TOOL / AFTER_TOOL 两个拦截点。`backend/app/modules/agent/hooks.py` |
| ✅ start.bat 一键启动 | 首次自动跑 setup，双击即用。`setup.bat` 已删除 |
| ✅ 启动后自动打开浏览器 | 等后端就绪后自动 `Start-Process` 打开 `http://127.0.0.1:9000` |
| ✅ start.ps1 移入 scripts/ | 根目录只剩 `start.bat` 和 `start.sh`，用户不困惑 |
| ✅ CI 修复 | 加密密钥改为合法 Fernet key；SPA 测试加 `@needs_dist` 跳过；死链修复 |

## 短期（下次迭代）

> 详细方案见 `docs/research/harness/jeeves-plan.md`

| 优先级 | 项目 | 改动量 | 参考 |
|--------|------|--------|------|
| 🔴 P0 | Plan/Build 双阶段 | ~50 行（利用已有钩子系统） | OpenCode agent 体系 |
| 🔴 P0 | 结构化压缩模板 | ~30 行（只改 prompt） | Pi 6-section / OpenCode 5-section |
| 🔴 P0 | 自动验证闭环 | ~80 行 | Aider lint→test→feedback |
| 🟡 P1 | 工具输出截断+文件卸载 | ~60 行 | OpenCode 50KB 截断 |
| 🟡 P1 | 分层规则体系 | ~100 行 | Claude Code CLAUDE.md 层级

## 中期

| 优先级 | 项目 | 说明 | 参考 |
|--------|------|------|------|
| 🟢 P3 | 权限分层 | 从当前 manual/auto 两档演进到工具分类（read/write/shell/network）× allow/ask/deny + glob 路径 | Claude Code 7 层权限 |
| 🟢 P3 | MCP 延迟发现 | MCP 启动时只加载工具名，需要时再加载 schema，节省 context | Claude Code ToolSearch |
| 🟢 P3 | RepoMap 式智能 Context | Tree-sitter + PageRank 符号排序，大仓库只注入关键符号 | Aider RepoMap |
| 🟢 P3 | Artifact 持久化 | 产物跨会话保留，类似"工作台"概念 | — |

## 不做

以下是从调研中明确排除的方向：

| 项目 | 原因 |
|------|------|
| LangGraph / 图编排 | ADR-0001：纯 while 循环已经够用，图结构增加调试成本无收益 |
| PEV 多阶段架构 | 模型自己会规划，固化成节点反而限制它 |
| TUI 界面 | ADR-0003：Web UI 比终端更适合非技术用户 |
| MCP 作为唯一扩展机制 | 当前 Protocol 工具系统比 MCP 更灵活，MCP 作为接入层即可 |

## 不做的理由

每次决定"不做"都有明确原因：

- **LangGraph**：四个常见实现全都绕开了它的核心机制（reducer / checkpointer / trace）。我们自己也验证过——`messages` 压缩要整体重写、`journal` 必须 append-only、事件走自己的 EventBus。详见 `docs/adr/0001-pure-while-loop-instead-of-langgraph.md`。
- **PEV 架构**：Codex CLI 的 Plan-Execute-Verify 需要 `update_plan` 工具来维护步骤状态，增加了 prompt 复杂度。而实践证明模型在单轮里自己就能规划——给它 while 循环 + 工具反馈，它会自然收敛。
- **TUI**：Jeeves 的目标用户不是 CLI 重度用户。Web UI 的 markdown 渲染、代码高亮、拖拽上传、设置面板是 TUI 做不出来的。
- **全量 MCP**：Goose 把所有扩展都绑在 MCP 上，但 MCP 的工具描述质量参差不齐，且每次连接都要起子进程。当前 Protocol 工具系统更可控。

---

## 调研产出

`docs/research/harness/` 目录（总计 ~280KB 源码级分析）：

| 文件 | 内容 |
|------|------|
| `README.md` | 调研目录索引 + 项目概况 |
| `overview.md` | 9 维度 × 8 项目对比矩阵 + 最佳实践排名 |
| `pi.md` | Pi Agent 工具包：双层循环、compaction 算法、token 估算、hooks |
| `opencode.md` | OpenCode：6 阶段工具流水线、20+ 插件钩子、Agent 配置系统 |
| `claude-code.md` | Claude Code：7 层权限、5 层 context 管理、MCP ToolSearch |
| `aider.md` | Aider：SEARCH/REPLACE 硬约束、RepoMap PageRank、Architect/Editor |
| `goose.md` | Goose：State Machine 编排、Stop Hook 死循环保护、安全三层 |
| `codex-cli.md` | Codex CLI：PEV 架构、沙箱云执行、Chrome 扩展、Goals 系统 |
| `hermes-agent.md` | Hermes：11 层 system prompt、Ephemeral Scaffolding、Threat Scanner |
| `jeeves-improvements.md` | 对 Jeeves 的具体改进建议（含已完成标记） |
