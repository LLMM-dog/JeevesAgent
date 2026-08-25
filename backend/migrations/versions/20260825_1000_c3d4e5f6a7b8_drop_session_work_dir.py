"""drop session work_dir

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-25 10:00:00.000000

工作区（workspace.root_path）就是工作目录，会话的 work_dir 字段重复了。
删除它：对话的根目录直接由 session.workspace_id 决定。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("session", schema=None) as batch_op:
        batch_op.drop_column("work_dir")


def downgrade() -> None:
    with op.batch_alter_table("session", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("work_dir", sa.Text(), nullable=False, server_default="")
        )
