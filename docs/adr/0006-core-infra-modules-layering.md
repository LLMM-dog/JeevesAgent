# ADR-0006: core/infra/modules 三层 + 垂直切分

## Context

传统的 api/service/model/schema 水平分层在 agent 项目里会导致：一个功能拆到四个目录，改一处要跳四个文件；而 agent 项目的变更几乎总是"整个功能一起动"。

## Decision

采用 **core / infra / modules 三层 + 模块内垂直切分**：

```
backend/app/
  core/       # 无外部依赖的通用能力（config, ids, events, crypto, pathguard...）
  infra/      # 外部依赖的适配器，一律 port + impl（llm, sandbox, mcp, websearch...）
  modules/    # 业务模块，每个含 models/schemas/service/router
```

依赖方向单向：`modules → infra → core`。core 不许 import infra 或 modules；infra 不许 import modules。

## Consequences

- **正面**："找实现"有唯一答案——代码位置跟着表和 service 走
- **正面**：core 层全部可单元测试，无需 mock
- **正面**：依赖方向明确，不会出现循环引用
- **负面**：新人需要理解三层含义

## Status

Accepted (2026-08-07)

## References

- `docs/architecture/backend.md`
