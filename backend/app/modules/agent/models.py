"""
智能体定义模型。

每个智能体是一个独立的"角色卡片"：绑定提示词、模型、权限、skills、MCP。
会话通过 agent_id 关联到智能体定义。
"""

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.db.base import Base, TimestampMixin


class AgentDefinition(Base, TimestampMixin):
    __tablename__ = "agent_defs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    avatar: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # ── 行为 ──
    system_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # NULL = 跟随对话设置（不绑定特定模型）
    model_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # ── 能力（JSON 数组，存字符串） ──
    skill_names: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    mcp_servers: Mapped[str] = mapped_column(Text, default="[]", nullable=False)

    # ── 权限 ──
    permission_read: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    permission_write: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    permission_shell: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    permission_network: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    permission_subagent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ── 系统字段 ──
    hidden: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_turns: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 额外 LLM 参数（智能体级）。用户自由填写的参数字符串，如
    # `thinking: {"type": "disabled"}`，解析后原样发给 LLM 的 body。
    #
    # 各模型的思考/采样参数不统一，这里不做抽象映射，让用户直接写原始字段。
    # 空串 = 不附加任何参数。
    extra_llm_params: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # 软删除
    deleted_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        Index("ix_agent_defs_name", "name"),
        Index("ix_agent_defs_deleted", "deleted_at"),
    )
