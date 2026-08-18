"""drop artifact columns

Revision ID: 8b72cef0a935
Revises: 5af6b673978d
Create Date: 2026-08-18 09:22:00.000000

删除 artifact（工作成果钉住）功能遗留的两列和部分唯一索引。

## 为什么现在才删

artifact 功能在代码层已移除（见提交「删除宏（macro）功能」之前的
工作区改动），但这两列当时保留了 —— 因为项目有"迁移只加不删"的约束，
删列会触发 test_no_drop_column_on_message 测试失败。

现在这条约束改成了【列级白名单】：默认仍禁止删列，但"功能已删除、
列从此不再写入"的列可以显式列出理由后删除。artifact_kind / artifact_path
就属于这一类 —— 它们只服务于已删除的功能，删掉不丢对话内容。

## 索引为什么也要删

ix_message_artifact 是带 `role = 'artifact'` WHERE 的部分唯一索引。
role='artifact' 的消息从此不会再产生，这个索引空转且没有意义。
而且 batch 重建表时对部分索引的反射不完整（见 initial 迁移注释），
不显式删掉的话 drop_column 重建表时会把它变成普通唯一索引或报错。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8b72cef0a935"
down_revision: str | None = "5af6b673978d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("message", schema=None) as batch_op:
        batch_op.drop_index(
            "ix_message_artifact", sqlite_where=sa.text("role = 'artifact'")
        )
        batch_op.drop_column("artifact_kind")
        batch_op.drop_column("artifact_path")


def downgrade() -> None:
    with op.batch_alter_table("message", schema=None) as batch_op:
        batch_op.add_column(sa.Column("artifact_kind", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("artifact_path", sa.Text(), nullable=True))
        batch_op.create_index(
            "ix_message_artifact",
            ["session_id", "agent_name"],
            unique=True,
            sqlite_where=sa.text("role = 'artifact'"),
        )
