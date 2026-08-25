"""add auth tables (remote access auth)

Revision ID: a1b2c3d4e5f6
Revises: 8b72cef0a935
Create Date: 2026-08-23 10:10:00.000000

远程访问鉴权：auth_user（用户名 + PBKDF2 密码哈希）与
auth_session（会话 token 哈希，支持吊销与过期）。

## 安全设计

- 密码绝不存明文，存 pbkdf2_sha256$iterations$salt$hash
- 会话 cookie 值是不透明随机 token，库里只存它的 SHA-256 ——
  数据库泄露不等于会话泄露
- 删除用户时级联删会话（FK ondelete=CASCADE，SQLite 需要
  PRAGMA foreign_keys=ON，启动时已开）
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "8b72cef0a935"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_user",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("is_admin", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Integer(), nullable=False),
        sa.Column("last_login_at", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_user")),
        sa.UniqueConstraint("username", name=op.f("uq_auth_user_username")),
    )
    op.create_table(
        "auth_session",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("created_ip", sa.String(length=64), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["auth_user.id"],
            name=op.f("fk_auth_session_user_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_session")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_session_token_hash")),
    )
    op.create_index(
        "ix_auth_session_user_id", "auth_session", ["user_id"], unique=False
    )
    op.create_index(
        "ix_auth_session_expires_at", "auth_session", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_auth_session_expires_at", table_name="auth_session")
    op.drop_index("ix_auth_session_user_id", table_name="auth_session")
    op.drop_table("auth_session")
    op.drop_table("auth_user")
