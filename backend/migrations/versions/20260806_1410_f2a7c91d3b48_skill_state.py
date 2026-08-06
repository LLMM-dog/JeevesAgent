"""skill enabled state

Revision ID: f2a7c91d3b48
Revises: c9d4e1b7a682
Create Date: 2026-08-06 14:10:00.000000

技能的启用开关。

## 为什么用表而不是写进 SKILL.md

启用与否是用户的偏好，不是技能作者的属性。写进 frontmatter 的话
升级技能包（upload 带 overwrite）会把开关冲掉，而且往第三方 zip 里
写东西等于污染它。

## 为什么表里只存"被关掉的"

没有记录 = 启用。新装的技能默认开着 —— 默认关闭会让用户装完发现
模型看不见它，而没有任何提示说明原因。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2a7c91d3b48"
down_revision: str | None = "c9d4e1b7a682"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skill_state",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        # server_default="1"：已有行不存在（新表），但显式给默认值
        # 是为了让手工 INSERT 也能省掉这一列。
        sa.Column(
            "enabled", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column(
            "created_at", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "updated_at", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_skill_state_name"),
    )
    op.create_index("ix_skill_state_name", "skill_state", ["name"])


def downgrade() -> None:
    op.drop_index("ix_skill_state_name", table_name="skill_state")
    op.drop_table("skill_state")
