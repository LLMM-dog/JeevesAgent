"""
记忆系统的数据模型。

## MemoryExtraction

记录每个智能体在每个会话中已提取到哪条消息（watermark）。
用于增量提取和多智能体隔离。

## MemoryScope / MemoryItem / WriteOp / BatchResult

这些是业务模型（dataclass），不是数据库表。
数据库表只有 MemoryExtraction 和 MemoryIndex（在 models_db.py）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.modules.memory.schema import MemoryScopeKind


class MemoryExtraction(Base, TimestampMixin):
    """
    记录每个智能体在每个会话中已提取到哪条消息。

    用途:
    - 增量提取: 只处理 seq > last_seq 的新消息
    - 多智能体隔离: 每个智能体各自维护水位线
    - 前端展示: 显示"已处理到第 N 条消息"
    """

    __tablename__ = "memory_extraction"

    session_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("session.id", ondelete="CASCADE"), primary_key=True
    )
    agent_id: Mapped[str] = mapped_column(String(32), primary_key=True)

    # 已提取到的最大 seq(含)。下次提取时只拿 seq > last_seq 的消息。
    last_seq: Mapped[int] = mapped_column(Integer, nullable=False)

    # 累计提取次数(方便前端显示"第 N 次提取")
    extraction_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # 最后一次提取的元信息(JSON 文本),存 CommitReport 的序列化
    last_report: Mapped[str] = mapped_column(Text, default="{}", nullable=False)


@dataclass
class MemoryScope:
    """
    记忆的作用域：global / agent / session / peer。

    三层层级：
    - global: 全局共享（所有智能体可见）
    - agent: 智能体级（跨会话，但只该智能体可见）
    - session: 会话级（属于会话本身，只被第一个智能体代表会话修改，不按智能体隔离）

    peer 维度用于「A 眼中的 B」，与 A 自己的记忆隔离。
    """

    agent_id: str = ""
    session_id: str = ""
    peer_agent_id: str = ""

    def __post_init__(self) -> None:
        """验证 scope 的一致性"""
        if self.session_id and not self.agent_id:
            raise ValueError("session_id 必须配合 agent_id 使用")
        if self.peer_agent_id and not self.agent_id:
            raise ValueError("peer_agent_id 必须配合 agent_id 使用")
        if self.agent_id and self.peer_agent_id and self.agent_id == self.peer_agent_id:
            raise ValueError("agent_id 和 peer_agent_id 不能等于同一个值")

    @property
    def is_peer_view(self) -> bool:
        """是否是 peer 视角（agents/A/peers/B/）"""
        return bool(self.peer_agent_id)

    def allows(self, target: MemoryScopeKind) -> bool:
        """
        当前 scope 能否访问 target 层级的记忆。

        规则：
        - global scope (无 agent_id) 只能访问 GLOBAL 层
        - agent scope (有 agent_id 无 session_id) 能访问 GLOBAL + AGENT 层
        - session scope (有 agent_id + session_id) 能访问所有三层

        这是**向下兼容的层次结构**：session 能看 agent 和 global，
        但 agent 看不到 session，global 只能看自己。
        """
        # 运行时导入避免循环依赖
        from app.modules.memory.schema import MemoryScopeKind as Kind

        if target is Kind.GLOBAL:
            return True  # 所有 scope 都能访问 global

        if target is Kind.AGENT:
            return bool(self.agent_id)  # 必须有 agent_id

        if target is Kind.SESSION:
            return bool(self.agent_id and self.session_id)  # 必须两者都有

        return False


@dataclass
class MemoryItem:
    """
    一条记忆的完整内容。

    从文件读出来的（render.py），或准备写进去的（file_store.py）。
    """

    uri: str
    memory_type: str
    scope: MemoryScope
    fields: dict[str, Any]  # 业务字段（schema 定义的那些）
    body: str  # 渲染后的正文（去掉 frontmatter）
    raw_content: str  # 原始文件内容（含 frontmatter）
    version: int = 1
    created_at: int = 0
    updated_at: int = 0
    agent_id: str = ""
    session_id: str = ""
    peer_agent_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)  # 系统字段但不属于上述几类的

    @property
    def title(self) -> str:
        """
        从 fields 里提取标题。

        按优先级尝试常见的标题字段名。不同 memory_type 用不同名字：
        - experiences: experience_name
        - preferences: preference_name
        - events: event_name
        - 其他: name / title / topic

        用于展示和日志（比如测试里的 {i.title for i in items}）。
        """
        return str(
            self.fields.get("experience_name")
            or self.fields.get("preference_name")
            or self.fields.get("event_name")
            or self.fields.get("name")
            or self.fields.get("title")
            or self.fields.get("topic")
            or ""
        )

    @property
    def merge_source(self) -> str:
        """
        下一次合并该拿什么当 current。

        ## 为什么不能直接用 body

        content_template 会在 content 外面套一层壳（tool_notes 的
        "# 工具：xxx" + 计数行）。拿渲染结果当输入再渲染一次，
        壳会被【重复叠加】—— 实测 run_shell.md 长出了两个标题和两组计数行，
        version 每涨一次多一层。

        所以优先返回 raw_content（渲染前的原始值），只有没有模板时
        （raw_content 为空）才回落 body。

        OpenViking 靠 MemoryFile.content 始终是原始内容来避免这件事；
        我们把原始值单独存了一份。
        """
        return self.raw_content or self.body


@dataclass
class WriteOp:
    """
    一次写入操作的全部参数。

    ExtractLoop 产出 page_id 形式的操作，commit.py 解析成这个结构，
    再传给 service.write_many。
    """

    scope: MemoryScope
    memory_type: str
    fields: dict[str, Any]
    extract_context: Any = None  # ExtractContext，用于填充模板变量
    extraction_id: str = ""  # 提取批次 ID（同一批共享）
    trace_id: str = ""  # 痕迹链 ID（多次提取属于同一次会话提交）


@dataclass
class WriteResult:
    """单条记忆的写入结果"""

    uri: str
    memory_type: str
    changed: bool  # 内容是否真的变了
    created: bool  # 是新建的还是更新
    version: int = 1
    error: str = ""
    before: str = ""  # 写入前的正文（用于 diff）
    after: str = ""  # 写入后的正文

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class DeleteResult:
    """单条记忆的删除结果"""

    uri: str = ""
    memory_type: str = ""
    deleted_content: str = ""  # 被删除的内容（用于痕迹）
    error: str = ""

    @property
    def ok(self) -> bool:
        """成功删除时为 True"""
        return not self.error

    def __bool__(self) -> bool:
        """成功删除时为 True（兼容旧代码的 if result: 判断）"""
        return not self.error


@dataclass
class BatchResult:
    """
    一批写入操作的汇总结果。

    包含成功/失败分类、全量日志、diff 输入。
    """

    extraction_id: str
    trace_id: str = ""  # 痕迹链 ID（多次提取属于同一次会话提交）
    results: list[WriteResult] = field(default_factory=list)
    deletes: list[DeleteResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def written(self) -> list[str]:
        """新建的记忆的 uri 列表"""
        return [r.uri for r in self.results if r.created and r.ok]

    @property
    def edited(self) -> list[str]:
        """更新的记忆的 uri 列表"""
        return [r.uri for r in self.results if r.changed and not r.created and r.ok]

    @property
    def unchanged(self) -> list[str]:
        """内容未变的 uri 列表"""
        return [r.uri for r in self.results if not r.changed and r.ok]

    @property
    def discarded(self) -> int:
        """失败的条数"""
        return sum(1 for r in self.results if not r.ok)

    @property
    def ok(self) -> bool:
        """整批是否全部成功（没有失败项）"""
        return all(r.ok for r in self.results) and not self.errors

    @property
    def succeeded_uris(self) -> list[str]:
        """成功的 uri（新建+更新+未变）"""
        return [r.uri for r in self.results if r.ok]

    def to_diff(self) -> dict[str, Any]:
        """
        生成 diff 文件的输入。

        返回格式与 OpenViking 的 _extract_memory_write_result 对齐。
        """
        return {
            "extraction_id": self.extraction_id,
            "trace_id": self.trace_id,
            "extracted_at": int(time.time() * 1000),
            "written": [{"uri": uri} for uri in self.written],
            "edited": [{"uri": uri} for uri in self.edited],
            "unchanged": [{"uri": uri} for uri in self.unchanged],
            "failed": [{"uri": r.uri, "error": r.error} for r in self.results if not r.ok],
            "deletes": [
                {"uri": d.uri, "memory_type": d.memory_type, "deleted_content": d.deleted_content}
                for d in self.deletes
            ],
            "errors": [r.error for r in self.results if not r.ok] + self.errors,
            "summary": {
                "total_adds": len(self.written),
                "total_updates": len(self.edited),
                "total_deletes": len([d for d in self.deletes if d]),
                "total_unchanged": len(self.unchanged),
                "total_errors": len([r for r in self.results if not r.ok]) + len(self.errors),
            },
            "operations": {
                "adds": [
                    {
                        "uri": r.uri,
                        "memory_type": r.memory_type,
                        "after": r.after,
                    }
                    for r in self.results
                    if r.created and r.ok
                ],
                "updates": [
                    {
                        "uri": r.uri,
                        "memory_type": r.memory_type,
                        "before": r.before,
                        "after": r.after,
                    }
                    for r in self.results
                    if r.changed and not r.created and r.ok
                ],
                "unchanged": [
                    {"uri": r.uri, "memory_type": r.memory_type}
                    for r in self.results
                    if not r.changed and r.ok
                ],
                "failed": [
                    {"uri": r.uri, "memory_type": r.memory_type, "error": r.error}
                    for r in self.results
                    if not r.ok
                ],
                "deletes": [
                    {
                        "uri": d.uri,
                        "memory_type": d.memory_type,
                        "deleted_content": d.deleted_content,
                    }
                    for d in self.deletes
                ],
            },
        }


@dataclass
class ArchiveSummary:
    """
    会话归档摘要。

    参考 OpenViking 的归档机制：每次记忆提取后生成一个归档，
    包含工作记忆摘要和已处理到的消息位置。

    用途：
    - 增量提取的 watermark（last_seq）
    - 注入上次摘要到提取上下文（overview）
    - 提供给前端展示提取历史
    """

    archive_id: str  # "archive_003"
    archive_index: int  # 3
    session_id: str
    overview: str = ""  # .overview.md 的内容（工作记忆摘要）
    abstract: str = ""  # .abstract.md 的内容（摘要的抽象）
    last_seq: int = -1  # 归档中最后一条消息的 seq（用作 watermark）
    last_message_id: str = ""  # 归档中最后一条消息的 ID
    message_count: int = 0  # 归档的消息数
    created_at: int = 0  # 归档创建时间（毫秒时间戳）
    overview_tokens: int = 0  # 摘要的 token 数估算

    @property
    def archive_uri(self) -> str:
        """归档目录的 URI 路径。"""
        return f"sessions/{self.session_id}/history/{self.archive_id}"

    def is_valid(self) -> bool:
        """检查归档是否有效（有摘要且有消息）。"""
        return bool(self.overview.strip() and self.message_count > 0)
