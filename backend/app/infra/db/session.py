"""
数据库引擎与会话。

db 不做 port + 适配器 —— SQLAlchemy 本身已是抽象层，再包一层纯属浪费。
（对比 llm / sandbox / websearch，那些是真需要换实现的。）
"""

from collections.abc import AsyncIterator
from typing import Any

import structlog
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

log = structlog.get_logger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _apply_pragmas(dbapi_conn: Any, _record: Any) -> None:
    """
    每个连接都要执行 —— 这些是【连接级】设置，不是数据库级。

    journal_mode=WAL: 读写不互斥。默认 DELETE 模式下写入会阻塞所有读，
      流式对话期间前端拉列表会卡住。
    foreign_keys=ON: SQLite 默认【关闭】外键约束。不显式开的话，
      ON DELETE CASCADE 完全无效，删会话不会删消息，留一堆孤儿行。
    busy_timeout: 锁等待 5 秒而非立即报 "database is locked"。
    synchronous=NORMAL: WAL 下 NORMAL 已足够安全，FULL 每次写都 fsync 太慢。
    """
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        _engine = create_async_engine(
            settings.db_dsn,
            echo=False,
            # SQLite 同时只允许一个写事务。单用户场景下不是问题，但要注意
            # 流式对话期间不要长时间持有写事务 —— 每条消息独立一个短事务。
            pool_pre_ping=True,
        )
        event.listen(_engine.sync_engine, "connect", _apply_pragmas)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
        log.info("database_ready", path=str(settings.db_path))
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖。"""
    async with get_sessionmaker()() as session:
        yield session


async def checkpoint_wal() -> None:
    """
    备份前必须先 checkpoint，否则拷出来的 .db 文件缺少 WAL 里未合并的数据。
    """
    async with get_engine().begin() as conn:
        await conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
