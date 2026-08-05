"""
span 异步写入。

## 第一原则：追踪写入永不影响主流程

一条值得记住的原则：

> Observability must never affect pi execution.

反面教材：追踪写入失败会
`raise HTTPException` —— **整段对话被打断**，因为记日志失败。

## 落地做法

1. 业务路径只做 `put_nowait`，不 await 数据库
2. 队列满时**丢最老的并计数**，不阻塞、不抛错
3. consumer 整体包 try/except，DB 异常只 warning，绝不上抛
4. 用**独立 session**，不复用请求级 session

第 4 点特别重要：复用请求级 session 的话，追踪写入失败会让业务事务
一起回滚 —— 这就成了"记日志失败导致用户消息丢了"。

## 为什么单 consumer

天然串行，不需要额外锁。同类实现 是同样思路。
span 的写入量不大（一次带 3 次工具调用的对话约 8-15 条），
单 consumer 完全够。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.time import now_ms
from app.modules.trace.models import Run, Span
from app.modules.trace.redact import redact

log = structlog.get_logger(__name__)

# 队列上限。满了丢最老的。
#
# 1000 条约等于 60~100 次对话的量，正常情况下永远不会满 ——
# 满了说明数据库卡住了，这时丢追踪数据是正确的选择。
QUEUE_MAX = 1000

# preview 双阈值：字节和行数，先到先算。
#
# 比 同类实现 保守（它那是工具输出，一次对话几条；span 行数多得多）。
PREVIEW_MAX_BYTES = 8 * 1024
PREVIEW_MAX_LINES = 200


def make_preview(text: str) -> tuple[str, bool, int]:
    """
    返回 (脱敏且截断后的文本, 是否截断, 原始字节数)。

    **先脱敏再截断**。顺序反了的话，截断点可能把一个密钥切成两半，
    前半段仍然是明文的一部分，而正则再也匹配不到它。
    """
    if not text:
        return "", False, 0
    raw_bytes = len(text.encode("utf-8"))
    safe = redact(text)

    truncated = False
    lines = safe.split("\n")
    if len(lines) > PREVIEW_MAX_LINES:
        safe = "\n".join(lines[:PREVIEW_MAX_LINES])
        truncated = True

    encoded = safe.encode("utf-8")
    if len(encoded) > PREVIEW_MAX_BYTES:
        safe = encoded[:PREVIEW_MAX_BYTES].decode("utf-8", errors="ignore")
        truncated = True

    return safe, truncated, raw_bytes


@dataclass
class SpanRecord:
    """一条待写入的 span。字段与表对齐。"""

    id: str
    run_id: str
    session_id: str
    kind: str
    name: str
    started_at: int
    parent_span_id: str | None = None
    depth: int = 0
    agent_name: str = ""
    status: str = "ok"
    ended_at: int | None = None
    duration_ms: int | None = None
    input_text: str = ""
    output_text: str = ""
    model_id: str = ""
    provider_name: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int = 0
    price_in_per_1m: float | None = None
    price_out_per_1m: float | None = None
    cost_usd: float = 0.0
    error: str = ""


@dataclass
class RunRecord:
    id: str
    session_id: str
    started_at: int
    parent_run_id: str | None = None
    agent_name: str = ""
    status: str = "running"
    stop_reason: str = ""
    ended_at: int | None = None
    duration_ms: int | None = None
    turns: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int = 0
    cost_usd: float = 0.0
    error: str = ""


@dataclass
class _Stats:
    written: int = 0
    dropped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


class TraceWriter:
    def __init__(self, sessionmaker: async_sessionmaker[Any]) -> None:
        self._sm = sessionmaker
        self._q: asyncio.Queue[SpanRecord | RunRecord] = asyncio.Queue(
            maxsize=QUEUE_MAX
        )
        self._task: asyncio.Task[None] | None = None
        self.stats = _Stats()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        """
        停止前把队列排空。

        不排空的话最后几条 span 会丢 —— 而那几条往往正是排查崩溃时最需要的。
        """
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._q.join(), timeout=5.0)
        except TimeoutError:
            log.warning("trace_flush_timeout", pending=self._q.qsize())
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    def submit(self, rec: SpanRecord | RunRecord) -> None:
        """
        业务路径唯一入口。同步、不阻塞、永不抛异常。

        注意这不是 async —— 调用方不需要 await，也就不可能因为追踪而变慢。
        """
        try:
            self._q.put_nowait(rec)
        except asyncio.QueueFull:
            # 丢最老的。新的 span 比旧的有用 —— 排查问题时看的是最近发生的。
            try:
                self._q.get_nowait()
                self._q.task_done()
                self.stats.dropped += 1
                self._q.put_nowait(rec)
            except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                self.stats.dropped += 1

    async def _consume(self) -> None:
        while True:
            rec = await self._q.get()
            try:
                await self._write(rec)
                self.stats.written += 1
            except asyncio.CancelledError:
                self._q.task_done()
                raise
            except Exception as e:  # noqa: BLE001
                # 【绝不上抛】。这是整个模块存在的理由 ——
                # 追踪失败只记 warning，业务继续跑。
                self.stats.failed += 1
                if len(self.stats.errors) < 10:
                    self.stats.errors.append(str(e))
                log.warning("trace_write_failed", err=str(e), kind=type(rec).__name__)
            finally:
                self._q.task_done()

    async def _write(self, rec: SpanRecord | RunRecord) -> None:
        # 独立 session。复用请求级 session 的话，追踪写入失败会让业务事务
        # 一起回滚 —— 那就成了"记日志失败导致用户消息丢了"。
        async with self._sm() as db:
            if isinstance(rec, RunRecord):
                await self._upsert_run(db, rec)
            else:
                self._insert_span(db, rec)
            await db.commit()

    def _insert_span(self, db: Any, rec: SpanRecord) -> None:
        in_prev, in_trunc, in_bytes = make_preview(rec.input_text)
        out_prev, out_trunc, out_bytes = make_preview(rec.output_text)
        db.add(
            Span(
                id=rec.id,
                run_id=rec.run_id,
                session_id=rec.session_id,
                parent_span_id=rec.parent_span_id,
                depth=rec.depth,
                kind=rec.kind,
                name=rec.name[:128],
                agent_name=rec.agent_name,
                status=rec.status,
                started_at=rec.started_at,
                ended_at=rec.ended_at,
                duration_ms=rec.duration_ms,
                input_preview=in_prev,
                input_truncated=in_trunc,
                input_bytes=in_bytes,
                output_preview=out_prev,
                output_truncated=out_trunc,
                output_bytes=out_bytes,
                model_id=rec.model_id[:128],
                provider_name=rec.provider_name[:64],
                input_tokens=rec.input_tokens,
                output_tokens=rec.output_tokens,
                cache_read_tokens=rec.cache_read_tokens,
                cache_write_tokens=rec.cache_write_tokens,
                reasoning_tokens=rec.reasoning_tokens,
                total_tokens=rec.total_tokens,
                price_in_per_1m=rec.price_in_per_1m,
                price_out_per_1m=rec.price_out_per_1m,
                cost_usd=rec.cost_usd,
                error=redact(rec.error)[:4000],
            )
        )

    async def _upsert_run(self, db: Any, rec: RunRecord) -> None:
        from sqlalchemy import select

        row = (
            await db.execute(select(Run).where(Run.id == rec.id))
        ).scalar_one_or_none()

        if row is None:
            # rollup 字段必须显式给 0。
            #
            # SQLAlchemy 的 default= 只在 flush 时生效，此刻新建的对象上
            # 这两个字段是 None —— 后面 max(None, x) 直接 TypeError。
            # 这个坑只在"新建 run 的同一次调用里就要读它"时才暴露。
            row = Run(
                id=rec.id,
                session_id=rec.session_id,
                started_at=rec.started_at,
                rollup_total_tokens=0,
                rollup_cost_usd=0.0,
            )
            db.add(row)

        row.parent_run_id = rec.parent_run_id
        row.agent_name = rec.agent_name
        row.status = rec.status
        row.stop_reason = rec.stop_reason
        row.ended_at = rec.ended_at
        row.duration_ms = rec.duration_ms
        row.turns = rec.turns
        row.input_tokens = rec.input_tokens
        row.output_tokens = rec.output_tokens
        row.cache_read_tokens = rec.cache_read_tokens
        row.cache_write_tokens = rec.cache_write_tokens
        row.reasoning_tokens = rec.reasoning_tokens
        row.total_tokens = rec.total_tokens
        row.cost_usd = rec.cost_usd
        row.error = redact(rec.error)[:4000]

        # 自身的 rollup 至少等于自己
        row.rollup_total_tokens = max(row.rollup_total_tokens, rec.total_tokens)
        row.rollup_cost_usd = max(row.rollup_cost_usd, rec.cost_usd)

        # ── 子 run 结束时向上累加 ──
        #
        # 常见实现在这里全部不合格：在前端内存累加、
        # pi 在展示层重建、没实现。共同结果是
        # 【后端无法回答"这次任务总共花了多少钱"】。
        if rec.parent_run_id and rec.status != "running":
            await self._rollup(db, rec.parent_run_id, rec.total_tokens, rec.cost_usd)

    async def _rollup(
        self, db: Any, run_id: str, tokens: int, cost: float, _depth: int = 0
    ) -> None:
        """逐级向上累加。防御成环，最多爬 8 层。"""
        if _depth > 8:  # pragma: no cover
            log.warning("trace_rollup_too_deep", run_id=run_id)
            return
        from sqlalchemy import select

        parent = (
            await db.execute(select(Run).where(Run.id == run_id))
        ).scalar_one_or_none()
        if parent is None:
            # 父 run 还没落库（子 run 先结束的竞态）。丢掉这次上卷 ——
            # 不重试、不建占位行。追踪数据不值得为准确性引入复杂度。
            return
        parent.rollup_total_tokens += tokens
        parent.rollup_cost_usd += cost
        if parent.parent_run_id:
            await self._rollup(
                db, parent.parent_run_id, tokens, cost, _depth + 1
            )


_writer: TraceWriter | None = None


def get_writer() -> TraceWriter | None:
    return _writer


def init_writer(sessionmaker: async_sessionmaker[Any]) -> TraceWriter:
    global _writer
    _writer = TraceWriter(sessionmaker)
    _writer.start()
    return _writer


async def shutdown_writer() -> None:
    global _writer
    if _writer is not None:
        await _writer.stop()
        _writer = None


def submit(rec: SpanRecord | RunRecord) -> None:
    """
    模块级便捷入口。writer 未初始化时静默丢弃 ——
    测试和脚本不该被迫初始化追踪。
    """
    w = _writer
    if w is not None:
        w.submit(rec)


def now() -> int:
    return now_ms()
