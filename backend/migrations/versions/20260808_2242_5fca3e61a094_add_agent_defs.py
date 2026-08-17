"""add_agent_defs

Revision ID: 5fca3e61a094
Revises: f2a7c91d3b48
Create Date: 2026-08-08 22:42:27.923265
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '5fca3e61a094'
down_revision: str | None = 'f2a7c91d3b48'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'agent_defs',
        sa.Column('id', sa.String(32), nullable=False),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('avatar', sa.String(50), nullable=True),
        sa.Column('system_prompt', sa.Text(), nullable=False),
        sa.Column('model_id', sa.String(32), nullable=True),
        sa.Column('skill_names', sa.Text(), nullable=False),
        sa.Column('mcp_servers', sa.Text(), nullable=False),
        sa.Column('permission_read', sa.Integer(), nullable=False),
        sa.Column('permission_write', sa.Integer(), nullable=False),
        sa.Column('permission_shell', sa.Integer(), nullable=False),
        sa.Column('permission_network', sa.Integer(), nullable=False),
        sa.Column('permission_subagent', sa.Integer(), nullable=False),
        sa.Column('verification_enabled', sa.Integer(), nullable=False),
        sa.Column('strict_mode', sa.Integer(), nullable=False),
        sa.Column('hidden', sa.Integer(), nullable=False),
        sa.Column('max_turns', sa.Integer(), nullable=True),
        # BigInteger 而非 Integer：TimestampMixin 用的是 BigInteger（UTC 毫秒）。
        # SQLite 不区分这两种，所以写错了也能跑 —— 但 autogenerate 会在之后
        # 每次生成迁移时都夹带一条 alter_column，把无关的改动混进新迁移里。
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
        sa.Column('deleted_at', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_agent_defs_name', 'agent_defs', ['name'])
    op.create_index('ix_agent_defs_deleted', 'agent_defs', ['deleted_at'])

    # 原本这里还给 memory 表加了 agent_id 列。那张表是早期的"一句话记忆"
    # （content + theme），【从来没有任何迁移创建过它】—— 它是 create_all
    # 建出来的，所以这条 ALTER 在任何全新数据库上都会失败：
    #   sqlite3.OperationalError: no such table: memory
    # 迁移链因此在这一步彻底断掉，新机器上装不起来。
    #
    # 记忆系统现在是文件形态（见 docs/architecture/memory.md），元数据表叫
    # memory_index 并有自己的迁移。这条 ALTER 已无对应模型，直接删掉。
    with op.batch_alter_table('session', schema=None) as batch_op:
        batch_op.add_column(sa.Column('agent_id', sa.String(length=32), nullable=False, server_default=''))


def downgrade() -> None:
    with op.batch_alter_table('session', schema=None) as batch_op:
        batch_op.drop_column('agent_id')

    op.drop_index('ix_agent_defs_deleted', table_name='agent_defs')
    op.drop_index('ix_agent_defs_name', table_name='agent_defs')
    op.drop_table('agent_defs')
