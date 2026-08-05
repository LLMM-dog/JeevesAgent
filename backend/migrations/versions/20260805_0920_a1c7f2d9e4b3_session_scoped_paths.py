"""per-session work_dir and whitelist

Revision ID: a1c7f2d9e4b3
Revises: de45134363a1
Create Date: 2026-08-05 09:20:00.000000

会话级工作目录与路径白名单。

## 为什么

原来工作目录是全局的（一张 workspace 表，`is_default=1` 那行），
新建会话自动指向项目自己的 `workspace/` 目录。用户想要的是：
新会话【没有】工作目录，在对话页里为这次对话指定一个。

白名单同理 —— 原来是全局一张表，所有会话共用。而"这个对话能读写
哪些目录"本质上是会话级的决定：给 A 会话开了 D:\proj 的写权限，
不应该让 B 会话也能写。

## 两处设计

`session.work_dir` 用空字符串而不是 NULL 表示"未设置"。
SQLite 对 NULL 的比较需要 `IS NULL`，而空串可以直接 `== ""`，
少一类查询写错的机会。

`path_whitelist.session_id` 可以为 NULL —— NULL 表示全局条目
（内置的上传目录、以及用户手动加的全局项）。会话级条目带 session_id，
会话删除时跟着级联删掉。

## unique 约束要换

原来 `path` 是全局 unique。改成会话级之后，同一个路径可以在不同
会话里各有一条（权限还可能不同），所以 unique 要改成
`(session_id, path)` 复合。

SQLite 不支持 ALTER 改约束，得用 batch_alter_table 重建表。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c7f2d9e4b3"
down_revision: str | None = "de45134363a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── session.work_dir ──
    #
    # 空串 = 未设置。已有会话保持空串，也就是"没有工作目录"——
    # 不自动继承旧的全局工作区，因为那正是用户抱怨的行为。
    with op.batch_alter_table("session") as b:
        b.add_column(
            sa.Column("work_dir", sa.Text(), nullable=False, server_default="")
        )

    # ── path_whitelist.session_id ──
    #
    # NULL = 全局条目。
    #
    # 用 batch_alter_table 是因为要同时做两件 SQLite 不支持在线改的事：
    # 加外键、把 path 上的 unique 换成 (session_id, path) 复合唯一。
    # batch 模式会建新表、拷数据、换名字。
    with op.batch_alter_table("path_whitelist") as b:
        b.add_column(sa.Column("session_id", sa.String(length=32), nullable=True))
        b.create_foreign_key(
            "fk_whitelist_session",
            "session",
            ["session_id"],
            ["id"],
            ondelete="CASCADE",
        )
        # 先扔掉旧的单列唯一。名字由 SQLAlchemy 的命名约定生成，
        # 不同版本可能不同，所以用 try —— 建表时如果是匿名约束，
        # batch 重建过程本身就不会带上它。
        try:
            b.drop_constraint("uq_path_whitelist_path", type_="unique")
        except Exception:  # noqa: BLE001
            pass
        b.create_unique_constraint(
            "uq_whitelist_session_path", ["session_id", "path"]
        )

    # 按会话查白名单是每次工具调用都要做的事，加索引
    op.create_index(
        "ix_path_whitelist_session_id", "path_whitelist", ["session_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_path_whitelist_session_id", table_name="path_whitelist")
    with op.batch_alter_table("path_whitelist") as b:
        b.drop_constraint("uq_whitelist_session_path", type_="unique")
        b.drop_constraint("fk_whitelist_session", type_="foreignkey")
        b.drop_column("session_id")
        b.create_unique_constraint("uq_path_whitelist_path", ["path"])
    with op.batch_alter_table("session") as b:
        b.drop_column("work_dir")
