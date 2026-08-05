# 数据库表结构

**这是数据结构的唯一真源。** 任何地方（Pydantic schema、TypeScript interface、SSE 事件字段）出现同名字段，必须与此处一致。

SQLite + SQLAlchemy 2.0 async + Alembic。

## 通用约定

### ID 规范

格式 `<前缀>_<base62 12位>`，如 `ses_7bK2mQ9xR4Lp`。

| 前缀 | 表 |
| --- | --- |
| `ses_` | session |
| `msg_` | message |
| `run_` | run |
| `spn_` | span |
| `todo_` | todo |
| `skl_` | skill_meta（如落库） |
| `prv_` | provider |
| `mdl_` | model |
| `bnd_` | model_binding |
| `mem_` | memory |
| `att_` | attachment |
| `wsp_` | workspace |
| `pth_` | path_whitelist |

**为什么带前缀**：日志里看到一个 ID 立刻知道是什么，不用回去查上下文。跨表误用（把 session_id 传给了要 message_id 的地方）在开发阶段就能看出来。

**为什么不用自增整数**：前端和 URL 里出现连续整数容易误操作（改个数字就到了别的记录）；且合并两个库时会主键冲突。

**为什么不用 UUID**：36 字符太长，日志和调试时占屏。base62 12 位约 71 bit 随机性，单人项目远够。

### 时间戳

所有时间字段：**INTEGER，UTC 毫秒**，字段名 `_at` 结尾。

不用 SQLite 的 `DATETIME`（它实际是文本，时区语义模糊），不用秒（前端 JS 天然用毫秒，转换是多余的出错点）。

统一通过 `core.time.now_ms()` 获取。

### 通用字段

所有表都有：

```python
class TimestampMixin:
    created_at: int   # now_ms()
    updated_at: int   # now_ms()，每次更新时刷新
```

**不做软删除。** `deleted` 字段只在企业审计场景下有意义。个人项目里软删除只带来"每个查询都要记得加 `WHERE deleted=0`"的负担，忘一次就出 bug。真删。

例外：`todo` 有 `archived_at`（验收关闭后归档但可查历史）。

### 枚举存字符串

`status`、`role`、`priority` 这类字段存字符串而非整数。直接 `SELECT` 就能读懂，排查时不用查映射表。

## session

```sql
CREATE TABLE session (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '',
    workspace_id    TEXT NOT NULL,
    pinned          INTEGER NOT NULL DEFAULT 0,
    approval_mode   TEXT NOT NULL DEFAULT 'manual',   -- manual | auto
    private_mode    INTEGER NOT NULL DEFAULT 0,
    amnesia_mode    INTEGER NOT NULL DEFAULT 0,
    vision_mode     INTEGER NOT NULL DEFAULT 0,
    last_message_at INTEGER NOT NULL DEFAULT 0,
    message_count   INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES workspace(id)
);
CREATE INDEX idx_session_list ON session(pinned DESC, last_message_at DESC);
```

### 四个模式开关是会话级的

不是全局的。不同会话可以有不同信任级别 —— 在测试目录里的会话开 `auto`，在真实项目里的保持 `manual`。

### last_message_at 与 message_count 是冗余字段

它们可以从 `message` 表算出来，但会话列表需要按最后活动时间排序并显示条数。每次列表查询都做子查询聚合，在几百个会话时就明显变慢。

**代价**：写消息时必须同步更新这两个字段。放在同一个事务里。

### title 默认空串

首轮对话结束后由 `title` 功能位的模型生成，发 `title` 事件。空串时前端显示"新会话"。

## message

```sql
CREATE TABLE message (
    id            TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    role          TEXT NOT NULL,     -- user|assistant|tool|system|summary|artifact
    agent_name    TEXT NOT NULL DEFAULT '',
    content       TEXT NOT NULL DEFAULT '',
    reasoning     TEXT,              -- 推理模型的思维链
    tool_calls    TEXT,              -- JSON: [{id, name, arguments}]
    tool_call_id  TEXT,              -- role=tool 时对应的 call id
    tool_name     TEXT,              -- role=tool 时的工具名
    tool_display  TEXT,              -- JSON: ToolResult.display
    is_error      INTEGER NOT NULL DEFAULT 0,
    refs          TEXT,              -- JSON: 引用清单 [{type, path/href, ...}]
    attachments   TEXT,              -- JSON: [attachment_id]
    artifact_kind TEXT,              -- role=artifact 时: file|code|doc
    artifact_path TEXT,
    run_id        TEXT,
    span_id       TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES session(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX idx_message_seq ON message(session_id, seq);
CREATE INDEX idx_message_session ON message(session_id, seq);
CREATE UNIQUE INDEX idx_message_artifact
    ON message(session_id, agent_name) WHERE role = 'artifact';
```

### seq 为什么必须存在

排序不能靠 `id`，也不能靠 `created_at`。

- **靠 id**：随机 base62 字符串的字典序与生成顺序无关。
- **靠 created_at**：同一毫秒内可能产生多条（assistant + 三个 tool 结果），毫秒级时间戳无法区分顺序。

`seq` 在会话内严格递增，是唯一可靠的排序依据。分配方式：`SELECT MAX(seq)+1 WHERE session_id=?`，在事务内做。

### agent_name 实现记忆线隔离

空串 = 用户可见主线；有值 = 该智能体私有线。见 [../01-architecture/agents.md](../01-architecture/agents.md#智能体的记忆线)。

组装上下文时按 `(session_id, agent_name)` 过滤。前端默认只查 `agent_name = ''`。

### artifact 的唯一索引

```sql
CREATE UNIQUE INDEX idx_message_artifact
    ON message(session_id, agent_name) WHERE role = 'artifact';
```

SQLite 支持部分索引（`WHERE` 子句），这点和 Postgres 一样。用它保证每个 `(session, agent)` 只有一条 artifact。

写入用 upsert：先删旧的再插新的，同事务。

**注意**：SQLite 的部分唯一索引对 NULL 的处理与 Postgres 一致（NULL 互不相等）。所以 `agent_name` 必须 `NOT NULL DEFAULT ''`，不能允许 NULL，否则主线的多条 artifact 都能插进去。

### refs 存清单不存内容

`refs` 只存引用清单（`[{"type":"file","path":"src/main.py"}]`），不存展开后的文件内容。

理由见 [../01-architecture/context.md](../01-architecture/context.md#引用内容不落进-messages-的持久内容)：内容按需重读，历史记录小，文件改了自动看到新版。

### reasoning 单独存

推理模型的思维链和正文分开。前端折叠显示，且**不发回给 LLM**（下一轮请求不带 reasoning，多数 API 也不接受）。

## workspace

```sql
CREATE TABLE workspace (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    root_path   TEXT NOT NULL,
    is_default  INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE UNIQUE INDEX idx_workspace_path ON workspace(root_path);
```

首次启动时自动建一条默认工作区，指向 `<项目>/workspace/`。

`root_path` 存绝对路径。工作区被添加时自动加进路径白名单。

## todo

```sql
CREATE TABLE todo (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    content     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|in_progress|completed|cancelled
    priority    TEXT NOT NULL DEFAULT 'medium',   -- high|medium|low
    order_index INTEGER NOT NULL DEFAULT 0,
    archived_at INTEGER,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES session(id) ON DELETE CASCADE
);
CREATE INDEX idx_todo_session ON todo(session_id, archived_at, order_index);
```

"一个会话同时只有一个 `in_progress`"靠应用层保证，不做数据库约束 —— SQLite 的部分唯一索引无法表达"某个值最多出现一次"。见 [../01-architecture/todo.md](../01-architecture/todo.md#一个会话同时只能有一个-in_progress)。

`archived_at` 非空表示已验收关闭。查询当前清单时过滤 `archived_at IS NULL`。
