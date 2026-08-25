"""
会话与消息模型。

字段与 docs/02-data/schema.md 一一对应，那份文档是唯一真源。
"""

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.db.base import Base, TimestampMixin


class Workspace(Base, TimestampMixin):
    __tablename__ = "workspace"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # 绝对路径。工作区被添加时自动加进路径白名单 ——
    # 否则建了工作区但 agent 读不了里面的文件，这个关联用户想不到。
    root_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    is_default: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ── 执行环境（每个工作区独立设置）──
    # local | docker。local = 直接在宿主执行（默认）。
    sandbox_backend: Mapped[str] = mapped_column(String(16), default="local", nullable=False)
    # 容器名。sandbox_backend=docker 时生效，必须唯一（应用层校验）。
    # 空串表示还没配容器。
    docker_container: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    docker_image: Mapped[str] = mapped_column(
        String(128), default="python:3.12-slim", nullable=False
    )
    # none | bridge。默认 none（网络隔离）。
    docker_network: Mapped[str] = mapped_column(String(16), default="none", nullable=False)


class Session(Base, TimestampMixin):
    __tablename__ = "session"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 空串时前端显示"新会话"。首轮对话后由 title 功能位的模型生成。
    title: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 这次对话的工作目录。空串 = 未设置。
    #
    # 【新会话默认为空】是有意的：自动指向某个目录会让用户在不知情的
    # 情况下让 agent 读写那里。要它干活先告诉它在哪干。
    #
    # 用空串而不是 NULL：SQLite 比较 NULL 要用 IS NULL，
    # 空串可以直接 == ""，少一类查询写错的机会。
    work_dir: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # 这次对话用哪个模型。空串 = 跟随功能位绑定的默认模型。
    #
    # 不加外键：模型被删掉时这里会悬空，但那时回落到默认绑定就行，
    # 比级联删掉整个会话好得多。取用时校验存在性。
    model_pk: Mapped[str] = mapped_column(String(32), default="", nullable=False)

    # 参与该会话的智能体列表（JSON 数组）。
    #
    # 支持多智能体协作：多个智能体可以同时参与一个会话。
    # 会话中可以动态添加或移除智能体。
    # 空列表 = 无智能体（仅用户消息）。
    #
    # 存储格式：JSON 数组字符串，如 '["agent1", "agent2"]'
    # SQLite 不支持原生数组类型，用 JSON 列 + Python 侧序列化。
    agent_ids: Mapped[str] = mapped_column(Text, default="[]", nullable=False)

    workspace_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("workspace.id"), nullable=False
    )
    pinned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # 四个模式开关是【会话级】而非全局的 ——
    # 在测试目录里的会话可以开 auto，在真实项目里的保持 manual。
    approval_mode: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    # 记忆的读写【分成两个开关】，不合并成一个。
    #
    # private_mode = 这轮不写记忆；amnesia_mode = 这轮不召回记忆。
    # 合成一个会丢掉"让它记但这轮别提"和"这轮别记但可以用旧记忆"
    # 这两类需求 —— 它们是不同的事。
    #
    # 也是分开的（graph.py:420-421 的 private_mode / skip_recall），
    # 而且声明为 config 必需字段，强制调用方表态。
    private_mode: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    amnesia_mode: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    vision_mode: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # 流式开关（会话级）。控制后端调用 LLM 时 stream: true/false。
    # 默认开 —— 流式是对话的默认体验，关闭只是少数需要"一次性拿完整回复"的场景。
    # 注意：智能体的 extra_llm_params 里若显式写了 stream，以智能体为准（见 loop）。
    stream_enabled: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # 冗余字段。可以从 message 表算出来，但会话列表需要按最后活动时间排序
    # 并显示条数，每次列表查询都做子查询聚合在几百个会话时就明显变慢。
    # 代价：写消息时必须在同一事务里同步更新这两个字段。
    last_message_at: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        Index("ix_session_list", pinned.desc(), last_message_at.desc()),
    )

    def get_agent_ids(self) -> list[str]:
        """获取智能体 ID 列表。"""
        import json

        if not self.agent_ids:
            return []
        try:
            ids = json.loads(self.agent_ids)
            return ids if isinstance(ids, list) else []
        except Exception:
            return []

    def set_agent_ids(self, ids: list[str]) -> None:
        """设置智能体 ID 列表。"""
        import json

        self.agent_ids = json.dumps(ids)

    def add_agent(self, agent_id: str) -> bool:
        """
        添加智能体到会话。

        Returns:
            True 如果添加成功，False 如果已存在
        """
        ids = self.get_agent_ids()
        if agent_id in ids:
            return False
        ids.append(agent_id)
        self.set_agent_ids(ids)
        return True

    def remove_agent(self, agent_id: str) -> bool:
        """
        从会话中移除智能体。

        Returns:
            True 如果移除成功，False 如果不存在
        """
        ids = self.get_agent_ids()
        if agent_id not in ids:
            return False
        ids.remove(agent_id)
        self.set_agent_ids(ids)
        return True


class Message(Base, TimestampMixin):
    __tablename__ = "message"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("session.id", ondelete="CASCADE"), nullable=False
    )
    # 排序【必须】靠这个，不能靠 id（随机 base62 的字典序与生成顺序无关），
    # 也不能靠 created_at（同一毫秒内可能产生多条：assistant + 三个 tool 结果）。
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    # 空串 = 用户可见主线；有值 = 该智能体的私有记忆线。
    # 这是"每个智能体有自己的记忆"的全部实现。
    # 必须 NOT NULL DEFAULT ''，不能允许 NULL ——
    # 否则部分唯一索引对 NULL 不生效（NULL 互不相等）。
    agent_name: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 推理模型的思维链。与正文分开存：前端折叠显示，且【不发回给 LLM】
    # （下一轮请求不带 reasoning，多数 API 也不接受）。
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)

    # JSON 文本。接口返回时已解析为对象/数组。
    tool_calls: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tool_display: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_error: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # 只存引用【清单】不存展开内容：内容按需重读，历史记录小，
    # 压缩不会误伤，且文件改了下一轮自动看到新版。
    refs: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachments: Mapped[str | None] = mapped_column(Text, nullable=True)

    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    span_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        Index("ix_message_seq", "session_id", "seq", unique=True),
        Index("ix_message_session", "session_id", "seq"),
    )
