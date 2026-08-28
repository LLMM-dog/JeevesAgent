"""add_model_group_id

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-27 10:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite 不支持直接给已有表加带 FK 的列，用 batch_alter_table 重建。
    with op.batch_alter_table("model", schema=None) as batch_op:
        batch_op.add_column(sa.Column("group_id", sa.String(length=32), nullable=True))
        batch_op.create_foreign_key(
            "fk_model_group_id", "provider", ["group_id"], ["id"], ondelete="SET NULL"
        )


def downgrade() -> None:
    with op.batch_alter_table("model", schema=None) as batch_op:
        batch_op.drop_constraint("fk_model_group_id", type_="foreignkey")
        batch_op.drop_column("group_id")
