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
from app.modules.memory import prefetch as prefetch_mod
from app.modules.memory import service as memory_service
from app.modules.memory.extract_context import ExtractContext, from_messages
from app.modules.memory.extract_input import prepare
from app.modules.memory.extract_loop import ExtractLoop, ExtractOutcome
from app.modules.memory.extract_tools import ToolRunner
from app.modules.memory.models import BatchResult, MemoryExtraction, MemoryScope, WriteOp
from app.modules.memory.schema import MemoryScopeKind, MemoryTypeSchema, OperationMode
from app.modules.memory.vectorize import VectorizeReport
from app.modules.session import repo

log = structlog.get_logger(__name__)


async def find_agents_in_session(db: AsyncSession, session_id: str) -> list[str]:
    """
    识别会话中参与过的所有智能体（按首次出现顺序）。

    ## 为什么从消息扫而不是从 session 表读

    `session.agent_id` 只是【主智能体】（创建会话时绑定的那个）。
    多智能体对话里 `message.agent_name` 可以是任意智能体，而且
    智能体可以在会话中途加入或退出 —— 维护一份"当前活跃列表"需要
    额外字段加同步逻辑，而消息本身就是唯一可靠的事实来源。

    ## 只统计【发过言】的

    只在旁观察但没发过消息的智能体不会出现在结果里 —— 那是对的：
    没有它的消息，就没有它的行为可提取，跑一次提取只会白烧 token。

    实现走 `repo.get_participating_agents`，SQL 层 GROUP BY 去重，
    只传输 agent_name 列不加载正文。
    """
    return await repo.get_participating_agents(db, session_id)


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

    # 阶段计数(多智能体时是所有智能体的总和)
    messages_loaded: int = 0
    messages_used: int = 0
    turns_held_back: int = 0
    messages_truncated: int = 0
    prefetched_items: int = 0

    outcome: ExtractOutcome | None = None
    batch: BatchResult | None = None
    vectorize: VectorizeReport | None = None
    diff_path: str = ""
    warnings: list[str] = field(default_factory=list)

    # 多智能体:每个智能体的提取结果
    agents: list[AgentReport] = field(default_factory=list)

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


async def commit_session(
    db: AsyncSession,
    *,
    session_id: str,
    agent_id: str | None = None,
    llm_call: Any,
    keep_recent_turns: int | None = None,
) -> CommitReport:
    """
    对会话中的所有智能体各提取一次。

    ## 单智能体兼容模式

    如果传了 `agent_id`，退化为单智能体模式：只给这个智能体提取，
    返回的 CommitReport 保持旧版格式（`batch`/`messages_loaded` 等字段
    有值，`agents` 为空）。这是为了让旧测试无需改动。

    ## 多智能体提取

    不传 `agent_id` 时（或传 None），识别会话中参与过的所有智能体
    （按 agent_name 去重），给每个智能体各调用一次 `commit_session_agent`。
    每个智能体看到的是:
    - 全部消息（包括用户和其他智能体的）
    - 只自己的记忆（预取和写入都隔离）
    - 只自己的 watermark（增量提取）

    llm_call 是 `async (messages) -> str`。注入而非内部构造：
    提取模型的解析走 endpoint.resolve('memory')，那属于调用方的关注点，
    而注入让测试能用一个假实现跑通整条管线。
    """
    report = CommitReport(extraction_id=new_id("ext"), session_id=session_id)

    if not settings.memory.enabled:
        report.skipped = "记忆系统已关闭（memory.enabled=false）"
        return report

    # ── 单智能体兼容模式 ──
    if agent_id is not None:
        agent_report = await commit_session_agent(
            db,
            session_id=session_id,
            agent_id=agent_id,
            llm_call=llm_call,
            keep_recent_turns=keep_recent_turns,
            use_watermark=False,  # 旧测试期待每次全量读
        )
        # 直接把 AgentReport 的字段映射回 CommitReport
        report.messages_loaded = agent_report.messages_new
        report.messages_used = agent_report.messages_new
        report.prefetched_items = agent_report.prefetched_items
        report.outcome = agent_report.outcome
        report.batch = agent_report.batch  # 完整的 batch，包含真实 uri
        report.diff_path = agent_report.diff_path
        report.warnings.extend(agent_report.warnings)
        if agent_report.warnings and not agent_report.written:
            report.skipped = agent_report.warnings[0] if agent_report.warnings else ""
        return report

    # ── 多智能体模式 ──
    agent_ids = await find_agents_in_session(db, session_id)
    if not agent_ids:
        report.skipped = "会话中没有智能体参与过"
        return report

    # 【避免并发冲突】
    #
    # global 和 session 记忆是共享的，多个智能体同时写会冲突。
    # 策略：只让**第一个智能体**提取全部三层（global + session + agent），
    # 其他智能体只提取自己的 agent 记忆（`agent_only=True`）。
    #
    # 这样：
    # - session 记忆只有一份（语义正确）
    # - 避免并发写冲突
    # - 避免重复提取
    import asyncio

    first_agent = agent_ids[0]
    other_agents = agent_ids[1:]

    # 第一个智能体：提取全部三层
    first_report = await commit_session_agent(
        db,
        session_id=session_id,
        agent_id=first_agent,
        llm_call=llm_call,
        keep_recent_turns=keep_recent_turns,
        agent_only=False,  # 提取 global + session + agent
    )

    # 其他智能体：并发提取各自的 agent 记忆
    if other_agents:
        other_tasks = [
            commit_session_agent(
                db,
                session_id=session_id,
                agent_id=aid,
                llm_call=llm_call,
                keep_recent_turns=keep_recent_turns,
                agent_only=True,  # 只提取 agent 层
            )
            for aid in other_agents
        ]
        other_reports = await asyncio.gather(*other_tasks)
        agent_reports = [first_report, *other_reports]
    else:
        agent_reports = [first_report]
    report.agents = list(agent_reports)

    log.info(
        "memory_session_commit_done",
        extraction_id=report.extraction_id,
        session_id=session_id,
        agents=len(agent_reports),
        summary=report.summary(),
    )
    return report


async def commit_session_agent(
    db: AsyncSession,
    *,
    session_id: str,
    agent_id: str,
    llm_call: Any,
    keep_recent_turns: int | None = None,
    use_watermark: bool = True,
    agent_only: bool = False,
) -> AgentReport:
    """
    对单个智能体跑一次记忆提取。

    ## 增量提取（use_watermark=True）

    读取 memory_extraction 表的 last_seq,只处理 seq > last_seq 的新消息。
    提取完更新 watermark。

    ## 全量提取（use_watermark=False）

    每次读全部消息（旧版行为）。用于兼容旧测试和单次手动提取场景。

    ## agent_only 参数

    - `False`（默认）：提取 global + session + agent 三层记忆
    - `True`：只提取 agent 层记忆，避免多智能体并发写冲突

    在多智能体场景下，只有第一个智能体会用 `agent_only=False`，
    其他智能体用 `agent_only=True`。

    ## 与旧版的差别

    旧版 `commit_session` 返回 CommitReport,包含全部阶段的计数。
    新版只负责单智能体,返回 AgentReport（更轻量）。
    多智能体编排由新的 `commit_session` 负责。

    llm_call 是 `async (messages) -> str`。注入而非内部构造：
    提取模型的解析走 endpoint.resolve('memory')，那属于调用方的关注点，
    而注入让测试能用一个假实现跑通整条管线。
    """
    from sqlalchemy import select

    # ── 0. 查水位线（可选） ──
    last_seq = -1
    extraction_count = 0
    if use_watermark:
        stmt = select(MemoryExtraction).where(
            MemoryExtraction.session_id == session_id,
            MemoryExtraction.agent_id == agent_id,
        )
        watermark_row = (await db.execute(stmt)).scalar_one_or_none()
        last_seq = watermark_row.last_seq if watermark_row else -1
        extraction_count = (watermark_row.extraction_count if watermark_row else 0) + 1

    agent_report = AgentReport(
        agent_id=agent_id,
        extraction_count=extraction_count,
        watermark_before=last_seq,
        watermark_after=last_seq,
        messages_new=0,
    )

    if not settings.memory.enabled:
        agent_report.warnings.append("记忆系统已关闭（memory.enabled=false）")
        return agent_report

    # ── 1. 加载消息（增量或全量） ──
    #
    # ## 为什么看【全部】记忆线而不只是自己的
    #
    # agent_name=None 表示不按记忆线过滤，拿到的是完整对话：用户的话、
    # 本智能体的话、其他智能体的话。这是必须的 —— 提取"用户偏好"要看
    # 用户说了什么，而那些消息的 agent_name 是空串。只看自己那条线
    # 会让偏好类记忆完全提不出来。
    #
    # ## after_seq 在 SQL 层过滤
    #
    # use_watermark=True 时只取 seq > last_seq 的新消息。
    # use_watermark=False 时取全部（兼容旧版）。
    new_rows = await repo.load_messages(
        db, session_id, agent_name=None, after_seq=last_seq if use_watermark and last_seq >= 0 else None
    )
    agent_report.messages_new = len(new_rows)

    if not new_rows:
        agent_report.warnings.append("没有新消息需要提取")
        return agent_report

    msgs = [repo.row_to_msg(r) for r in new_rows]
    stamps = [r.created_at for r in new_rows]
    new_max_seq = max(r.seq for r in new_rows)

    # ── 2. 截断 ──
    prepared = prepare(msgs, stamps, keep_recent_turns=keep_recent_turns)
    if prepared.is_empty:
        agent_report.warnings.append("截断后没有可提取的消息（对话还太短，或都在保留窗口内）")
        return agent_report

    ctx = from_messages(prepared.messages, prepared.timestamps)

    # ── 3. 预取（用截断后的消息构建搜索查询） ──
    #
    # ## 为什么用截断后的消息
    #
    # 截断是为了不提取"正在进行的对话"（最近 N 轮）。
    # 向量搜索也应该基于"可以提取的部分"：
    # - 最近 N 轮还在进行中，不应该被提取
    # - 搜索也不应该基于这些"临时上下文"召回记忆
    #
    # ## agent_only 模式
    #
    # `agent_only=True` 时只用 scope_agent（只读 agent 层），避免：
    # - 多个智能体并发写 global/session 记忆（数据竞争）
    # - 重复提取共享记忆（浪费 token）
    #
    # `agent_only=False` 时用 scope_session（读 global + session + agent），
    # 这是多智能体场景下的"第一个智能体"，或单智能体模式。
    scope_agent = MemoryScope(agent_id=agent_id)
    scope_session = MemoryScope(agent_id=agent_id, session_id=session_id)
    prefetch_scope = scope_agent if agent_only else scope_session

    # 获取嵌入模型（用于向量搜索）
    embed_model = await memory_service.resolve_embedding_model(db)
    if not embed_model:
        log.debug("memory_prefetch_no_embedding", reason="未配置嵌入模型")

    # 预取：向量搜索 + 预算感知加载
    pre = await prefetch_mod.prefetch(
        prefetch_scope,
        messages=prepared.messages,  # 截断后的消息
        db=db,
        model=embed_model,
    )
    agent_report.prefetched_items = pre.total

    # ── 4. ReAct 循环 ──
    #
    # ## 工具永远可用
    #
    # 参考 OpenViking session_extract_context_provider.py:604：
    # - eager_prefetch 只控制"预取时是否自动读 top-N"
    # - 工具（read/search）永远可用，LLM 自己决定是否需要读更多
    #
    # 之前我们的实现把 eager 和工具可用性绑定，这是错误的。
    # 即使预取了全文，LLM 也可能需要：
    # - 读取预取中被截断的记忆全文
    # - 搜索预取范围之外的记忆
    # - 读取预取列表中它认为需要的其他记忆
    schemas = memory_service.visible_types(prefetch_scope)
    tool_runner = ToolRunner(scope=prefetch_scope, pages=pre.pages, read_uris=set(pre.read_uris))

    loop = ExtractLoop(
        llm_call=llm_call,
        schemas=schemas,
        prefetched=pre,
        extract_context=ctx,
        tool_runner=tool_runner,
    )
    outcome = await loop.run()
    agent_report.outcome = outcome
    agent_report.warnings.extend(outcome.warnings)

    if not outcome.operations and not outcome.delete_page_ids:
        agent_report.warnings.append("模型判断没有值得记的内容")
        # 【watermark 仍要推进】（仅增量模式）。没提取到东西不代表这批消息要重复处理。
        if use_watermark:
            await _update_watermark(
                db, session_id, agent_id, new_max_seq, extraction_count, agent_report
            )
        return agent_report

    # ── 5. page_id → 写入操作 ──
    ops = _to_write_ops(
        outcome,
        schemas=schemas,
        pages=pre.pages,
        scope_agent=scope_agent,
        scope_session=scope_session,
        extract_context=ctx,
        warnings=agent_report.warnings,
        agent_only=agent_only,
    )

    # ── 6. 合并写入 ──
    extraction_id = new_id("ext")
    batch = await memory_service.write_many(ops, db=db, extraction_id=extraction_id)
    agent_report.batch = batch

    # ── 7. 删除 ──
    #
    # ## 同批冲突保护
    #
    # 模型可能同时"改写 X"和"删除 X"—— 它把「替换」表达成了「删旧的 + 建新的」，
    # 而两者算出同一个路径。照字面执行会把刚写好的内容删掉，
    # 净效果是这条记忆【凭空消失】，且 diff 里显示"写入成功 + 删除成功"，
    # 看不出问题。
    #
    # 正确的语义是把它当更新：保留写入，跳过删除。
    # 照抄 OpenViking 的同批保护（memory_updater.py:913-935），
    # 包括它的大小写折叠 —— Windows 和 macOS 的文件系统大小写不敏感，
    # "Testing.md" 和 "testing.md" 是同一个文件，不折叠会漏掉这种冲突。
    upserted = {u.casefold().rstrip("/") for u in (*batch.written, *batch.edited)}

    # ## 有写入失败时【整批不删】
    #
    # 写入失败意味着这批操作没有按模型的意图完整执行。
    # 而删除是不可逆的 —— 如果模型的意图是"把 A 的内容搬到 B 然后删掉 A"，
    # 而 B 写失败了，那时删掉 A 就是净数据丢失。
    #
    # 保守到底：任何写入错误都阻止全部删除。代价是留下几条该删的记忆
    # （下次提取会再删一次），收益是绝不因为半成功的批次丢数据。
    # 照抄 OpenViking 的 has_unresolved_upserts 保护（memory_updater.py:917）。
    # 失败有两个来源，都要算进来：
    #
    # 1. 写入阶段失败（patch 打不上、路径渲染失败）→ batch.results
    # 2. 【提取阶段被丢弃】→ outcome.warnings
    #
    # 第 2 个是我第一版漏的：循环在修复重试后仍失败的条目会被丢掉
    # （只保留能写的那些），于是 batch 里【没有】失败记录，
    # write_failed 是空的，删除照常执行 —— 而那正是"意图没被完整执行"
    # 的情形。测试抓到了这个洞。
    write_failed = [r for r in batch.results if not r.ok]
    dropped_by_loop = bool(outcome.warnings)

    if (write_failed or dropped_by_loop) and outcome.delete_page_ids:
        reason = "写入失败" if write_failed else "提取阶段有条目被丢弃"
        agent_report.warnings.append(
            f"本批{reason}，已跳过全部 {len(outcome.delete_page_ids)} 个删除"
            "（避免半成功批次丢数据）"
        )
        log.warning(
            "memory_deletes_skipped_incomplete_batch",
            write_failed=len(write_failed),
            dropped_by_loop=dropped_by_loop,
            skipped_deletes=len(outcome.delete_page_ids),
        )
        outcome.delete_page_ids = []

    for pid in outcome.delete_page_ids:
        uri = pre.pages.uri_of(pid)
        if not uri:
            agent_report.warnings.append(f"delete_page_ids 里的 {pid} 无效，已忽略")
            continue
        if uri.casefold().rstrip("/") in upserted:
            agent_report.warnings.append(
                f"跳过删除 {uri}：同批刚写入过（模型把「替换」表达成了「删+建」，按更新处理）"
            )
            log.info("memory_delete_skipped_same_batch", uri=uri)
            continue
        batch.deletes.append(await memory_service.delete_with_trace(uri, db=db))

    # ── 7b. supersedes：新经验取代旧经验 ──
    #
    # 同样受"写入失败则不删"保护 —— supersedes 也是删除操作。
    # 新经验没写成功却删掉了被它取代的旧经验，那是净损失。
    if not write_failed and not dropped_by_loop:
        await _apply_supersedes(
            db, batch, scope_agent=scope_agent, warnings=agent_report.warnings, upserted=upserted
        )
    elif any(_declares_supersedes(op) for op in ops):
        agent_report.warnings.append("本批不完整，已跳过 supersedes 删除")

    # ── 8. 目录索引 ──
    #
    # 【删除的类型也要刷新】。只刷新写入过的类型会留下过时的 overview ——
    # 删掉某类最后一条记忆后，overview 里仍然列着它，点进去是 404。
    #
    # OpenViking 同样把 delete 的目录并进刷新集合
    # （memory_updater.py:982-988）。
    touched = {op.memory_type for op in ops}
    touched.update(d.memory_type for d in batch.deletes if d.memory_type)

    for mtype in touched:
        schema = memory_service.get_schema(mtype)
        if schema is None or not schema.overview_template:
            continue
        scope = scope_session if schema.scope is MemoryScopeKind.SESSION else scope_agent
        await memory_service.refresh_overview(scope, mtype)

    # ── 9. 向量化 ──
    #
    # 【只增类型也要算】。events / trajectories 预取时跳过（不会被改，
    # 回顾只是白烧 token），但向量化不能跳过 —— 那是为了以后能召回。
    # 两件事目的不同。OpenViking 同样对全部 written + edited 向量化
    # （memory_updater.py:1352），只排除 overview / abstract。
    #
    # 放在痕迹之前：向量化失败不该阻止痕迹落盘，但它的结果要能进报告。
    changed_uris = [*batch.written, *batch.edited]
    if changed_uris:
        vec_report = await memory_service.vectorize(db, changed_uris)
        if vec_report.errors:
            # 嵌入失败【不算 commit 失败】—— 记忆已经写进文件了，
            # 缺的只是向量，下次 revectorize 能补上。
            agent_report.warnings.extend(vec_report.errors)

    agent_report.diff_path = await memory_service.write_diff(batch, scope=scope_agent)
    agent_report.warnings.extend(batch.errors)

    # ── 10. 更新 watermark（仅增量模式） ──
    if use_watermark:
        await _update_watermark(db, session_id, agent_id, new_max_seq, extraction_count, agent_report)

    log.info(
        "memory_agent_extraction_done",
        session_id=session_id,
        agent_id=agent_id,
        extraction_count=extraction_count,
        written=agent_report.written,
        discarded=agent_report.discarded,
    )
    return agent_report


async def _update_watermark(
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
    agent_only: bool = False,
) -> list[WriteOp]:
    """
    把模型输出转成写入操作。

    ## page_id 在这里被消化掉

    page_id 只是"引用哪条已有记忆"的手段。转成 WriteOp 时它已经完成使命 ——
    存储层按字段值算出路径，写到哪由 filename_template 决定。

    ## 为什么无效 page_id 当新建而不是报错

    模型对新记忆误填了一个 id 是常见错误。当新建处理的结果是"多了一条
    可能重复的记忆"，报错的结果是"这条信息永久丢失"。前者可以之后合并。

    ## agent_only 参数

    `True` 时只写 agent 层记忆，跳过 global 和 session 类型。
    用于多智能体场景下的非首个智能体，避免并发写冲突。
    """
    from app.modules.memory.schema import MemoryScopeKind

    by_name = {s.memory_type: s for s in schemas}
    ops: list[WriteOp] = []

    for mtype, items in outcome.operations.items():
        schema = by_name.get(mtype)
        if schema is None:
            warnings.append(f"未知记忆类型 {mtype}，已忽略 {len(items)} 条")
            continue

        # agent_only 模式下跳过 global 和 session 类型
        if agent_only and schema.scope in (MemoryScopeKind.GLOBAL, MemoryScopeKind.SESSION):
            warnings.append(f"{mtype}：agent_only 模式下跳过 {len(items)} 条 {schema.scope.value} 记忆")
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
