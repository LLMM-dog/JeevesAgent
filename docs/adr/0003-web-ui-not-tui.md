# ADR-0003: Web UI 而非 TUI

## Context

AI 助手需要交互界面。TUI 启动快、适合纯写代码场景；Web UI 支持更丰富的交互。

项目需要的能力包括：Todo 看板、技能管理面板、图片预览、气泡树可视化、上下文占用条。这些在终端里都会打折。

## Decision

选择 Web UI：FastAPI 后端 + React/Vite 前端。

## Consequences

- **正面**：丰富的 UI 组件生态（shadcn/ui、react-virtuoso 等）；浏览器访问 `localhost` 即可
- **正面**：上下文占用条、气泡树等复杂交互在 Web 上实现成本低
- **负面**：需要维护前后端两套代码
- **缓解措施**：全栈 snake_case 消除数据对齐成本；共用类型定义

## Status

Accepted (2026-08-07)

## References

- `docs/guides/product.md` — 形态决策 1
- `docs/architecture/backend.md`
