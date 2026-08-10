"""
端点、模型、绑定。

没有明文列 —— 任何 API 响应都只返回 key_hint。
"""

from app.infra.db.base import Base, TimestampMixin
from sqlalchemy import (
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column


class Endpoint(Base, TimestampMixin):
    __tablename__ = "provider"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    # 存【规范化后】的值（补齐 /v1、去尾斜杠）。规范化在 service 层做。
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_cipher: Mapped[str] = mapped_column(Text, nullable=False)
    # 尾 4 位，供用户辨认是哪个 Key
    key_hint: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_probe_at: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Model(Base, TimestampMixin):
    __tablename__ = "model"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    endpoint_id: Mapped[str] = mapped_column(
        "provider_id", String(32), ForeignKey("provider.id", ondelete="CASCADE"), nullable=False
    )
    # 端点的模型标识，如 "deepseek-chat"
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    context_window: Mapped[int] = mapped_column(Integer, default=32768, nullable=False)
    # matched | manual | default
    # default 状态必须让用户知道 —— 它直接影响压缩阈值。匹配不到却静默用默认值，
    # 会导致大窗口模型被过早压缩（用户觉得"怎么老是压缩"）或小窗口模型直接 400。
    window_source: Mapped[str] = mapped_column(String(16), default="default", nullable=False)
    # true | false | unknown（三态字符串，不是布尔）
    # unknown 必须存在：核验有成本（要发真实请求），不能对每个模型都跑一遍。
    supports_vision: Mapped[str] = mapped_column(String(8), default="unknown", nullable=False)
    supports_tools: Mapped[str] = mapped_column(String(8), default="unknown", nullable=False)
    vision_checked_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # 每百万 token 单价（USD）。NULL 表示未配 —— 不是免费。
    #
    # 三态很重要：0.0 意味着"确实免费"（本地模型），NULL 意味着"不知道"。
    # 混在一起的话成本报表里会把未配价的模型显示成零成本，
    # 用户以为省钱了。
    #
    # span 行里存【快照单价】，不查这张表现算 —— 价格会变，
    # 历史成本必须可复算。
    price_in_per_1m: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_out_per_1m: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (Index("ix_model_unique", "provider_id", "model_id", unique=True),)


class ModelBinding(Base, TimestampMixin):
    __tablename__ = "model_binding"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # '' 表示全局默认
    agent_name: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    # chat | vision | title | compact | embedding
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    # 字段名用 model_pk 而非 model_id —— 因为 model.model_id 已经表示
    # "端点的模型标识"，两个 model_id 会混。这是命名冲突的必要妥协。
    model_pk: Mapped[str] = mapped_column(
        String(32), ForeignKey("model.id", ondelete="CASCADE"), nullable=False
    )

    __table_args__ = (Index("ix_binding_unique", "agent_name", "purpose", unique=True),)


class PathWhitelist(Base, TimestampMixin):
    __tablename__ = "path_whitelist"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)

    # 属于哪个会话。NULL = 全局条目（内置项、以及用户加的全局项）。
    #
    # "这个对话能读写哪些目录"本质上是会话级的决定 —— 给 A 会话开了
    # D:\proj 的写权限，不该让 B 会话也能写。
    session_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("session.id", ondelete="CASCADE"), nullable=True, index=True
    )

    # 绝对路径，插入时就 resolve，避免每次校验都做。
    #
    # 唯一性是 (session_id, path) 复合 —— 同一个路径可以在不同会话里
    # 各有一条，权限还可能不同。
    path: Mapped[str] = mapped_column(Text, nullable=False)
    can_write: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 内置项不可删 —— 删了 agent 就完全不能读写文件，
    # 且用户不容易想到是这个原因
    builtin: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint("session_id", "path", name="uq_whitelist_session_path"),
    )
