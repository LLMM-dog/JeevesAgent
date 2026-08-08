# ADR-0001: 纯 while 循环替代 LangGraph Agent

## Context

最初设计使用 LangGraph 的 StateGraph 作为 agent 主循环。实现过程中发现 LangGraph 的三项核心机制在本项目全部用不上：

- **状态自动提交 + reducer**：压缩要整体重写 `messages`（删中段、插摘要），reducer 只能追加。为此在 5 个返回分支全部用 `Overwrite` 显式关掉 reducer，并额外需要一个 `messages_persist` 节点专门落库
- **checkpointer 恢复状态**：项目从 DB 重组 + `repair_tool_pairing` 无条件修一遍，用不上 checkpointer
- **回调与 trace 体系**：事件全部走自己的 `EventBus` 显式 emit，LangGraph 的 `astream_events` hook 反而被关了

四个参考实现（open-interpreter、aider、openclaw、claude-code）中，只有 claude-code 用了多智能体，且用的是独立 `asyncio.Task` + 队列，没用子图/Send。

## Decision

Agent 主循环改为纯 `while` 循环：

```
load_context → reason → 有 tool_calls? → act → 回到 reason
                       → 无 → 返回 final
```

保留 `messages`（工作副本）和 `journal`（append-only 流水）分离。保留立即落库策略（不等循环结束）。

## Consequences

- **正面**：代码量减少，调试成本降低，不再需要为 reducer 写绕过代码
- **负面**：失去 LangGraph 的图可视化（`draw_mermaid_png`）
- **保留**：`langgraph` 依赖仍在 `pyproject.toml` 中，ToolNode 等组件仍在使用，等 M6 确认后移除

## Status

Accepted (2026-08-07)

## References

- `docs/architecture/agent-loop.md`
- `backend/app/modules/agent/loop.py`
