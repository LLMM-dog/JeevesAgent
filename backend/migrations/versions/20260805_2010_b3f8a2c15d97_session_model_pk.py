"""session model_pk

Revision ID: b3f8a2c15d97
Revises: a1c7f2d9e4b3
Create Date: 2026-08-05 20:10:00.000000

会话记住自己用哪个模型。

## 为什么

对话页要能快捷切模型，而切换必须【只影响这个对话】—— 改功能位绑定
是全局的，用户在一个对话里换成便宜模型，不该让所有对话都跟着换。

空串 = 跟随功能位绑定的默认模型。这是绝大多数对话的状态，
所以默认值就是空串，已有会话不受影响。

## 为什么不加外键

模型被删掉时这里会悬空。但那时回落到默认绑定就行，
比 ON DELETE CASCADE 把整个会话删掉好得多 ——
用户删一个没在用的模型，不该丢掉历史对话。

取用时校验存在性。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3f8a2c15d97"
down_revision: str | None = "a1c7f2d9e4b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("session") as b:
        b.add_column(
            sa.Column(
                "model_pk", sa.String(length=32), nullable=False, server_default=""
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("session") as b:
        b.drop_column("model_pk")
