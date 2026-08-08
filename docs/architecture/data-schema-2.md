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

**没有明文列。** 任何 API 响应都只返回 `key_hint`。见 [../architecture/providers.md](../architecture/providers.md#api-key-存储)。

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

见 [../architecture/providers.md](../architecture/providers.md#vision多模态)。

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

解析顺序见 [../architecture/agents.md](../architecture/agents.md#每个智能体可绑不同模型)。

## memory

```sql
CREATE TABLE memory (
    id                TEXT PRIMARY KEY,
    content           TEXT NOT NULL,
    theme             TEXT NOT NULL,          -- 主题分类
    hit               INTEGER NOT NULL,       -- 命中次数
    last_hit_at       INTEGER,
    confidence        REAL NOT NULL,          -- 默认 0.6
    source            TEXT NOT NULL,          -- auto / manual / tool
    origin_session_id TEXT,
    history           TEXT NOT NULL,          -- JSON，每次变更的记录
    archived_at       INTEGER,                -- 非 NULL = 已归档
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);
CREATE INDEX ix_memory_recall  ON memory(archived_at, theme, hit);
CREATE INDEX ix_memory_updated ON memory(archived_at, updated_at);
```

跨会话的长期记忆。

`history` 存**每次变更的理由**。改一条记忆时必须写 reason —— 记忆会影响之后所有对话，而"这条为什么变成现在这样"事后完全无从追溯。

`archived_at` 表示归档而非真删。删错一条记忆是不可逆的，而归档留了退路；召回时用它过滤，所以两个索引都以它开头。

`hit` / `last_hit_at`：记忆条目会越积越多，需要淘汰依据。长期没被命中的条目可以提示用户清理。

`confidence` 默认 0.6 而不是 1.0 —— 模型自动提炼出来的东西不该一开始就被当成确定事实。

**召回零 LLM 调用**：SQL 粗筛（按 theme + hit）+ 关键词打分。用 LLM 判断相关性的话每轮对话都要多一次调用，而且成本随记忆总量线性增长。

**不做向量检索。** 个人项目的记忆条目量级在几百条。真的多到装不下再上 `sqlite-vec`。

`origin_session_id` 不做外键 —— 会话删除后记忆应该保留。

## path_whitelist

```sql
CREATE TABLE path_whitelist (
    id          TEXT PRIMARY KEY,
    session_id  TEXT,                 -- NULL = 全局条目
    path        TEXT NOT NULL,        -- 绝对路径，已 resolve
    can_write   INTEGER NOT NULL DEFAULT 1,
    note        TEXT NOT NULL DEFAULT '',
    builtin     INTEGER NOT NULL DEFAULT 0,   -- 内置项不可删
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES session(id) ON DELETE CASCADE
);
CREATE INDEX ix_path_whitelist_session_id ON path_whitelist(session_id);
CREATE UNIQUE INDEX uq_whitelist_session_path
    ON path_whitelist(session_id, path);
```

`path` 存 `resolve()` 后的绝对路径。插入时就 resolve，避免每次校验都做。

**唯一约束是 `(session_id, path)` 复合**，不是单独的 `path`。同一路径在不同会话可以有不同权限 —— "这个对话能读写哪些目录"本质上是会话级的决定：给 A 会话开了 `D:\proj` 的写权限，不该让 B 会话也能写。

`session_id` 为 NULL 表示全局条目（内置项和用户加的全局项）。

`builtin=1` 的四条初始记录不允许删除：`workspace/`（可写）、`data/uploads/`（只读）、`skills/`（可写）、`macros/`（可写）。删了 agent 就不能读写文件了，而用户不容易想到是这个原因。

这四条是**逐条 upsert** 的，不是"表为空才插"。后者会让已经在用的用户永远拿不到新增的内置项 —— 症状是"文档说能写 `skills/`，我这儿报路径不在白名单内"。

`can_write=0` 表示只读放行。用于"让 agent 读参考代码但不许改"的场景。

## skill_state

```sql
CREATE TABLE skill_state (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,        -- SKILL.md frontmatter 里的 name
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX ix_skill_state_name ON skill_state(name);
CREATE UNIQUE INDEX uq_skill_state_name ON skill_state(name);
```

技能的启用开关。关掉的技能不进系统提示词，见 [../architecture/skills.md](../architecture/skills.md#技能开关)。

**为什么用表而不是写进 SKILL.md frontmatter**：启用与否是用户的偏好，不是技能作者的属性。写进文件的话升级技能包（upload 带 overwrite）会把开关冲掉，而且往 zip 装进来的第三方内容里写东西等于污染它。

**表里只存被关掉的**。没有记录 = 启用 —— 默认关闭会让用户装完发现模型看不见它，而没有任何提示说明原因。

按 `name` 而不是路径关联：技能可以被移动目录，而模型看到的一直是名字。也意味着删掉技能再装回来时开关状态还在，那符合直觉。

## run

```sql
CREATE TABLE run (
    id            TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    parent_run_id TEXT,              -- 子智能体的 run 指向父 run
    agent_name    TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL,     -- running|done|cancelled|error
    stop_reason   TEXT NOT NULL DEFAULT '',
    started_at    INTEGER NOT NULL,
    ended_at      INTEGER,
    duration_ms   INTEGER,
    turns         INTEGER NOT NULL DEFAULT 0,
    -- token 六维度拆分。只存 prompt/completion 两项的话
    -- 算不出缓存命中省了多少，也看不出推理模型的思考开销。
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cache_read_tokens   INTEGER,
    cache_write_tokens  INTEGER,
    reasoning_tokens    INTEGER,
    total_tokens        INTEGER NOT NULL DEFAULT 0,
    -- rollup = 含全部子 run 的累计值。子智能体的开销必须能归到父任务上，
    -- 否则用户看到主 run 只花了几百 token 而账单上是几万。
    rollup_total_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd            REAL NOT NULL DEFAULT 0,
    rollup_cost_usd     REAL NOT NULL DEFAULT 0,
    error         TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES session(id) ON DELETE CASCADE
);
CREATE INDEX ix_run_session_started ON run(session_id, started_at);
CREATE INDEX ix_run_created ON run(created_at);
CREATE INDEX ix_run_parent  ON run(parent_run_id);
```

`cost_usd` 按**下单时刻的单价快照**算，不是查询时再乘当前价格。模型改价之后历史成本不该跟着变。

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

## cron_task

```sql
CREATE TABLE cron_task (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    prompt        TEXT NOT NULL,        -- 到点发给 agent 的内容
    cron          TEXT NOT NULL,        -- 五段 cron 表达式
    timezone      TEXT NOT NULL,        -- IANA 时区名
    workspace_id  TEXT NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    on_missed     TEXT NOT NULL,        -- skip | run_once
    last_fired_at INTEGER NOT NULL DEFAULT 0,
    next_fire_at  INTEGER NOT NULL DEFAULT 0,
    run_count     INTEGER NOT NULL DEFAULT 0,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
CREATE INDEX ix_cron_task_enabled ON cron_task(enabled);
```

`timezone` 必须单独存，不能只靠服务器本地时区 —— "每天早上 9 点"在用户换时区或服务器时区变化后必须仍然是他的 9 点。

`next_fire_at` 预计算并落库，不是每次扫描时现算。调度器只查 `next_fire_at <= now` 的行，不需要把所有任务的 cron 表达式都解一遍。

`on_missed` 处理服务没运行时错过的窗口：`skip` 直接跳到下一次，`run_once` 补跑一次 —— 而不是把错过的 N 次全跑一遍（关机一周再开机会瞬间触发几十个 run）。

## cron_run

```sql
CREATE TABLE cron_run (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    scheduled_at INTEGER NOT NULL,      -- 计划触发时刻
    started_at   INTEGER NOT NULL DEFAULT 0,
    finished_at  INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL,          -- pending|running|done|error|skipped
    detail       TEXT NOT NULL DEFAULT '',
    session_id   TEXT NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
CREATE INDEX ix_cron_run_task_time ON cron_run(task_id, scheduled_at);
```

`scheduled_at` 与 `started_at` 分开存。两者的差值就是调度延迟 —— 只存一个的话看不出"任务晚了 10 分钟才跑"。

`session_id` 记录这次触发开在哪个会话里，用户能点进去看 agent 实际做了什么。定时任务是无人值守的，没有这个入口就只能看一行状态。

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
