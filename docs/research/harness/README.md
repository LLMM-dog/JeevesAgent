# 驾驭工程（Harness Engineering）开源项目调研

> 调研时间：2026-08-08
> 调研目的：为 Jeeves 项目的驾驭工程优化提供参考

## 什么是驾驭工程

驾驭工程（Harness Engineering）是将 LLM 转化为可靠生产级 Agent 的系统工程学科。它关心模型**之外**的一切：

$$H = (E,\ T,\ C,\ S,\ L,\ V)$$

| 组件 | 中文 | 职责 |
|------|------|------|
| **E** | 执行循环 (Execution Loop) | Agent 推理循环：规划→行动→观察→重复 |
| **T** | 工具注册 (Tool Registry) | 注册、验证、调度 Agent 可调用的工具 |
| **C** | 上下文管理 (Context Manager) | 每步 LLM 看到的信息 |
| **S** | 状态存储 (State Store) | 跨轮次/会话的持久记忆 |
| **L** | 生命周期钩子 (Lifecycle Hooks) | 前置/后置拦截、护栏、验证器 |
| **V** | 评估接口 (Evaluation Interface) | 输出验证、评分、改进 |

三层架构：**信息层**（Agent 看到什么）→ **执行层**（工作如何完成）→ **反馈层**（系统如何改进）

## 调研对象

| 项目 | 定位 | Stars | 语言 | 核心特点 |
|------|------|-------|------|----------|
| [Pi](./pi.md) | 极简 Agent 工具包 | 82K | TypeScript | 极简 agent loop + TUI + skills 渐进式加载 |
| [OpenCode](./opencode.md) | 全栈开源编码 Agent | 194K | TypeScript | C/S 架构 + 多 Agent + 20+ 钩子插件系统 |
| [Claude Code](./claude-code.md) | Anthropic 官方编码 Agent | - | TypeScript | 7 层权限体系 + MCP + hooks + 子 Agent |
| [Aider](./aider.md) | 老牌 CLI 结对编程 | 35K+ | Python | RepoMap + 4 种编辑模式 + Git 原生 |
| [Goose](./goose.md) | Block 开源可扩展 Agent | 15K+ | Rust | MCP 原生 + 多界面 + 多 Provider |
| [Cline/Roo Code](./cline-roocode.md) | VS Code Agent 扩展 | 20K+ | TypeScript | IDE 内 Agent + MCP + 云端 Agent |
| [Codex CLI](./codex-cli.md) | OpenAI 官方编码 CLI | 91K | Rust | 沙箱执行 + Chrome 扩展 + goals 系统 |
| [Hermes Agent](./hermes-agent.md) | 本项目的驾驭底座 | - | TypeScript + Python | Skills + Memory + delegate_task + config 系统 |

## 调研维度

每个项目的调研覆盖以下驾驭工程维度：

1. **Agent Loop** — 执行循环如何设计（ReAct、PEV、多阶段）
2. **工具系统** — 工具注册/发现/调度/权限模型
3. **上下文管理** — Context 构建/压缩/渐进式加载策略
4. **权限与安全** — 沙箱/权限层级/护栏机制
5. **可扩展性** — Skills / MCP / Hooks / Plugins / 自定义工具
6. **多 Agent 编排** — 子 Agent 委派/并行/多角色协调
7. **会话管理** — Session 持久化/分支/fork/恢复
8. **开发者体验** — 配置方式/CLI/文档/调试能力

## 项目间关系

```
驾驭工程成熟度光谱:
  工具增强 ←──────────────────────────→ 全自主 Agent
    |                                        |
  Aider                           Claude Code / Codex CLI
  (人在回路,               (自主规划+执行,
   Git 驱动)                 多 Agent 编排)

  平台化 ←────────────────────────────→ 垂直整合
    |                                        |
  OpenCode / Goose                Claude Code / Codex CLI
  (75+ Provider,                 (绑定自家模型,
   任意模型)                      深度优化)

  极简 ←──────────────────────────────→ 功能完备
    |                                        |
  Pi                               OpenCode / Hermes Agent
  (最小核心+扩展,                 (全栈 C/S + Plugin +
   教育友好)                      Skills + Memory)
```
