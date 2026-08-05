# 迁移约定

Alembic + SQLite。SQLite 的 DDL 能力有限，有几个特有的坑。

## 基本流程

```bash
# 改完 models.py 后生成迁移
alembic revision --autogenerate -m "add todo table"

# 检查生成的文件（必须人工看一遍，见下）
# 应用
alembic upgrade head

# 回退一步
alembic downgrade -1
```

启动时自动执行 `upgrade head`，不需要手动跑。但**生成迁移必须手动**——自动生成的内容需要人工审查。

## 必须开 render_as_batch

SQLite 不支持 `ALTER COLUMN`、`DROP COLUMN`（3.35 之前）、加约束。Alembic 的 batch 模式会自动改写成"建新表 → 拷数据 → 删旧表 → 改名"。

`migrations/env.py`：

```python
context.configure(
    connection=connection,
    target_metadata=target_metadata,
    render_as_batch=True,      # SQLite 必须。不开的话任何改列的迁移都会直接报错
    compare_type=True,         # 检测列类型变化
)
```

## autogenerate 生成的迁移必须人工检查

三类它会搞错的情况：

### 1. 部分索引（WHERE 子句）

```sql
CREATE UNIQUE INDEX idx_message_artifact
    ON message(session_id, agent_name) WHERE role = 'artifact';
```

autogenerate **检测不到** `WHERE` 子句的变化，也可能在重新生成时丢掉它。这类索引在迁移文件里手写，并在 models.py 里用注释标注"该索引需手动维护"。

### 2. 索引与约束的重命名

autogenerate 会把重命名识别成"删旧的 + 建新的"。数据量大时这很慢。小项目里无所谓，但要知道它在干什么。

### 3. 默认值变更

SQLite 的 `server_default` 变更在 batch 模式下会重建整表。确认是否真的需要改——很多时候在应用层给默认值就够了。

## 数据迁移单独写

结构变更和数据变更**不放在同一个 revision 里**。

理由：结构变更失败可以 downgrade 回去；数据变更失败往往无法完美回退。混在一起时，中途失败会留下"表结构改了但数据没转换"的中间态。

```python
# 0003_add_window_source.py    只加列
# 0004_backfill_window_source.py  只填数据
```

数据迁移里不要 import 项目的 models——models 会随代码演进，而迁移必须能在任何历史版本上跑。用 `sa.table()` / `sa.column()` 声明当时的表结构：

```python
def upgrade():
    # 不用 from app.modules.provider.models import Model
    # 迁移必须冻结在写它的那一刻。半年后 Model 类可能已经多了三个字段，
    # 这个迁移就跑不动了。
    model_t = sa.table("model",
        sa.column("id", sa.String),
        sa.column("window_source", sa.String),
    )
    op.execute(model_t.update().values(window_source="default"))
```

## 命名规范

文件名：`<4位序号>_<snake_case 描述>.py`

```
0001_initial.py
0002_add_workspace.py
0003_add_todo.py
```

用递增序号而非随机 hash 作为文件名前缀（`revision` ID 仍是 hash），这样 `ls` 出来就是执行顺序。

`down_revision` 链必须是单线的，**不做分支**。个人项目没有并行开发，分支只会带来 merge 冲突。

## 约束命名

SQLite 的匿名约束在 batch 模式下无法被引用（"没有名字的约束怎么 DROP"）。所以必须给所有约束命名。

在 `Base` 的 metadata 里配命名约定：

```python
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}

class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
```

**必须在写第一个迁移之前配好。** 之后再加的话，已有的匿名约束仍然匿名，且 autogenerate 会试图重建所有约束。

## 备份

迁移前自动备份数据库：

```python
# lifespan 里，upgrade 之前
if pending_migrations():
    shutil.copy2(db_path, db_path.with_suffix(f".bak.{now_ms()}"))
```

SQLite 是单文件，`copy2` 就是完整备份。但**必须先 checkpoint WAL**，否则拷出来的文件缺少 WAL 里未合并的数据：

```python
await conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
```

保留最近 5 个备份，更早的删除。

个人项目里这个自动备份价值很高——一次写错的迁移可能毁掉几个月的对话记录，而个人项目通常没有别的备份。

## 启动时自动 upgrade

`main.py` 的 lifespan 里会自动跑 `alembic upgrade head`。

这是个本地单用户应用，用户不会记得手动执行迁移。忘了跑的表现是某个功能报 `no such column`，而报错完全不提示"你需要跑迁移"。自动跑掉这一整类问题。

### 两个实现细节

**必须在 `asyncio.to_thread` 里调。** alembic 的 command API 是同步的，且 `env.py` 内部会自己 `asyncio.run()`。在已运行的 event loop 里直接调会抛 `asyncio.run() cannot be called from a running event loop`。

**必须让 `env.py` 跳过 `fileConfig`。** 应用启动时 structlog 已经配好了，`env.py` 再调一次 `fileConfig` 会用 `alembic.ini` 的 formatter 覆盖 handler，且 `disable_existing_loggers` 默认 `True` 会直接禁掉已有 logger。

表现是控制台开始打印裸的 `%(levelname)-5.5s [%(name)s] %(message)s` 字面量，应用自己的结构化日志全部消失——而这看起来像是日志配置写错了，不像是迁移引起的。

```python
# main.py
cfg.attributes["embedded"] = True

# env.py
if config.config_file_name is not None and not config.attributes.get("embedded"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)
```

## 部分索引：实测能被 autogenerate 捕获

本文档早先说部分索引（带 `WHERE`）必须手写。**实测在当前 Alembic 版本下它能正确捕获**：

```python
batch_op.create_index(
    'ix_message_artifact', ['session_id', 'agent_name'],
    unique=True, sqlite_where=sa.text("role = 'artifact'"),
)
```

生成的 DDL 也是对的：

```sql
CREATE UNIQUE INDEX ix_message_artifact ON message (session_id, agent_name)
WHERE role = 'artifact'
```

但**仍然要在每次 autogenerate 后确认这一行还在**。丢了它的后果是 artifact 的 upsert 语义失效，同一个 `(session_id, agent_name)` 会累积多条产物，而这不会立即报错——只会让上下文里出现多个版本的产物。

## alembic.ini 不能有 BOM

**症状**：

```
configparser.MissingSectionHeaderError: File contains no section headers.
file: 'alembic.ini', line: 1
'\ufeff[alembic]\n'
```

PowerShell 的 `Set-Content -Encoding utf8` 会写入 BOM，而 `configparser` 不识别它，于是把 `\ufeff[alembic]` 当成一个无节名的行。

Windows 上用 `[System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding $false))` 写不带 BOM 的 UTF-8。

## 开发期的例外

表结构还在剧烈变动时，每次改都生成迁移太啰嗦。此时允许删库重建：

```bash
rm data/jeeves.db
# 启动即自动 upgrade，不需要手动执行
```

判断"什么时候开始严格"：**一旦库里有你不想丢的对话记录，就开始严格。**

## 检查清单

每个迁移文件提交前确认：

-  `upgrade()` 和 `downgrade()` 都写了且互逆
-  本地跑过 `upgrade head` 然后 `downgrade -1` 然后再 `upgrade head`
-  部分索引（带 WHERE）在生成结果里还在
-  没有 import 项目的 models
-  结构变更与数据变更分开
-  新增的 NOT NULL 列有 `server_default`（否则已有行插不进去）

最后一条最常犯。SQLite 给已有数据的表加 NOT NULL 列时，必须提供默认值，否则报错且报错信息不明显。
