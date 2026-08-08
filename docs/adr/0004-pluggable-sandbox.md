# ADR-0004: 可插拔沙箱（Local + Docker）

## Context

代码执行需要安全边界。完全隔离（Docker）安全性好但重；本地执行人为审批但轻。

## Decision

定义统一的 `Sandbox` 接口，两个实现：

- **LocalSandbox**（默认）：子进程 + 工作区路径限制 + 超时 + 输出截断 + 审批模式。零依赖。
- **DockerSandbox**（可选）：`--network none` + 资源限制 + `cap-drop ALL`，只挂工作区。

检测不到 Docker 时自动降级到本地并明确告知。不装 Docker 也能全功能使用。

## Consequences

- **正面**：零依赖默认，装 Docker 即可真隔离
- **正面**：降级透明，不静默回落（持续提示）
- **负面**：需要维护两套实现
- **缓解措施**：统一 `Sandbox` Protocol，上层只 import port

## Status

Accepted (2026-08-07)

## References

- `docs/architecture/sandbox.md`
- `docs/guides/product.md` — 形态决策 2
