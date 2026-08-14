"""
记忆索引表。

## 为什么记忆存文件却要一张 SQL 表

文件适合存内容，不适合查。这张表只存元数据不存正文，职责四件：

1. 列举与筛选（"这个智能体有哪些经验"）不必 rglob 整个目录再逐个解 frontmatter
2. 热度统计（active_count 每次召回命中 +1）需要频繁小写入。
   写进 Markdown frontmatter 会让 git diff 全是计数器噪音
3. 删会话时按 session_id 找出该清理的文件
4. 嵌入模型漂移检测：换了嵌入模型后旧向量全部失效，
   不检测就会静默算出错误的相似度

【文件是真源，索引是缓存】。冲突时永远相信文件，索引可以随时
从文件重建（rebuild_index）。这条不能反。
"""

from sqlalchemy import BigInteger, Index, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.db.base import Base, TimestampMixin


class MemoryIndex(Base, TimestampMixin):
    __tablename__ = "memory_index"

    # 相对 data/memory/ 的 POSIX 路径，如
    # "agents/adf_xxx/preferences/testing.md"。
    #
    # 【用相对路径而非绝对路径做主键】：绝对路径含项目根目录，
    # 把项目整体移动或换机器后全表失效，而记忆文件本身还在。
    uri: Mapped[str] = mapped_column(Text, primary_key=True)

    # global | agent | session。与 MemoryScopeKind 对应。
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    memory_type: Mapped[str] = mapped_column(String(64), nullable=False)

    # 空串 = 不适用（global 域没有 agent_id）。
    #
    # 用空串而非 NULL：SQLite 比较 NULL 要用 IS NULL，空串可以直接 == ""，
    # 少一类查询写错的机会。与 message.agent_name 的取法一致。
    agent_id: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    session_id: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    peer_agent_id: Mapped[str] = mapped_column(String(32), default="", nullable=False)

    # 列表展示用。取 title / name / event_name 之一，回落文件名。
    title: Mapped[str] = mapped_column(Text, default="", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # 召回命中次数。热度分的频率分量。
    active_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # 正文 + 业务字段的哈希。
    #
    # 【幂等写入靠它】：合并后哈希不变就不写盘、version 不递增。
    # 没有它的话每次 commit 都产生一堆无意义的 version 跳动和 git diff。
    content_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    # 向量化时用的嵌入模型与维度。
    #
    # 换模型后维度变化，旧向量全部失效。不记的话相似度计算会静默出错 ——
    # 那是最难发现的一类 bug：召回还在返回结果，只是结果没有意义。
    #
    # 用户可以随时换嵌入模型，所以这两个字段是【重算的触发依据】：
    # 当前配置的模型/维度与行里存的不一致 → 这条的向量作废，需要重算。
    embedding_model: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    embedding_dim: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # 向量本体。float32 紧凑二进制，不是 JSON。
    #
    # ## 为什么用 BLOB 而不是 JSON 或独立的 vector 表
    #
    # 一个 1024 维向量存成 JSON 文本约 12KB（每个浮点数带小数点和逗号），
    # 存成 float32 BLOB 是 4KB —— 三倍差距，而记忆可能有几千条。
    # 而且 JSON 每次读都要解析，那是纯 CPU 浪费。
    #
    # 不建独立的 vector 表：向量与索引行【一对一且同生命周期】，
    # 拆表只会让每次召回多一次 JOIN。也不引入 sqlite-vss 这类扩展 ——
    # 它需要编译安装，破坏"clone 下来能跑"这条底线，而几千条记忆
    # 用 Python 算余弦足够快（实测 3000 条 1024 维约 25ms）。
    #
    # 【向量不进记忆文件】：它是派生数据、不可读、且换模型就失效。
    # 写进 frontmatter 会让 git diff 里出现 4KB 的乱码。
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, default=None, nullable=True)

    # 向量对应的正文哈希。
    #
    # 与 content_hash 分开：content_hash 是"文件内容变没变"，
    # 这个是"向量算的是哪一版内容"。两者不同时说明记忆改过但向量没重算 ——
    # 那时召回用的是旧语义，是个必须能被发现的状态。
    embedded_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    # 文件的 updated_at。与 TimestampMixin 的 updated_at 分开 ——
    # 后者是索引行的更新时间，重建索引时会变，而这个跟着文件走。
    file_updated_at: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    __table_args__ = (
        # 列举一个智能体某类记忆：WHERE agent_id=? AND memory_type=?
        Index("ix_memory_index_owner", "agent_id", "memory_type"),
        # 删会话时清理：WHERE session_id=?
        Index("ix_memory_index_session", "session_id"),
        # 按 scope 筛选（"有哪些全局记忆"）
        Index("ix_memory_index_scope", "scope", "memory_type"),
    )
