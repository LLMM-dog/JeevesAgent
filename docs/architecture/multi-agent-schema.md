# 多智能体系统 — 数据库 Schema v2

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

    -- 验证增强（v2 新增 — 每智能体独立）
    verification_enabled INTEGER NOT NULL DEFAULT 0,
    strict_mode           INTEGER NOT NULL DEFAULT 0,  -- 1=不通过时阻止继续

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

## memory 表改动

```sql
-- v2 新增
ALTER TABLE memory ADD COLUMN agent_id TEXT;

-- 查询示例
-- 用户 profile（跨智能体）
SELECT * FROM memory WHERE target='user';

-- 智能体「代码审查员」的跨会话记忆
SELECT * FROM memory WHERE target='agent' AND agent_id='adf_xxx';

-- 当前会话中「代码审查员」的记忆
SELECT * FROM memory WHERE target='session'
  AND session_id='ses_xxx' AND agent_id='adf_xxx';
```

### 验证智能体的记忆 ID

```sql
-- 验证智能体的记忆条目。agent_id 用特殊前缀区分
INSERT INTO memory (target, agent_id, content)
VALUES ('agent', 'adf_xxx__verification', '该智能体常犯的错误：...');
-- 双下划线 __verification 后缀表示这是验证智能体的记忆
```

---

## sessions 表改动

```sql
ALTER TABLE sessions ADD COLUMN agent_id TEXT;
-- NULL = 使用默认智能体
-- 恢复会话时重新绑定同一个智能体定义
```

---

## skills 目录约定（文件系统，不在数据库）

```
skills/
├── code-review/              ← 系统 skill
│   └── SKILL.md
├── <agent_name>/             ← 智能体私有 skill 目录
│   ├── SKILL.md              ← Agent 自己创建的 skill
│   └── verification/         ← 验证智能体的 skill 目录
│       └── SKILL.md
```

智能体调用 `skill_manage(action="create", name="x")` 时，后端自动创建在 `skills/<agent_name>/<name>/SKILL.md`。删改同理，只能在 `skills/<agent_name>/` 下操作。
