"""
Alembic 环境。

## 关键配置

render_as_batch=True —— SQLite 不支持 ALTER COLUMN / DROP COLUMN。
batch 模式会自动改写成"建新表 → 拷数据 → 删旧表 → 改名"。
不开的话任何字段变更类迁移都会直接报 unsupported。

target_metadata 必须导入所有模型模块，否则 autogenerate 只看到部分表,
生成的迁移会试图 DROP 那些"不存在"的表。
"""

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.core.config import settings  # noqa: E402
from app.infra.db.base import Base  # noqa: E402

# 必须全部导入 —— 少一个模块，autogenerate 就会认为那些表该被删掉
from app.modules.agent import models as agent_models  # noqa: E402,F401
from app.modules.auth import models as auth_models  # noqa: E402,F401
from app.modules.cron import models as cron_models  # noqa: E402,F401
from app.modules.endpoint import models as provider_models  # noqa: E402,F401
from app.modules.memory import models as memory_extraction_models  # noqa: E402,F401
from app.modules.memory import models_db as memory_models  # noqa: E402,F401
from app.modules.session import models as session_models  # noqa: E402,F401
from app.modules.settings import models as settings_models  # noqa: E402,F401
from app.modules.skill import models as skill_models  # noqa: E402,F401
from app.modules.todo import models as todo_models  # noqa: E402,F401
from app.modules.trace import models as trace_models  # noqa: E402,F401

config = context.config

# 只在【独立运行 alembic 命令】时配置日志。
#
# 应用启动时也会调迁移（main.py 的 _run_migrations），那时 structlog 已经配好了。
# 这里再调一次 fileConfig 会：
#   1. 用 alembic.ini 的 formatter 覆盖掉 structlog 的 handler
#   2. disable_existing_loggers 默认 True，直接禁掉已有 logger
# 表现是控制台开始打印裸的 "%(levelname)-5.5s [%(name)s] %(message)s" 字面量,
# 应用自己的结构化日志全部消失。
if config.config_file_name is not None and not config.attributes.get("embedded"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

config.set_main_option("sqlalchemy.url", settings.db_dsn)
target_metadata = Base.metadata

# 【SQLite 不会自己建父目录】。
#
# 全新克隆的仓库里没有 data/（它在 .gitignore 里），此时
# `alembic upgrade head` 直接失败：
#
#   sqlite3.OperationalError: unable to open database file
#
# 而这个报错完全不指向"目录不存在" —— 看起来像权限问题或者路径写错了。
#
# 应用自己启动时不会踩到：get_engine() 里有 mkdir（session.py:45）。
# 所以只有"先跑迁移再起服务"这个顺序会中招，而那恰恰是文档里
# 推荐的顺序，也是 CI 的顺序。
settings.data_dir.mkdir(parents=True, exist_ok=True)


def run_migrations_offline() -> None:
    context.configure(
        url=settings.db_dsn,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite 必需。见模块 docstring。
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
