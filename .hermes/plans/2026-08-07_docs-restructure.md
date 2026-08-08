# Jeeves 文档规范化与对齐计划

> **基于**：Diátaxis + ADR 业界标准，保留项目现有编号体系。

**目标**：
1. 添加 `docs/adr/` 目录，迁移已有的架构决策
2. 修正已发现的不一致（LangGraph 描述、React 版本）
3. 规范化目录索引

---

## Task 1：修正已发现的不一致

### 1a: README.md — LangGraph 描述

当前：第 5 行和第 378 行写"FastAPI + LangGraph"
实际：agent loop 是纯 `while` 循环，LangGraph 仅作为依赖保留（ToolNode 等），等待 M6 确认后移除
修正：改为"FastAPI（基于 LangGraph 组件）"

### 1b: docs/04-frontend/architecture.md — React 版本

当前：第 7 行写 "React 18"
实际：`frontend/package.json` 为 React 19.2.0
修正：改为 "React 19"

### 1c: docs/README.md — LangGraph 描述

当前：第 3 行 "Python 后端（FastAPI + LangGraph）+ React 前端"
修正：同 1a

---

## Task 2：创建 `docs/adr/` 目录

从现有文档提取架构决策记录。每个 ADR 格式：Context → Decision → Consequences → Status

### ADR-0001: 纯 while 循环替代 LangGraph Agent
来源：`docs/01-architecture/agent-loop.md`

### ADR-0002: SQLite + SQLAlchemy + Alembic
来源：`docs/00-overview/product.md` 形态决策 3

### ADR-0003: Web UI 而非 TUI
来源：`docs/00-overview/product.md` 形态决策 1

### ADR-0004: 可插拔沙箱（Local + Docker）
来源：`docs/00-overview/product.md` 形态决策 2

### ADR-0005: 全栈 snake_case
来源：`docs/README.md` 全局硬约束、`docs/04-frontend/architecture.md`

### ADR-0006: core/infra/modules 三层 + 垂直切分
来源：`docs/01-architecture/backend.md`

---

## Task 3：添加 docs/adr/README.md 索引

---

## Task 4：更新 docs/README.md

- 添加 ADR 目录到索引表
- 修正 LangGraph 描述
