# 数据库表结构（续）

接 [data-schema.md](data-schema.md)。通用约定见该文件。

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
    model_type      TEXT NOT NULL DEFAULT 'chat',  -- chat|reasoning|embedding|rerank|tts|audio|image
    price_in_per_1m  REAL,
    price_out_per_1m REAL,
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

`builtin=1` 的三条初始记录不允许删除：`workspace/`（可写）、`data/uploads/`（只读）、`skills/`（可写）。删了 agent 就不能读写文件了，而用户不容易想到是这个原因。

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
    session_id      TEXT NOT NULL,
    parent_span_id  TEXT,
    depth           INTEGER NOT NULL DEFAULT 0,
    kind            TEXT NOT NULL,   -- llm|tool|agent|compaction
    name            TEXT NOT NULL,
    agent_name      TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'ok',  -- running|ok|error
    started_at      INTEGER NOT NULL,
    ended_at        INTEGER,
    duration_ms     INTEGER,
    input_preview   TEXT NOT NULL DEFAULT '',
    input_truncated INTEGER NOT NULL DEFAULT 0,
    input_bytes     INTEGER NOT NULL DEFAULT 0,
    output_preview  TEXT NOT NULL DEFAULT '',
    output_truncated INTEGER NOT NULL DEFAULT 0,
    output_bytes    INTEGER NOT NULL DEFAULT 0,
    model_id        TEXT NOT NULL DEFAULT '',
    provider_name   TEXT NOT NULL DEFAULT '',
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cache_read_tokens   INTEGER,
    cache_write_tokens  INTEGER,
    reasoning_tokens    INTEGER,
    total_tokens        INTEGER NOT NULL DEFAULT 0,
    price_in_per_1m  REAL,
    price_out_per_1m REAL,
    cost_usd         REAL NOT NULL DEFAULT 0.0,
    error            TEXT NOT NULL DEFAULT '',
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    FOREIGN KEY (run_id) REFERENCES run(id) ON DELETE CASCADE
);
CREATE INDEX idx_span_run ON span(run_id, started_at);
CREATE INDEX idx_span_parent ON span(parent_span_id);
```

`*_preview` 字段截断到 2000 字符。完整内容已经在 `message` 表里了，span 只是执行树的骨架 —— 存全量会让这张表迅速变成数据库里最大的表。`model_id`/`provider_name`/`price_*` 存快照，模型删了报表还能算成本。

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

## memory_index

记忆本身是 Markdown 文件（`data/memory/`），不在数据库里。这张表只存元数据。完整设计见 [memory.md](memory.md)。

```sql
CREATE TABLE memory_index (
    uri             TEXT PRIMARY KEY,       -- 相对 data/memory/ 的 POSIX 路径
    scope           TEXT NOT NULL,          -- global|agent|session
    memory_type     TEXT NOT NULL,
    agent_id        TEXT NOT NULL DEFAULT '',
    session_id      TEXT NOT NULL DEFAULT '',
    peer_agent_id   TEXT NOT NULL DEFAULT '',   -- 非空 = "A 眼中的 B"
    title           TEXT NOT NULL DEFAULT '',
    version         INTEGER NOT NULL DEFAULT 1,
    level           INTEGER NOT NULL DEFAULT 2, -- 0=L0(abstract) 1=L1(overview) 2=L2(details)
    active_count    INTEGER NOT NULL DEFAULT 0, -- 召回命中次数，热度分的频率分量
    content_hash    TEXT NOT NULL DEFAULT '',   -- 幂等写入的依据
    embedding_model TEXT NOT NULL DEFAULT '',
    embedding_dim   INTEGER NOT NULL DEFAULT 0,
    embedding       BLOB,                       -- float32 紧凑二进制，不是 JSON
    embedded_hash   TEXT NOT NULL DEFAULT '',    -- 向量算的是哪一版内容
    file_updated_at INTEGER NOT NULL DEFAULT 0, -- 文件的 updated_at
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL            -- 索引行的更新时间
);
CREATE INDEX ix_memory_index_owner   ON memory_index(agent_id, memory_type);
CREATE INDEX ix_memory_index_scope   ON memory_index(scope, memory_type);
CREATE INDEX ix_memory_index_session ON memory_index(session_id);
```

**文件是真源，这张表是可重建的缓存。** 冲突时相信文件（`service.rebuild_index()` 全量重建）。这条不能反。

为什么记忆存文件却要一张表：

| 字段 | 为什么不能只靠文件 |
| --- | --- |
| `title` / `memory_type` | 列举时不必 rglob 整个目录再逐个解 frontmatter |
| `active_count` | 每次召回命中都要 +1。写进 Markdown frontmatter 会让 git diff 全是计数器噪音 |
| `content_hash` | 幂等写入：合并后哈希不变就不写盘、`version` 不递增 |
| `embedding_model` / `embedding_dim` | 换嵌入模型后旧向量失效。不检测会静默算出错误的相似度 —— 召回还在返回结果，只是结果没有意义 |
| `embedding` | 向量本身。不进 Markdown：它是派生数据、不可读、换模型就失效，写进 frontmatter 会让 git diff 出现 4KB 乱码 |
| `embedded_hash` | 与 `content_hash` 比较能发现"记忆改过但向量没重算"，那时召回用的是旧语义 |
| `session_id` | 删会话时按它找出该清理的文件 |

`embedding` 用 float32 BLOB 而非 JSON：1024 维存 JSON 约 12KB、存 BLOB 是 4KB，而且 JSON 每次读都要解析。不建独立的 vector 表——向量与索引行一对一且同生命周期，拆表只会让每次召回多一次 JOIN。

## app_setting

用户在前端调的运行时设置。

```sql
CREATE TABLE app_setting (
    key        TEXT PRIMARY KEY,   -- 点分路径，如 memory.keep_recent_turns
    value      TEXT NOT NULL,      -- 一律存字符串，读取时按目标类型转
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
```

**key-value 而非每项一列**：设置会持续增加，每加一个就要一次迁移。表里只存「用户改过的那些」，其余回落 `config.py` 的默认值——所以"恢复默认"就是删行，不必记住默认值是多少。

值存字符串而非 JSON：SQLite 的 JSON 支持依赖编译选项，而这些值都是标量。转换失败时忽略那一项并告警，不让一个坏值导致设置页打不开。

可改的 key 有**白名单**（`settings/service.py` 的 `SETTABLE`）。没有白名单的话前端能写 `security.encryption_key` 或 `db.path`，那会直接破坏系统。白名单同时给前端提供类型和范围——前端不硬编码可调项列表，否则两边必然不同步。

`uri` 用相对路径而非绝对路径：绝对路径含项目根目录，移动项目或换机器后全表失效，而记忆文件本身还在。

`file_updated_at` 与 `updated_at` 分开：后者是索引行的更新时间（重建索引时会变），前者跟着文件走。混用会让"哪些记忆最近变过"在重建后全部错乱。

## auth_user

远程访问鉴权的用户表（用户名 + 密码哈希）。

```sql
CREATE TABLE auth_user (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,     -- pbkdf2_sha256$iters$salt$hash
    is_admin      INTEGER NOT NULL DEFAULT 1,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
```

**密码绝不存明文。** 哈希格式自描述（算法 + 迭代数 + 盐 + 摘要），
换算法时可平滑迁移。`enabled=0` 立即失效（中间件校验），不等会话过期。

## auth_session

登录会话。cookie 值是随机 token，**库里只存它的 SHA-256** ——
数据库泄露不等于会话泄露；原始 token 只在登录响应里出现一次。

```sql
CREATE TABLE auth_session (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES auth_user(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,   -- SHA-256 hex
    expires_at INTEGER NOT NULL,       -- UTC 毫秒
    created_ip TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
```

过期由中间件惰性清理（读到过期行即删）。删用户时级联删会话。
## memory_extraction

记忆提取水位线：记录每个智能体在每个会话里已提取到哪条消息（seq），
实现增量提取与多智能体隔离。

```sql
CREATE TABLE memory_extraction (
    session_id        TEXT NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    agent_id          TEXT NOT NULL,
    last_seq          INTEGER NOT NULL,      -- 已提取到的最大消息 seq
    extraction_count  INTEGER NOT NULL DEFAULT 0,
    last_report       TEXT NOT NULL DEFAULT '{}',  -- 上次提取的 CommitReport 序列化
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    PRIMARY KEY (session_id, agent_id)
);
```

复合主键 (session_id, agent_id)：每个智能体各自维护水位线，互不干扰。
下次提取只处理 `seq > last_seq` 的消息。

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
