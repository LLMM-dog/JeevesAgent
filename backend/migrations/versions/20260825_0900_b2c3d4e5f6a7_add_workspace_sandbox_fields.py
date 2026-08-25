"""add workspace sandbox fields

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-25 09:00:00.000000

工作区级执行环境：每个工作区可以独立配置本机或 Docker 容器执行。

## 设计

- sandbox_backend: local（默认，宿主直接执行）| docker（容器隔离）
- docker_container: 容器名，唯一（应用层校验，DB 不加唯一约束 ——
  多个 local 工作区的空串会冲突）
- docker_image / docker_network: docker 后端的镜像与网络
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workspace", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("sandbox_backend", sa.String(length=16), nullable=False, server_default="local")
        )
        batch_op.add_column(
            sa.Column("docker_container", sa.String(length=64), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("docker_image", sa.String(length=128), nullable=False, server_default="python:3.12-slim")
        )
        batch_op.add_column(
            sa.Column("docker_network", sa.String(length=16), nullable=False, server_default="none")
        )


def downgrade() -> None:
    with op.batch_alter_table("workspace", schema=None) as batch_op:
        batch_op.drop_column("sandbox_backend")
        batch_op.drop_column("docker_container")
        batch_op.drop_column("docker_image")
        batch_op.drop_column("docker_network")
