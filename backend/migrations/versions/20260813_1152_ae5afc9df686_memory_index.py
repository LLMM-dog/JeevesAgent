"""memory_index

记忆索引表。记忆本身是 Markdown 文件（data/memory/），这张表只存元数据 ——
列举、热度统计、按会话清理、嵌入模型漂移检测。文件是真源，索引是可重建的缓存。
见 docs/architecture/memory.md#但索引进-sql

Revision ID: ae5afc9df686
Revises: 5fca3e61a094
Create Date: 2026-08-13 11:52:57.347161
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'ae5afc9df686'
down_revision: str | None = '5fca3e61a094'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'memory_index',
        # 相对 data/memory/ 的 POSIX 路径。用相对路径做主键：绝对路径含项目
        # 根目录，移动项目或换机器后全表失效，而记忆文件本身还在。
        sa.Column('uri', sa.Text(), nullable=False),
        sa.Column('scope', sa.String(length=16), nullable=False),
        sa.Column('memory_type', sa.String(length=64), nullable=False),
        # 空串 = 不适用（global 域没有 agent_id）。用空串而非 NULL 与
        # message.agent_name 的取法一致 —— 少一类查询写错的机会。
        sa.Column('agent_id', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('session_id', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('peer_agent_id', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('title', sa.Text(), nullable=False, server_default=''),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        # 召回命中次数。热度分的频率分量。
        sa.Column('active_count', sa.Integer(), nullable=False, server_default='0'),
        # 正文 + 业务字段的哈希。幂等写入靠它判断内容有没有变。
        sa.Column('content_hash', sa.String(length=64), nullable=False, server_default=''),
        # 换嵌入模型后维度变化，旧向量的相似度计算毫无意义但不报错。
        sa.Column('embedding_model', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('embedding_dim', sa.Integer(), nullable=False, server_default='0'),
        # 文件的 updated_at。与下面的 updated_at 分开 —— 后者是索引行的更新时间，
        # 重建索引时会变，而这个跟着文件走。
        sa.Column('file_updated_at', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('uri', name=op.f('pk_memory_index')),
    )
    with op.batch_alter_table('memory_index', schema=None) as batch_op:
        batch_op.create_index('ix_memory_index_owner', ['agent_id', 'memory_type'], unique=False)
        batch_op.create_index('ix_memory_index_scope', ['scope', 'memory_type'], unique=False)
        batch_op.create_index('ix_memory_index_session', ['session_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('memory_index', schema=None) as batch_op:
        batch_op.drop_index('ix_memory_index_session')
        batch_op.drop_index('ix_memory_index_scope')
        batch_op.drop_index('ix_memory_index_owner')

    op.drop_table('memory_index')
