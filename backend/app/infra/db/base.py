"""
SQLAlchemy 基类。

不做软删除。deleted 字段只在企业审计场景下有意义；个人项目里软删除
只带来"每个查询都要记得加 WHERE deleted=0"的负担，忘一次就出 bug。真删。
例外：todo 有 archived_at（验收关闭后归档但可查历史）。
"""

from sqlalchemy import BigInteger, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.time import now_ms

# 必须在写第一个迁移之前配好命名约定。
# SQLite 的匿名约束在 alembic batch 模式下无法被引用（"没有名字的约束怎么 DROP"）。
# 之后再加的话，已有的匿名约束仍然匿名，且 autogenerate 会试图重建所有约束。
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """
    UTC 毫秒整数。字段名以 _at 结尾。

    用 default 而非 server_default：SQLite 的 server_default 无法取毫秒，
    且我们要求全项目统一走 core.time.now_ms()（测试可 patch 一处）。
    """

    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms, nullable=False)
    updated_at: Mapped[int] = mapped_column(
        BigInteger, default=now_ms, onupdate=now_ms, nullable=False
    )
