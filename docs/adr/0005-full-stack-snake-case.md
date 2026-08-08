# ADR-0005: 全栈 snake_case

## Context

前后端数据序列化涉及 Python（snake_case 原生）和 TypeScript（camelCase 社区惯例）。两种选择：做转换层（camelCase ↔ snake_case）或统一 snake_case。

## Decision

全栈 snake_case：数据库列、Python 字段、JSON key、TypeScript interface 字段全部 snake_case。不做 camelCase 转换。

理由：
- 不需要维护映射逻辑
- 没有性能开销
- 带 `_` 的字段在前端一目了然是后端来的
- API 无鉴权，不考虑公开 API 的惯例问题

## Consequences

- **正面**：零转换层，前后端数据结构直接对齐
- **正面**：视觉区分后端数据（`msg.agent_name`）与前端数据（`messageItemProps`）
- **负面**：不符合 TypeScript 社区惯例
- **缓解措施**：纯前端的东西（组件 props、hook 返回）仍用 camelCase；只在 API 类型定义中用 snake_case

## Status

Accepted (2026-08-07)

## References

- `docs/README.md` — 全局硬约束
- `docs/architecture/architecture.md` — 为什么不用 camelCase
- `docs/development/conventions.md` — 命名规范
