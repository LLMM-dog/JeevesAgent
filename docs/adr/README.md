# 架构决策记录 (ADR)

本目录记录 Jeeves 项目的关键架构决策。每条记录包含 Context（背景）→ Decision（决策）→ Consequences（后果）→ Status（状态）。

ADRs are the project's long-term memory. Never delete old ADRs; mark them `Status: superseded by ADR-NNNN` instead.

| # | 标题 | 状态 |
|---|------|------|
| [0001](0001-pure-while-loop-instead-of-langgraph.md) | 纯 while 循环替代 LangGraph Agent | Accepted |
| [0002](0002-sqlite-sqlalchemy-alembic.md) | SQLite + SQLAlchemy + Alembic | Accepted |
| [0003](0003-web-ui-not-tui.md) | Web UI 而非 TUI | Accepted |
| [0004](0004-pluggable-sandbox.md) | 可插拔沙箱（Local + Docker） | Accepted |
| [0005](0005-full-stack-snake-case.md) | 全栈 snake_case | Accepted |
| [0006](0006-core-infra-modules-layering.md) | core/infra/modules 三层 + 垂直切分 | Accepted |

## 格式

```markdown
# ADR-NNNN: 简短标题

## Context
为什么需要做这个决策。已有的约束、相关事实。

## Decision
做了什么决策，怎么选的。

## Consequences
正面 + 负面 + 缓解措施。

## Status
Proposed | Accepted | Deprecated | Superseded by ADR-NNNN

## References
相关文档链接。
```

## 什么时候写 ADR

- 选择了某种技术方案而非另一种，且有明确理由
- 改变模块边界或数据模型
- 推翻之前的某个决策
- 引入新的全局约束
