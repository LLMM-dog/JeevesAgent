"""add_model_type

Revision ID: b7d2c4e8f1a3
Revises: 2caff439cec9
Create Date: 2026-08-15 18:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "b7d2c4e8f1a3"
down_revision: str | None = "2caff439cec9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite 不支持 ALTER 加列，env.py 里 render_as_batch=True。
    # server_default 必带 —— SQLite 加 NOT NULL 列时没有默认值会报错。
    with op.batch_alter_table("model", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("model_type", sa.String(length=16), nullable=False, server_default="chat")
        )


def downgrade() -> None:
    with op.batch_alter_table("model", schema=None) as batch_op:
        batch_op.drop_column("model_type")
