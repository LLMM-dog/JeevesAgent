"""remove_verification_fields

Revision ID: 59c26c359fb6
Revises: b7d2c4e8f1a3
Create Date: 2026-08-16 11:04:21.393381
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '59c26c359fb6'
down_revision: str | None = 'b7d2c4e8f1a3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite 不支持 DROP COLUMN，需要用 batch mode 重建表
    with op.batch_alter_table('agent_defs', schema=None) as batch_op:
        batch_op.drop_column('verification_enabled')
        batch_op.drop_column('strict_mode')


def downgrade() -> None:
    # 回滚时恢复这两列
    with op.batch_alter_table('agent_defs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('verification_enabled', sa.INTEGER(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('strict_mode', sa.INTEGER(), nullable=False, server_default='0'))

