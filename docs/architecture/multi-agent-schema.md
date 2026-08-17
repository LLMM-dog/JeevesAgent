# 多智能体系统 — 数据库 Schema v2

> 状态：agent_defs 已实现（`backend/app/modules/agent/models.py`）。agent_groups 表尚未实现，见 [multi-agent-roadmap.md](multi-agent-roadmap.md)。

---

## agent_defs

```sql
CREATE TABLE agent_defs (
    id          TEXT PRIMARY KEY,        -- "adf_7bK2mQ9xR4Lp"
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    avatar      TEXT,

    -- 行为
    system_prompt TEXT NOT NULL DEFAULT '',
    model_id    TEXT,                    -- NULL=跟随对话设置

    -- 能力
    skill_names  TEXT NOT NULL DEFAULT '[]',   -- ["code-review", "security-audit"]
    mcp_servers  TEXT NOT NULL DEFAULT '[]',   -- ["github"]

    -- 权限
    permission_read      INTEGER NOT NULL DEFAULT 1,
    permission_write     INTEGER NOT NULL DEFAULT 0,
    permission_shell     INTEGER NOT NULL DEFAULT 0,
    permission_network   INTEGER NOT NULL DEFAULT 0,
    permission_subagent  INTEGER NOT NULL DEFAULT 0,

    -- 验证增强（v2 — 每智能体独立，默认关闭）

    -- 系统字段
    hidden     INTEGER NOT NULL DEFAULT 0,   -- 1=不在选择列表中显示
    max_turns  INTEGER,                      -- NULL=使用全局默认

    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    deleted_at  INTEGER
);
```

### 权限字段为什么不用 JSON

五个布尔字段而非一个 JSON 列：`permission_read = 0` 可以直接在 WHERE 里过滤，不需要解析 JSON。五个字段未来也不会再增加（Agent 的能力边界已确定）。

---

## agent_groups

不变。见 v1 schema。

---

## agent_group_members

不变。见 v1 schema。

---

## 记忆隔离

记忆改用文件记忆系统（`memory/` 模块），按 `agent_id` 目录隔离 —— 每个智能体有自己的记忆目录，天然隔离，不需要给表加 `agent_id` 列。

验证智能体是独立实例，有自己的记忆目录（`data/memory/<agent_id>/`）。

---

## sessions 表改动

```sql
ALTER TABLE sessions ADD COLUMN agent_id TEXT;
-- NULL = 使用默认智能体
-- 恢复会话时重新绑定同一个智能体定义
```

---

## skills 目录约定（分阶段实现）

**Phase 1（当前）**：skills 统一存放在 `skills/` 下，智能体只能引用已有 skill，不能自建。

**Phase 2（远期）**：智能体自管理 skills，目录按 agent_id 隔离。

```bash
skills/
├── code-review/              ← 系统 skill（Phase 1 可用）
│   └── SKILL.md
├── <agent_id>/               ← Phase 2: 智能体私有 skill 目录
│   └── <skill_name>/
│       └── SKILL.md
```

Phase 2 需要先实现：curator（自动维护）、pinned（保护重要 skill）、approval（用户审批模型创建的 skill）、MCP 服务器隔离。
