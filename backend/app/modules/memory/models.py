"""
长期记忆模型。

## 与常见实现的关系

只有 实现了真正的跨会话长期记忆。`memory_agent_node.py`
是 **0 字节空文件**，它唯一的记忆表 `shortterm_memory` 索引带
`conversation_uid`，天然不跨会话；同类实现 走静态 `AGENTS.md` 路线，模型只读不写。

所以这块主要参考 ，但避开它的两个缺陷（`lru_cache` 永不失效、
LLM 检索 O(记忆总量)）。

## 为什么用数据库而不是 YAML

存 `config/personas/memory.yaml`，靠 `portalocker` 文件锁保证
并发。本项目已经有 SQLite，再引入一套文件锁没有收益 —— 而且记忆需要按
theme 过滤、按 hit 排序、按时间范围查，这些用 SQL 一行就够，用 YAML 得
全量加载到内存再筛。
"""

from __future__ import annotations

from sqlalchemy import Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.db.base import Base, TimestampMixin


class Memory(Base, TimestampMixin):
    """
    一条长期记忆。

    ## history 是这张表最重要的字段

    记忆一定会记错。排查"AI 为什么以为我喜欢 X"时，`history` 是唯一线索 ——
    没有它只能看到当前状态，无法知道它怎么来的、要不要信。

    少见实现做了这个设计，
    而且它把 reason 做成了 update/delete/merge 三个工具的**必需参数**。
    这条直接照抄。
    """

    __tablename__ = "memory"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)

    # 记忆正文。一句话一件事 —— 一条塞多件事会让后续的更新和删除
    # 无法精确操作（想改其中一件就得重写整条）。
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # 分类主题。用于召回时缩小候选集，避免全量注入 LLM。
    #
    # 不做成枚举：主题是随用户领域长出来的（写代码的人和写小说的人
    # 需要的分类完全不同），固定枚举一定不够用。
    theme: Mapped[str] = mapped_column(String(64), default="其他", nullable=False)

    # 变更历史，JSON 数组。每条含 op / reason / before / at。
    #
    # 存 JSON 而不是开独立表：它只跟着这条记忆读写，从不单独查询，
    # 独立表只会带来一次多余的 join。
    history: Mapped[str] = mapped_column(Text, default="[]", nullable=False)

    # 引用计数。召回命中时递增，用于"宽泛查询优先给高频记忆"。
    hit: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_hit_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 置信度。常见实现没有这个字段。
    #
    # 记忆是模型提炼的，一定有错。没有置信度的话，"用户随口说过一次"和
    # "反复确认过"在召回时权重相同 —— 而前者恰恰是记忆污染的主要来源。
    #
    # 用户手动添加/编辑的记为 1.0，模型自动提炼的默认 0.6，
    # 被再次确认时提升。
    confidence: Mapped[float] = mapped_column(Float, default=0.6, nullable=False)

    # 来源：auto（模型提炼）/ manual（用户手写）/ tool（模型主动调工具记）
    #
    # 溯源的第一层。用户看到一条错误记忆时，首先想知道的是
    # "这是我说的还是它自己编的"。
    source: Mapped[str] = mapped_column(String(16), default="auto", nullable=False)

    # 提炼自哪个会话。用于"这条记忆是哪次对话产生的"回溯。
    # 会话被删除后保留这个值（不做外键级联）—— 记忆比会话活得长，
    # 加了 CASCADE 会导致清理旧会话时记忆一起消失。
    origin_session_id: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    agent_id: Mapped[str] = mapped_column(String(32), default="", nullable=False)

    # 归档而非删除。用户"删掉"一条记忆时置位，仍可在界面里查看和恢复。
    #
    # 真删的问题：模型下一轮可能重新提炼出同一条，用户得反复删。
    # 归档保留了"这条被否决过"的信息，可以据此避免重复写入。
    archived_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        # 召回主路径：先按 theme 过滤，未归档的，按命中数和时间排
        Index("ix_memory_recall", "archived_at", "theme", "hit"),
        # 列表页按更新时间倒序
        Index("ix_memory_updated", "archived_at", "updated_at"),
    )
