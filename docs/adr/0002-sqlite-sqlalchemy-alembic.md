# ADR-0002: SQLite + SQLAlchemy + Alembic

## Context

需要持久化会话、消息、Todo、模型配置、运行追踪。数据量以千条计。选择数据库系统时考虑了三个方向：纯文件（YAML/JSON）、SQLite、PostgreSQL。

## Decision

结构化数据走 SQLite + SQLAlchemy 2.0（async）+ Alembic。技能包、人设、MCP 配置走文件系统。

- SQLite 理由：单机项目不需要网络数据库；数据量在 SQLite 舒适区内；单文件备份方便
- SQLAlchemy 2.0 async 理由：与 FastAPI 的 async 生态一致；迁移用 Alembic
- 文件系统理由：技能包和人设需要手改、git diff、直接从别处 copy，存数据库反而别扭

## Consequences

- **正面**：零运维，备份即复制文件；迁移只加不删（有测试保证）
- **正面**：向量检索（如果做）用 `sqlite-vec`，不引独立向量库
- **负面**：不支持并发写入；WAL 模式下写入锁会阻塞读
- **缓解措施**：个人项目无并发，不是问题

## Status

Accepted (2026-08-07)

## References

- `docs/architecture/data-schema.md`
- `docs/guides/product.md` — 形态决策 3
