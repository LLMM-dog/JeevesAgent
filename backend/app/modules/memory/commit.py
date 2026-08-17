"""
提取管线的编排：从会话消息到落盘的记忆。

    load 消息（DB）
      → prepare 截断        extract_input.py
      → prefetch 已有记忆   prefetch.py
      → ExtractLoop 循环    extract_loop.py
      → resolve page_id → uri/字段
      → write_many 合并写入 service.py
      → write_diff 痕迹     service.py

## 为什么单独一层

每个阶段都可以独立测试和替换。混在一起是 OpenViking 的
memory_updater.py（64KB）和 extract_loop.py（38KB）变得难以维护的原因 ——
它们把编排、存储、向量化、链接图放在同一层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.ids import new_id
from app.modules.memory import layout
from app.modules.memory import prefetch as prefetch_mod
from app.modules.memory import service as memory_service
from app.modules.memory.extract_context import ExtractContext, from_messages
from app.modules.memory.extract_input import prepare
from app.modules.memory.extract_loop import ExtractLoop, ExtractOutcome
from app.modules.memory.extract_tools import ToolRunner
from app.modules.memory.models import ArchiveSummary, BatchResult, MemoryExtraction, MemoryItem, MemoryScope, WriteOp
from app.modules.memory.schema import MemoryScopeKind, MemoryTypeSchema, OperationMode
from app.modules.session import repo

log = structlog.get_logger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# 归档读取
# ══════════════════════════════════════════════════════════════════════════════


async def get_latest_archive_summary(session_id: str) -> ArchiveSummary | None:
    """
    读取会话的最新归档摘要。

    参考 OpenViking 的实现：
    - 扫描 sessions/{session_id}/history/archive_NNN/ 目录
    - 找到最大编号的归档
    - 读取 .overview.md（摘要）和 messages.jsonl（获取 last_seq）

    返回 None 表示：
    - 会话还没有归档
    - 归档目录不存在或损坏
    """
    import json
    from pathlib import Path

    history_dir = layout.session_root(session_id) / "history"
    if not history_dir.exists():
        return None

    # 查找所有 archive_NNN 目录
    archives: list[tuple[int, Path]] = []
    for item in history_dir.iterdir():
        if item.is_dir() and item.name.startswith("archive_"):
            try:
                index = int(item.name.split("_")[1])
                archives.append((index, item))
            except (IndexError, ValueError):
                continue

    if not archives:
        return None

    # 按编号倒序，找最新的
    archives.sort(reverse=True, key=lambda x: x[0])

    for index, archive_dir in archives:
        archive_id = f"archive_{index:03d}"

        # 读取 .overview.md
        overview_file = archive_dir / ".overview.md"
        overview = ""
        if overview_file.exists():
            try:
                overview = overview_file.read_text(encoding="utf-8")
            except Exception as e:
                log.warning("read_archive_overview_failed", archive_id=archive_id, error=str(e))
                continue

        # 读取 .abstract.md
        abstract_file = archive_dir / ".abstract.md"
        abstract = ""
        if abstract_file.exists():
            try:
                abstract = abstract_file.read_text(encoding="utf-8")
            except Exception:
                pass

        # 读取 messages.jsonl 获取 last_seq 和 message_count
        messages_file = archive_dir / "messages.jsonl"
        last_seq = -1
        last_message_id = ""
        message_count = 0

        if messages_file.exists():
            try:
                lines = messages_file.read_text(encoding="utf-8").strip().split("\n")
                message_count = len([line for line in lines if line.strip()])

                # 最后一条消息
                if lines:
                    last_line = lines[-1].strip()
                    if last_line:
                        last_msg = json.loads(last_line)
                        # messages.jsonl 中的消息格式可能包含 seq 或 id
                        last_seq = last_msg.get("seq", -1)
                        last_message_id = last_msg.get("id", "")
            except Exception as e:
                log.warning("read_archive_messages_failed", archive_id=archive_id, error=str(e))
                continue

        # 读取 .meta.json 获取创建时间和 token 数
        meta_file = archive_dir / ".meta.json"
        created_at = 0
        overview_tokens = 0

        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                created_at = meta.get("created_at", 0)
                overview_tokens = meta.get("overview_tokens", 0)
            except Exception:
                pass

        # 构造 ArchiveSummary
        summary = ArchiveSummary(
            archive_id=archive_id,
            archive_index=index,
            session_id=session_id,
            overview=overview,
            abstract=abstract,
            last_seq=last_seq,
            last_message_id=last_message_id,
            message_count=message_count,
            created_at=created_at,
            overview_tokens=overview_tokens,
        )

        # 如果这个归档有效，返回它
        if summary.is_valid():
            log.info(
                "archive_summary_loaded",
                session_id=session_id,
                archive_id=archive_id,
                last_seq=last_seq,
                message_count=message_count,
            )
            return summary

    # 所有归档都无效
    return None


def _render_archive_overview(agent_reports: list[Any], watermark: int) -> str:
    """
    生成归档摘要（.overview.md 的内容）。

    摘要不是逐字对话，而是"这次提取把哪些内容变成了记忆"的清单。
    上下文加载时用它替代被归档的原始消息 —— 模型看到的是"聊过什么、
    记下了什么"，而不是几千字的原文。
    """
    lines = ["# 会话归档摘要", "", f"覆盖到 seq {watermark}", ""]
    for r in agent_reports:
        if r.batch is None:
            continue
        prefix = f"[{r.agent_id}] " if len(agent_reports) > 1 else ""
        if r.batch.written:
            lines.append(f"## {prefix}新增记忆")
            for uri in r.batch.written:
                lines.append(f"- {uri}")
        if r.batch.edited:
            lines.append(f"## {prefix}更新记忆")
            for uri in r.batch.edited:
                lines.append(f"- {uri}")
        if r.batch.deletes:
            lines.append(f"## {prefix}删除记忆")
            for d in r.batch.deletes:
                if d:
                    lines.append(f"- {d.uri}")
    if len(lines) == 3:
        lines.append("（本次没有产生记忆变更）")
    return "\n".join(lines) + "\n"


async def write_archive(
    *,
    session_id: str,
    watermark: int,
    archive_messages: list[dict[str, Any]],
    agent_reports: list[Any],
) -> str | None:
    """
    写归档目录，返回 archive_id，或 None（没有可归档的消息）。

    ## 归档目录契约（与 get_latest_archive_summary 对应）

        sessions/{session_id}/history/archive_NNN/
          messages.jsonl  归档的消息（每行 JSONL，含 seq/id/role/content）
          .overview.md    会话摘要（本次提取的记忆变更）
          .meta.json      元数据（created_at / overview_tokens / last_seq）

    ## 为什么归档要落盘而不是只更新 DB 水位线

    记忆提取和上下文加载是两套逻辑。归档目录既是"水位线"（last_seq）
    也是"摘要"（overview）的载体，落到文件系统里，加载侧只要读最新
    archive 就能同时拿到两者，不需要额外查 DB。
    """
    import json
    import time

    if not archive_messages:
        return None

    history_dir = layout.session_root(session_id) / "history"
    history_dir.mkdir(parents=True, exist_ok=True)

    # 确定下一个归档编号（扫描现有 archive_NNN 取最大 + 1）
    next_index = 1
    for item in history_dir.iterdir():
        if item.is_dir() and item.name.startswith("archive_"):
            try:
                idx = int(item.name.split("_")[1])
                next_index = max(next_index, idx + 1)
            except (IndexError, ValueError):
                continue

    archive_id = f"archive_{next_index:03d}"
    archive_dir = history_dir / archive_id
    archive_dir.mkdir(parents=True, exist_ok=True)

    # messages.jsonl
    payload = "\n".join(json.dumps(m, ensure_ascii=False) for m in archive_messages) + "\n"
    (archive_dir / "messages.jsonl").write_text(payload, encoding="utf-8")

    # .overview.md
    overview = _render_archive_overview(agent_reports, watermark)
    (archive_dir / ".overview.md").write_text(overview, encoding="utf-8")

    # .meta.json
    meta = {
        "created_at": int(time.time() * 1000),
        "last_seq": watermark,
        # 粗略 token 估算（中文约 1 token / 2 字符，这里偏保守取 /3）
        "overview_tokens": len(overview) // 3,
        "archived_count": len(archive_messages),
    }
    (archive_dir / ".meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    log.info(
        "archive_written",
        session_id=session_id,
        archive_id=archive_id,
        watermark=watermark,
        archived=len(archive_messages),
    )
    return archive_id


@dataclass
class AgentReport:
    """
    单个智能体的提取结果。包含在 CommitReport.agents 里。
    """

    agent_id: str
    extraction_count: int  # 这是该智能体在本会话中的第几次提取
    watermark_before: int  # 提取前的 last_seq
    watermark_after: int  # 提取后的 last_seq
    messages_new: int  # 本次新增的消息条数

    outcome: ExtractOutcome | None = None
    batch: BatchResult | None = None  # 完整的写入结果（包含真实 uri）
    diff_path: str = ""
    trace_uri: str = ""
    warnings: list[str] = field(default_factory=list)
    prefetched_items: int = 0  # 预取的记忆条数

    @property
    def written(self) -> int:
        return len(self.batch.written) + len(self.batch.edited) if self.batch else 0

    @property
    def discarded(self) -> int:
        return self.batch.discarded if self.batch else 0


@dataclass
class CommitReport:
    """
    一次提取的完整报告。

    每个阶段的数字都留下来 —— 排错时要能回答"是哪一步没产出内容"。
    只给最终结果的话，"提取了 0 条"可能是截断把所有消息滤掉了、
    可能是模型没找到值得记的、也可能是 patch 全部失败。

    ## 多智能体提取

    一次会话可能有多个智能体参与,每个智能体各提取一次。
    顶层 `commit_session` 返回的报告包含 `agents: list[AgentReport]`,
    每个元素是单智能体的提取结果。单智能体时 `agents` 只有一个元素。
    """

    extraction_id: str = ""
    session_id: str = ""
    skipped: str = ""  # 非空表示整次跳过，值是原因
    archive_id: str = ""  # 本次提取写出的归档 ID（archive_NNN），空表示未归档

    # 多智能体:每个智能体的提取结果
    agents: list[AgentReport] = field(default_factory=list)

    # ── 兼容属性：单智能体测试访问 ──
    #
    # 旧测试期待访问 report.batch / report.outcome 等字段。
    # 现在这些在 agents[0] 里，通过 @property 提供兼容访问。

    @property
    def batch(self) -> BatchResult | None:
        """单智能体兼容：返回第一个智能体的 batch。"""
        return self.agents[0].batch if self.agents else None

    @property
    def outcome(self) -> ExtractOutcome | None:
        """单智能体兼容：返回第一个智能体的 outcome。"""
        return self.agents[0].outcome if self.agents else None

    @property
    def diff_path(self) -> str:
        """单智能体兼容：返回第一个智能体的 diff_path。"""
        return self.agents[0].diff_path if self.agents else ""

    @property
    def messages_loaded(self) -> int:
        """单智能体兼容：返回第一个智能体的 messages_new。"""
        return self.agents[0].messages_new if self.agents else 0

    @property
    def messages_used(self) -> int:
        """单智能体兼容：返回第一个智能体的 messages_new。"""
        return self.agents[0].messages_new if self.agents else 0

    @property
    def prefetched_items(self) -> int:
        """单智能体兼容：返回第一个智能体的 prefetched_items。"""
        return self.agents[0].prefetched_items if self.agents else 0

    @property
    def warnings(self) -> list[str]:
        """多智能体：合并所有智能体的警告。"""
        if not self.agents:
            return []
        all_warnings = []
        for _i, agent in enumerate(self.agents):
            if agent.warnings:
                prefix = f"[{agent.agent_id}] " if len(self.agents) > 1 else ""
                all_warnings.extend(f"{prefix}{w}" for w in agent.warnings)
        return all_warnings

    @property
    def ok(self) -> bool:
        if self.skipped:
            return False
        if self.agents:
            return all(r.written > 0 or not r.warnings for r in self.agents)
        return self.batch is None or bool(self.batch.ok)

    def summary(self) -> str:
        if self.skipped:
            return f"跳过提取:{self.skipped}"
        if self.agents:
            parts = [f"{r.agent_id}({r.written}写/{r.discarded}弃)" for r in self.agents]
            return f"多智能体提取: {', '.join(parts)}"
        b = self.batch
        parts = [
            f"消息 {self.messages_used}/{self.messages_loaded}",
            f"预取 {self.prefetched_items}",
            f"迭代 {self.outcome.iterations if self.outcome else 0}",
        ]
        if b is not None:
            parts += [f"新建 {len(b.written)}", f"更新 {len(b.edited)}", f"未变 {len(b.unchanged)}"]
        if self.vectorize is not None and self.vectorize.attempted:
            parts.append(f"向量 {self.vectorize.succeeded}/{self.vectorize.attempted}")
        if self.warnings:
            parts.append(f"警告 {len(self.warnings)}")
        return " | ".join(parts)


async def _load_messages_for_extract(
    db: AsyncSession,
    session_id: str,
    after_seq: int | None = None,
) -> tuple[list[Any], list[int], list[int]]:
    """
    加载会话消息用于记忆提取。

    参数:
        after_seq: 只加载 seq > after_seq 的消息（增量提取）

    返回:
        - messages: 消息列表（原始，未截断）
        - timestamps: 时间戳列表（原始，未截断）
        - seqs: seq 列表（与 messages 一一对应，用于确定归档 watermark）

    注意：不在这里截断，交给调用方决定何时截断。
    这样可以在多智能体模式下只截断一次。
    """

    # 读取全部消息（agent_name=None 表示不过滤智能体）
    rows = await repo.load_messages(db, session_id, agent_name=None, after_seq=after_seq)
    if not rows:
        return [], [], []

    msgs = [repo.row_to_msg(r) for r in rows]
    stamps = [r.created_at for r in rows]
    seqs = [r.seq for r in rows]

    return msgs, stamps, seqs


async def commit_session(
    db: AsyncSession,
    *,
    session_id: str,
    llm_call: Any,
    keep_recent_turns: int | None = None,
) -> CommitReport:
    """
    对会话中的所有智能体各提取一次。这是记忆提取的唯一公开接口。

    ## 统一流程（无论单/多智能体）

    1. 加载会话消息（原始）
    2. 读取上次归档摘要（获取 watermark）
    3. 截断最近会话（keep_recent_turns）
    4. 预处理会话消息（一次）
    5. 用预处理消息向量计算，提取全局记忆（global + session，一次）
    6. 并行：为每个智能体提取私有记忆（agent）
    7. 并行：为每个智能体组装上下文并运行提取
       - 第一个智能体：全局 + session + 自己的 agent 记忆
       - 其他智能体：只有自己的 agent 记忆

    ## 多智能体记忆隔离

    - **预取阶段**：所有智能体都能看到全局/session记忆（用于参考）
    - **提取阶段**：只有第一个智能体能修改全局/session记忆
    - **系统提示词**：第一个智能体包含全局/session类型的模板

    ## 归档和 Watermark

    - 读取最新归档的 `last_seq`，只提取新消息
    - 归档摘要注入到提取上下文，让 LLM 知道"上次提取了什么"
    - 不使用 `memory_extraction` 表，文件系统是 source of truth

    参数:
        db: 数据库会话
        session_id: 会话 ID
        llm_call: LLM 调用函数 `async (messages) -> str`
        keep_recent_turns: 保留最近 N 轮对话不提取

    返回:
        CommitReport 包含所有智能体的提取结果
    """
    import asyncio

    from sqlalchemy import select

    from app.modules.memory import registry
    from app.modules.memory.prefetch import prefetch_multi_agent
    from app.modules.session.models import Session

    report = CommitReport(extraction_id=new_id("ext"), session_id=session_id)

    if not settings.memory.enabled:
        report.skipped = "记忆系统已关闭（memory.enabled=false）"
        return report

    # ── 1. 读取会话和智能体列表 ──
    stmt = select(Session).where(Session.id == session_id)
    session = (await db.execute(stmt)).scalar_one_or_none()
    if not session:
        report.skipped = "会话不存在"
        return report

    agent_ids = session.get_agent_ids()
    if not agent_ids:
        report.skipped = "会话中没有智能体"
        return report

    # ── 2. 读取归档摘要（获取 watermark 和上次摘要）──
    latest_archive = await get_latest_archive_summary(session_id)
    watermark_seq = latest_archive.last_seq if latest_archive else -1
    archive_overview = latest_archive.overview if latest_archive else ""

    if latest_archive:
        log.info(
            "using_archive_watermark",
            session_id=session_id,
            archive_id=latest_archive.archive_id,
            last_seq=watermark_seq,
        )

    # ── 3. 加载消息（只加载新消息）──
    messages, timestamps, seqs = await _load_messages_for_extract(
        db,
        session_id=session_id,
        after_seq=watermark_seq if watermark_seq >= 0 else None,
    )

    if not messages:
        report.skipped = "没有新消息需要提取"
        return report

    # ── 4. 截断（保留最近 N 轮）──
    prepared = prepare(
        messages, timestamps, keep_recent_turns=keep_recent_turns, seqs=seqs
    )
    if prepared.is_empty:
        report.skipped = "截断后没有可提取的消息"
        return report

    # ── 5. 预处理消息（用于向量搜索）──
    # 获取嵌入模型
    embedding_model = None
    try:
        from app.modules.llm import get_embedding_model
        embedding_model = await get_embedding_model(db)
    except Exception as e:
        log.debug("memory_embedding_model_unavailable", error=str(e))

    # ── 6. 两阶段预取 ──
    # 阶段1：提取共享记忆（global + session），所有智能体共享
    # 阶段2：并行提取每个智能体的私有记忆（agent scope）
    multi_prefetch = await prefetch_multi_agent(
        session_id=session_id,
        agent_ids=agent_ids,
        messages=prepared.messages,  # 使用截断后的消息
        db=db,
        model=embedding_model,
        eager=True,
    )

    log.info(
        "memory_multi_agent_prefetch_done",
        session_id=session_id,
        agents=len(agent_ids),
        shared_items=sum(len(v) for v in multi_prefetch.shared.by_type.values()),
    )

    # ── 7. 并行提取所有智能体的记忆 ──
    # 像遍历列表一样，无论1个还是N个都走同样的循环

    async def extract_agent_memory(agent_id: str, is_first: bool) -> AgentReport:
        """为单个智能体执行提取。纯内部函数。"""
        # 初始化报告
        agent_report = AgentReport(
            agent_id=agent_id,
            extraction_count=1,  # TODO: 从归档读取累计次数
            watermark_before=watermark_seq,
            watermark_after=prepared.last_seq if prepared.last_seq >= 0 else watermark_seq,
            messages_new=len(messages),
            prefetched_items=0,
        )

        # 合并该智能体的完整预取结果（共享 + 私有）
        agent_prefetch = multi_prefetch.merge_for_agent(agent_id)
        agent_report.prefetched_items = agent_prefetch.total

        # 获取该智能体可见的记忆类型
        visible_schemas = registry.get_visible_schemas(is_first)

        # 构建提取上下文
        extract_ctx = from_messages(prepared.messages, prepared.timestamps)

        # 创建工具运行器
        tool_runner = ToolRunner(
            scope=MemoryScope(agent_id=agent_id, session_id=session_id),
            pages=agent_prefetch.pages,
            read_uris=set(agent_prefetch.read_uris),
        )

        # 运行 ReAct 循环
        loop = ExtractLoop(
            llm_call=llm_call,
            schemas=visible_schemas,
            prefetched=agent_prefetch,
            extract_context=extract_ctx,
            tool_runner=tool_runner,
            is_first_agent=is_first,
            archive_overview=archive_overview,  # 注入上次摘要
        )
        outcome = await loop.run()
        agent_report.outcome = outcome
        agent_report.warnings.extend(outcome.warnings)

        # 如果没有提取到任何内容，直接返回
        if not outcome.operations and not outcome.delete_page_ids:
            agent_report.warnings.append("模型判断没有值得记的内容")
            return agent_report

        # ── 解析写入操作 ──
        scope_agent = MemoryScope(agent_id=agent_id)
        scope_session = MemoryScope(agent_id=agent_id, session_id=session_id)

        ops = _to_write_ops(
            outcome,
            schemas=visible_schemas,
            pages=agent_prefetch.pages,
            scope_agent=scope_agent,
            scope_session=scope_session,
            extract_context=extract_ctx,
            warnings=agent_report.warnings,
        )

        # ── 批量写入 ──
        extraction_id = new_id("ext")
        batch = await memory_service.write_many(ops, db=db, extraction_id=extraction_id)
        agent_report.batch = batch

        # ── 删除处理 ──
        # 同批冲突保护：写入和删除同一个 URI 时，保留写入
        upserted = {u.casefold().rstrip("/") for u in (*batch.written, *batch.edited)}

        # 写入失败时不执行删除（避免半成功批次丢数据）
        write_failed = [r for r in batch.results if not r.ok]
        dropped_by_loop = bool(outcome.warnings)

        if (write_failed or dropped_by_loop) and outcome.delete_page_ids:
            reason = "写入失败" if write_failed else "提取阶段有条目被丢弃"
            agent_report.warnings.append(
                f"本批{reason}，已跳过全部 {len(outcome.delete_page_ids)} 个删除"
            )
            log.warning(
                "memory_deletes_skipped_incomplete_batch",
                agent_id=agent_id,
                write_failed=len(write_failed),
                dropped_by_loop=dropped_by_loop,
                skipped_deletes=len(outcome.delete_page_ids),
            )
            outcome.delete_page_ids = []

        # 执行删除
        for pid in outcome.delete_page_ids:
            uri = agent_prefetch.pages.uri_of(pid)
            if not uri:
                agent_report.warnings.append(f"delete_page_ids 里的 {pid} 无效，已忽略")
                continue
            if uri.casefold().rstrip("/") in upserted:
                agent_report.warnings.append(
                    f"跳过删除 {uri}：同批刚写入过（模型把「替换」表达成了「删+建」，按更新处理）"
                )
                log.info("memory_delete_skipped_same_batch", uri=uri, agent_id=agent_id)
                continue
            batch.deletes.append(await memory_service.delete_with_trace(uri, db=db))

        # ── supersedes 处理 ──
        if not write_failed and not dropped_by_loop:
            await _apply_supersedes(
                db, batch, scope_agent=scope_agent, warnings=agent_report.warnings, upserted=upserted
            )
        elif any(_declares_supersedes(op) for op in ops):
            agent_report.warnings.append("本批不完整，已跳过 supersedes 删除")

        # ── 刷新目录索引（生成 L1 概览）──
        # 删除的类型也要刷新（避免 overview 中有指向不存在文件的链接）
        affected = {r.memory_type for r in batch.results if r.ok}
        affected.update(d.memory_type for d in batch.deletes if d)

        overview_uris = []  # 收集生成的 .overview.md URI，稍后向量化
        for mtype in affected:
            schema = next((s for s in visible_schemas if s.memory_type == mtype), None)
            if schema and schema.overview_template:
                # 刷新该类型的 overview（生成 L1）
                # 根据 schema 的 scope 选择正确的 MemoryScope
                from app.modules.memory.schema import MemoryScopeKind
                if schema.scope == MemoryScopeKind.SESSION:
                    overview_uri = await memory_service.refresh_overview(scope_session, mtype)
                else:
                    overview_uri = await memory_service.refresh_overview(scope_agent, mtype)

                if overview_uri:
                    overview_uris.append(overview_uri)
                    log.info("memory_overview_generated", agent_id=agent_id, memory_type=mtype, uri=overview_uri)

        # ── 向量化 L2 记忆 ──
        # 对新写入和更新的记忆进行向量化
        changed_uris = [r.uri for r in batch.results if r.ok and r.changed]
        if changed_uris or overview_uris:
            try:
                from app.modules.llm import get_embedding_model
                from app.modules.memory import vectorize as vec_mod

                embed_model = await get_embedding_model(db)
                if embed_model:
                    # 读取函数：从文件系统加载记忆
                    async def read_item(uri: str) -> MemoryItem | None:
                        return await memory_service.read_uri(uri)

                    # 向量化 L2（单个记忆文件）
                    if changed_uris:
                        vec_report = await vec_mod.vectorize_uris(
                            db=db,
                            uris=changed_uris,
                            model=embed_model,
                            read_item=read_item,
                        )
                        log.info(
                            "memory_l2_vectorized",
                            agent_id=agent_id,
                            attempted=vec_report.attempted,
                            succeeded=vec_report.succeeded,
                            skipped=vec_report.skipped,
                        )

                    # 向量化 L1（.overview.md）
                    if overview_uris:
                        vec_report_l1 = await vec_mod.vectorize_uris(
                            db=db,
                            uris=overview_uris,
                            model=embed_model,
                            read_item=read_item,
                        )
                        log.info(
                            "memory_l1_vectorized",
                            agent_id=agent_id,
                            attempted=vec_report_l1.attempted,
                            succeeded=vec_report_l1.succeeded,
                            skipped=vec_report_l1.skipped,
                        )
                else:
                    log.debug("memory_vectorize_skipped_no_model", count=len(changed_uris) + len(overview_uris))
            except Exception as e:
                log.warning("memory_vectorize_failed", agent_id=agent_id, error=str(e))

        # ── 写入 diff 痕迹 ──
        # 用 scope_session 而非 scope_agent：一次提取是会话级的，痕迹要带上
        # session_id 才能按会话过滤（追踪页按会话查看提取历史）。
        if batch.results or batch.deletes:
            agent_report.diff_path = await memory_service.write_diff(batch, scope=scope_session)

        return agent_report

    # 并行提取所有智能体（像遍历列表一样）
    tasks = [extract_agent_memory(agent_id, is_first=(i == 0)) for i, agent_id in enumerate(agent_ids)]
    agent_reports = await asyncio.gather(*tasks)
    report.agents = list(agent_reports)

    # ── 归档：把已提取的消息从上下文移除，用摘要替代 ──
    #
    # 只有提取覆盖到的消息（seq <= watermark）才归档；held_back（最近 N 轮）
    # 还没结束，保留在 live 消息里下次再处理。归档后 load_context 只加载
    # seq > watermark 的消息 + 注入 archive 的 .overview.md 摘要，
    # token 占用随之下降。
    watermark = prepared.last_seq
    if watermark >= 0:
        archive_messages = [
            {
                "seq": seq,
                "id": messages[i].message_id or "",
                "role": messages[i].role,
                "content": messages[i].content or "",
            }
            for i, seq in enumerate(seqs)
            if seq <= watermark
        ]
        try:
            archive_id = await write_archive(
                session_id=session_id,
                watermark=watermark,
                archive_messages=archive_messages,
                agent_reports=agent_reports,
            )
            report.archive_id = archive_id or ""
        except Exception as e:
            # 归档失败不能拖垮提取 —— 记忆已经写进文件了，缺的只是
            # "移出上下文"这一步，下次提取会重新处理（幂等）。
            log.warning("archive_write_failed", session_id=session_id, error=str(e))

    log.info(
        "memory_session_commit_done",
        extraction_id=report.extraction_id,
        session_id=session_id,
        agents=len(agent_reports),
        archive_id=report.archive_id,
        watermark=watermark,
        summary=report.summary(),
    )
    return report


async def commit_session_agent(
    db: AsyncSession,
    session_id: str,
    agent_id: str,
    new_seq: int,
    extraction_count: int,
    report: AgentReport,
) -> None:
    """更新或插入 watermark 记录。"""
    import json
    import time

    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    report.watermark_after = new_seq

    stmt = (
        sqlite_insert(MemoryExtraction)
        .values(
            session_id=session_id,
            agent_id=agent_id,
            last_seq=new_seq,
            extraction_count=extraction_count,
            last_report=json.dumps({"written": report.written, "discarded": report.discarded}),
            created_at=int(time.time() * 1000),
            updated_at=int(time.time() * 1000),
        )
        .on_conflict_do_update(
            index_elements=["session_id", "agent_id"],
            set_={
                "last_seq": new_seq,
                "extraction_count": extraction_count,
                "last_report": json.dumps({"written": report.written, "discarded": report.discarded}),
                "updated_at": int(time.time() * 1000),
            },
        )
    )
    await db.execute(stmt)
    await db.commit()


def _declares_supersedes(op: WriteOp) -> bool:
    """这条写入操作有没有声明 supersedes。只用于警告文案。"""
    return bool(str(op.fields.get("supersedes") or "").strip())


async def _apply_supersedes(
    db: AsyncSession,
    batch: BatchResult,
    *,
    scope_agent: MemoryScope,
    warnings: list[str],
    upserted: set[str],
) -> None:
    """
    处理 supersedes：新写入的记忆声明它取代了另一条，就把旧的删掉。

    ## 为什么需要它

    经验会逐步泛化。第一次记的是"pytest 挂住时查 CancelledError"，
    后来发现更普适的规律是"任何 await 被裸 except 包住都会挂"——
    第二条完全覆盖第一条，但【名字不同】所以不会被 upsert 合并。

    不处理的话两条并存，召回时注入一条窄的和一条宽的，
    而窄的那条会误导（让模型以为只有 pytest 场景才需要检查）。

    ## 为什么删而不是留个"已过时"标记

    留标记要求召回层理解这个标记，多一处耦合。而经验被取代后
    没有保留价值 —— 它的内容已经包含在新的那条里了。
    删除有痕迹（deleted_content 存全文），需要时能从 diff 里找回。

    ## 为什么不在 write 里做

    supersedes 指向的是【另一条记忆】，而 write 的职责边界是单条。
    在 write 里做会让"写一条"变成"可能删另一条"，那个副作用很难预期。
    """
    for result in batch.results:
        if not result.ok or not result.changed:
            continue

        item = await memory_service.read_uri(result.uri)
        if item is None:
            continue
        old_name = str(item.fields.get("supersedes") or "").strip()
        if not old_name:
            continue

        # 自引用：模型有时把自己的名字填进去。忽略而非删掉刚写的那条。
        if old_name == item.title:
            warnings.append(f"{result.memory_type}：supersedes 指向自己（{old_name}），已忽略")
            continue

        old = await memory_service.get(scope_agent, result.memory_type, old_name)
        if old is None:
            # 旧的不存在不是错误 —— 模型可能记错名字，或它已被别处删掉。
            warnings.append(f"supersedes 指向的 {old_name} 不存在，已忽略")
            continue

        # 【按 uri 而非 title 判同批冲突】。
        #
        # 只比 title 不够：两个不同的名字可能渲染出同一个路径
        # （filename_template 会做规范化，"Foo Bar" 和 "foo_bar" 都可能
        # 变成 foo_bar.md）。那时删掉"旧的"实际是删掉刚写的那条。
        if old.uri.casefold().rstrip("/") in upserted:
            warnings.append(
                f"跳过 supersedes 删除 {old.uri}：它与本批刚写入的记忆是同一个文件"
            )
            continue

        deleted = await memory_service.delete_with_trace(old.uri, db=db)
        batch.deletes.append(deleted)
        log.info(
            "memory_superseded",
            new_uri=result.uri,
            old_uri=old.uri,
            memory_type=result.memory_type,
        )


def _to_write_ops(
    outcome: ExtractOutcome,
    *,
    schemas: list[MemoryTypeSchema],
    pages: prefetch_mod.PageMap,
    scope_agent: MemoryScope,
    scope_session: MemoryScope,
    extract_context: ExtractContext,
    warnings: list[str],
) -> list[WriteOp]:
    """
    把模型输出转成写入操作。

    ## page_id 在这里被消化掉

    page_id 只是"引用哪条已有记忆"的手段。转成 WriteOp 时它已经完成使命 ——
    存储层按字段值算出路径，写到哪由 filename_template 决定。

    ## 为什么无效 page_id 当新建而不是报错

    模型对新记忆误填了一个 id 是常见错误。当新建处理的结果是"多了一条
    可能重复的记忆"，报错的结果是"这条信息永久丢失"。前者可以之后合并。

    ## Scope 隔离

    每个智能体只能写入自己作用域内的记忆：
    - Global：所有智能体共享（如系统配置）
    - Session：会话内所有智能体共享（如对话摘要）
    - Agent：智能体私有（如个人偏好）

    写入冲突由文件系统的原子性和版本号机制保护。
    """

    by_name = {s.memory_type: s for s in schemas}
    ops: list[WriteOp] = []

    for mtype, items in outcome.operations.items():
        schema = by_name.get(mtype)
        if schema is None:
            warnings.append(f"未知记忆类型 {mtype}，已忽略 {len(items)} 条")
            continue

        # 根据 schema.scope 选择正确的 scope
        if schema.scope is MemoryScopeKind.SESSION:
            scope = scope_session
        elif schema.scope is MemoryScopeKind.GLOBAL:
            scope = MemoryScope()  # global scope 没有 agent_id
        else:  # AGENT
            scope = scope_agent

        for item in items:
            fields = {k: v for k, v in item.items() if k != "page_id"}
            if not _has_content(schema, fields):
                warnings.append(f"{mtype}：一条记忆没有任何有效字段，已跳过")
                continue

            pid = item.get("page_id")
            if pid not in (None, 0, "") and not pages.resolve(pid):
                warnings.append(f"{mtype}：page_id={pid} 无效，当作新建处理")

            # update_only 的类型如果没给 page_id，写入时会因为目标不存在而跳过，
            # 那是符合语义的，不在这里拦。
            if schema.operation_mode is OperationMode.ADD_ONLY:
                fields.pop("page_id", None)

            ops.append(
                WriteOp(
                    scope=scope,
                    memory_type=mtype,
                    fields=fields,
                    extract_context=extract_context,
                )
            )
    return ops


def _has_content(schema: MemoryTypeSchema, fields: dict[str, Any]) -> bool:
    """
    这条记忆有没有实际内容。

    模型偶尔输出 `{"page_id": null}` 这样的空壳。写进去会产生一个
    只有 frontmatter 的空文件 —— 那比不写更糟，因为它会出现在列表里
    占位，而点开是空的。
    """
    for field_def in schema.llm_fields():
        value = fields.get(field_def.name)
        if isinstance(value, dict):  # patch 结构
            blocks = value.get("blocks") or []
            if any((b or {}).get("replace") for b in blocks if isinstance(b, dict)):
                return True
        elif isinstance(value, str) and value.strip():
            return True
        elif isinstance(value, int | float) and value:
            return True
    return False
