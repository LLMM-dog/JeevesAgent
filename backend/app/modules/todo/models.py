"""
Todo 模型。

"一个会话同时只有一个 in_progress"靠应用层保证，不做数据库约束 ——
SQLite 的部分唯一索引无法表达"某个值最多出现一次"。
"""

from app.infra.db.base import Base, TimestampMixin
from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column


class Todo(Base, TimestampMixin):
    __tablename__ = "todo"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("session.id", ondelete="CASCADE"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    priority: Mapped[str] = mapped_column(String(16), default="medium", nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 非空表示已验收关闭。不删除 —— 仍可在历史里查。
    archived_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (Index("ix_todo_session", "session_id", "archived_at", "order_index"),)
