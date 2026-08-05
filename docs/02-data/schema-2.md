# 数据库表结构（续）

接 [schema.md](schema.md)。通用约定见该文件。

## provider

```sql
CREATE TABLE provider (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    base_url        TEXT NOT NULL,
    api_key_cipher  TEXT NOT NULL,     -- Fernet 密文，带 v1: 前缀
    key_hint        TEXT NOT NULL,     -- 尾 4 位，如 "a3f9"
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_probe_at   INTEGER,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE UNIQUE INDEX idx_provider_name ON provider(name);
```

**没有明文列。** 任何 API 响应都只返回 `key_hint`。见 [../01-architecture/providers.md](../01-architecture/providers.md#api-key-存储)。

`base_url` 存的是**规范化后**的值（补齐 `/v1`、去尾斜杠）。规范化在 service 层做，不在数据库层。

## model

```sql
CREATE TABLE model (
    id              TEXT PRIMARY KEY,
    provider_id     TEXT NOT NULL,
    model_id        TEXT NOT NULL,     -- 供应商侧的模型标识，如 "deepseek-chat"
    display_name    TEXT NOT NULL DEFAULT '',
    context_window  INTEGER NOT NULL DEFAULT 32768,
    window_source   TEXT NOT NULL DEFAULT 'default',  -- matched|manual|default
    supports_vision TEXT NOT NULL DEFAULT 'unknown',  -- true|false|unknown
    supports_tools  TEXT NOT NULL DEFAULT 'unknown',
    vision_checked_at INTEGER,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    FOREIGN KEY (provider_id) REFERENCES provider(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX idx_model_unique ON model(provider_id, model_id);
```

### window_source 三态

| 值 | 含义 | UI 表现 |
| --- | --- | --- |
| `matched` | 从内置映射表匹配到 | 正常显示 |
| `manual` | 用户手动填的 | 显示"手动设置" |
| `default` | 匹配不到，用了默认 32768 | 显示"未知，按 32K 处理，建议手动设置" |

这个字段的价值：`default` 状态需要让用户知道，因为它直接影响压缩阈值。匹配不到却静默用默认值，会导致大窗口模型被过早压缩（用户觉得"怎么老是压缩"）或小窗口模型直接 400。

### supports_vision 是三态字符串不是布尔

`unknown` 状态必须存在：核验有成本（要发真实请求），不能对每个模型都跑一遍。未核验的模型标 `unknown`，允许用于 chat 但不允许开视觉模式。

见 [../01-architecture/providers.md](../01-architecture/providers.md#vision多模态)。

## model_binding

```sql
CREATE TABLE model_binding (
    id          TEXT PRIMARY KEY,
    agent_name  TEXT NOT NULL DEFAULT '',   -- '' 表示全局默认
    purpose     TEXT NOT NULL,              -- chat|vision|title|compact|embedding
    model_pk    TEXT NOT NULL,              -- 指向 model.id
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    FOREIGN KEY (model_pk) REFERENCES model(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX idx_binding_unique ON model_binding(agent_name, purpose);
```

字段名用 `model_pk` 而非 `model_id` —— 因为 `model.model_id` 已经表示"供应商侧的模型标识"，两个 `model_id` 会混。这是命名冲突的必要妥协。

`ON DELETE CASCADE`：模型删除时绑定自动消失。解析时找不到绑定会回落到 chat 位并发 `model_fallback` 事件，不会崩。

解析顺序见 [../01-architecture/agents.md](../01-architecture/agents.md#每个智能体可绑不同模型)。

## memory

```sql
CREATE TABLE memory (
    id          TEXT PRIMARY KEY,
    category    TEXT NOT NULL DEFAULT 'general',
    content     TEXT NOT NULL,
    source_session_id TEXT,
    hit_count   INTEGER NOT NULL DEFAULT 0,
    last_hit_at INTEGER,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX idx_memory_category ON memory(category, updated_at DESC);
```

跨会话的长期记忆。M5 阶段实现。

`hit_count` / `last_hit_at`：记忆条目会越积越多，需要淘汰依据。长期没被命中的条目可以提示用户清理。

**不做向量检索。** 个人项目的记忆条目量级在几百条，全量注入或按 category 过滤后注入即可。真的多到装不下再上 `sqlite-vec`。

`source_session_id` 不做外键 —— 会话删除后记忆应该保留。

## attachment

```sql
CREATE TABLE attachment (
    id          TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,        -- 清洗后的安全文件名
    orig_name   TEXT NOT NULL,        -- 原始名，仅用于显示
    mime        TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    rel_path    TEXT NOT NULL,        -- 相对 data/uploads/ 的路径
    is_image    INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
```

`rel_path` 存**相对路径**而非绝对路径。项目目录移动后绝对路径全部失效，相对路径不受影响。

`is_image` 用 magic bytes 判定，不信扩展名。

## path_whitelist

```sql
CREATE TABLE path_whitelist (
    id          TEXT PRIMARY KEY,
    path        TEXT NOT NULL,        -- 绝对路径，已 resolve
    can_write   INTEGER NOT NULL DEFAULT 1,
    note        TEXT NOT NULL DEFAULT '',
    builtin     INTEGER NOT NULL DEFAULT 0,   -- 内置项不可删
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE UNIQUE INDEX idx_whitelist_path ON path_whitelist(path);
```

`path` 存 `resolve()` 后的绝对路径。插入时就 resolve，避免每次校验都做。

`builtin=1` 的两条初始记录（`workspace/`、`skills/`）不允许删除 —— 删了 agent 就完全不能读写文件了，且用户不容易想到是这个原因。

`can_write=0` 表示只读放行。用于"让 agent 读参考代码但不许改"的场景。

## run

```sql
CREATE TABLE run (
    id            TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    agent_name    TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL,     -- running|done|cancelled|error
    stop_reason   TEXT,              -- final|max_turns|cancelled|error
    turns         INTEGER NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    started_at    INTEGER NOT NULL,
    ended_at      INTEGER,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES session(id) ON DELETE CASCADE
);
CREATE INDEX idx_run_session ON run(session_id, started_at DESC);
```

## span

```sql
CREATE TABLE span (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    parent_span_id  TEXT,
    depth           INTEGER NOT NULL DEFAULT 0,
    kind            TEXT NOT NULL,   -- llm|tool|agent|compaction
    name            TEXT NOT NULL,
    status          TEXT NOT NULL,   -- running|ok|error
    input_preview   TEXT,            -- 截断到 2000 字符
    output_preview  TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    error_message   TEXT,
    started_at      INTEGER NOT NULL,
    ended_at        INTEGER,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    FOREIGN KEY (run_id) REFERENCES run(id) ON DELETE CASCADE
);
CREATE INDEX idx_span_run ON span(run_id, started_at);
CREATE INDEX idx_span_parent ON span(parent_span_id);
```

M6 阶段实现。在此之前 span 只存在于内存（用于事件的 span 三件套），不落库。

`*_preview` 字段截断到 2000 字符。完整内容已经在 `message` 表里了，span 只是执行树的骨架 —— 存全量会让这张表迅速变成数据库里最大的表。

## SQLite 特定配置

```python
# 连接时执行：
PRAGMA journal_mode = WAL;      # 读写不互斥。默认 DELETE 模式下
                                # 写入会阻塞所有读，流式对话期间前端拉列表会卡住
PRAGMA foreign_keys = ON;       # SQLite 默认关闭外键约束（!），必须显式开
PRAGMA busy_timeout = 5000;     # 锁等待 5 秒而非立即报 database is locked
PRAGMA synchronous = NORMAL;    # WAL 下 NORMAL 已足够安全，FULL 每次写都 fsync 太慢
```

`foreign_keys = ON` 必须每个连接都执行 —— 它是连接级设置，不是数据库级。用 SQLAlchemy 的 `connect` 事件挂上。

### 异步驱动

`aiosqlite`。连接串：

```
sqlite+aiosqlite:///<绝对路径>/data/jeeves.db
```

**路径必须绝对**，基于 `__file__` 推导。相对路径按进程 cwd 解析，从别的目录启动服务会在那里新建一个空库 —— 表现为"我的会话全没了"。

### 并发限制

SQLite 同时只允许一个写事务。单用户场景下这不是问题，但要注意：**流式对话期间不要长时间持有写事务**。

做法：每条消息独立一个短事务写入，不要开一个事务包住整个 run。

## 索引策略

只建实际查询用到的索引。已列出的索引对应这些查询：

| 索引 | 服务的查询 |
| --- | --- |
| `idx_session_list` | 会话列表（固定优先 + 按最后活动排序） |
| `idx_message_session` | 拉某会话的消息（按 seq 排） |
| `idx_message_artifact` | artifact 唯一性约束 + upsert 查找 |
| `idx_todo_session` | 拉某会话未归档的 Todo |
| `idx_binding_unique` | 功能位唯一性 + 绑定解析 |
| `idx_span_parent` | 构建执行树 |

不预先建"可能有用"的索引。SQLite 在几万行规模下全表扫描也很快，过多索引反而让写入变慢。
