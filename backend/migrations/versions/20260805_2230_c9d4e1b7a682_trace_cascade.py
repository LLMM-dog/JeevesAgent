"""trace cascade on session delete

Revision ID: c9d4e1b7a682
Revises: b3f8a2c15d97
Create Date: 2026-08-05 22:30:00.000000

追踪记录跟着会话走。

## 问题

run 和 span 表都有 session_id，但【没有外键】—— 删会话时它们留在库里
成为孤儿。实测你的库里：99 条 run 有 29 条的会话已不存在，
402 条 span 有 114 条。

后果有两个：

  1. 磁盘只增不减。span 表增长速度是消息表的 5~10 倍，
     而唯一的清理手段是按时间清（14 天前），
     删掉的会话如果是昨天的，它的 span 还要占 14 天。
  2. 统计口径错。/traces-stats 的累计花费把已删会话的也算进去，
     用户看到的数字和他现有的会话对不上。

## 修法

加 ON DELETE CASCADE 外键。SQLite 不支持 ALTER 加约束，
得用 batch_alter_table 重建表。

## 为什么先清孤儿再加外键

已有的孤儿行指向不存在的 session.id。加了外键之后它们违反约束 ——
SQLite 在 PRAGMA foreign_keys=ON 时对已存在的违规行不会报错
（它只在写入时检查），但那些行永远删不掉也查不出来，
而且任何一次 VACUUM 或完整性检查都会把它们暴露成"数据库损坏"。

所以顺序必须是：先删孤儿，再加约束。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9d4e1b7a682"
down_revision: str | None = "b3f8a2c15d97"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── 先清孤儿 ──
    #
    # 顺序：span 先于 run。span 也有 run_id，先删 run 的话
    # span 会同时缺两个父。
    for table in ("span", "run"):
        conn.execute(
            sa.text(
                f"DELETE FROM {table} "
                "WHERE session_id NOT IN (SELECT id FROM session)"
            )
        )

    # ── 再加外键 ──
    #
    # batch 模式会建新表、拷数据、换名字 —— 这是 SQLite 下加约束
    # 唯一可行的办法。
    with op.batch_alter_table("run") as b:
        b.create_foreign_key(
            "fk_run_session", "session", ["session_id"], ["id"], ondelete="CASCADE"
        )
    with op.batch_alter_table("span") as b:
        b.create_foreign_key(
            "fk_span_session", "session", ["session_id"], ["id"], ondelete="CASCADE"
        )


def downgrade() -> None:
    with op.batch_alter_table("span") as b:
        b.drop_constraint("fk_span_session", type_="foreignkey")
    with op.batch_alter_table("run") as b:
        b.drop_constraint("fk_run_session", type_="foreignkey")
