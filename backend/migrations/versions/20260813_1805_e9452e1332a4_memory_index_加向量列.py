"""memory_index 加向量列

Revision ID: e9452e1332a4
Revises: ae5afc9df686
Create Date: 2026-08-13 18:05:58.063247

向量存 float32 BLOB 而非 JSON：1024 维存 JSON 约 12KB，存 BLOB 是 4KB。
embedded_hash 与 content_hash 分开 —— 前者记"向量算的是哪一版内容"，
两者不同说明记忆改过但向量没重算，那是必须能被发现的状态。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e9452e1332a4"
down_revision: str | None = "ae5afc9df686"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("memory_index", schema=None) as batch_op:
        batch_op.add_column(sa.Column("embedding", sa.LargeBinary(), nullable=True))
        # server_default 是必须的：SQLite 给已有行加 NOT NULL 列时没有默认值会失败。
        # 空串表示"还没算过向量"，与 embedding_model 的空串语义一致。
        batch_op.add_column(
            sa.Column(
                "embedded_hash",
                sa.String(length=64),
                nullable=False,
                server_default="",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("memory_index", schema=None) as batch_op:
        batch_op.drop_column("embedded_hash")
        batch_op.drop_column("embedding")
