"""
追踪查询与清理。

## 数据量与 TTL

span 表的增长速度是消息表的 5~10 倍（一次带 3 次工具调用的对话约
8~15 条 span）。常见实现没做 TTL —— 它们的追踪数据要么在内存里
（丢了就丢了），要么在日志文件里（靠 logrotate）。

落库了就必须管清理，否则这张表会无声长到几百 MB。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import now_ms
from app.modules.trace.models import Run, Span

log = structlog.get_logger(__name__)

DEFAULT_RETAIN_DAYS = 14


@dataclass
class SpanNode:
    span: Span
    children: list[SpanNode]


async def span_token_totals(db: AsyncSession, run_id: str) -> dict[str, Any]:
    """
    从 span 汇总真实用量。

    ## 为什么不直接用 run.total_tokens

    子代理与父代理**共享同一个 run_id**（它是同一次用户输入触发的），
    所以库里没有独立的子 run 行 —— `rollup_total_tokens` 也就永远等于
    `total_tokens`，字段名承诺了"含子代理"但实际没有。

    这正是常见实现共同的毛病：在前端内存累加、pi 在展示层重建、
    没实现，结果**后端答不出"这次任务总共花了多少钱"**。

    span 表里已经有全部数据（含子代理的 llm span），所以直接从它汇总。
    保留 `rollup_*` 字段是给真正跨 run 的场景（将来独立子会话）用的。

    ## 只统计 llm 类 span

    agent 类 span 上也挂了 token（它记的是子代理 loop 的累计），
    和它自己的 llm 子 span 重复。只算 llm 就不会重复计。
    """
    rows = list(
        (
            await db.execute(
                select(
                    Span.agent_name,
                    func.sum(Span.total_tokens),
                    func.sum(Span.cost_usd),
                    func.count(),
                )
                .where(Span.run_id == run_id, Span.kind == "llm")
                .group_by(Span.agent_name)
            )
        ).all()
    )
    by_agent = [
        {
            "agent_name": r[0] or "main",
            "total_tokens": int(r[1] or 0),
            "cost_usd": round(float(r[2] or 0.0), 8),
            "llm_calls": int(r[3] or 0),
        }
        for r in rows
    ]
    return {
        "total_tokens": sum(a["total_tokens"] for a in by_agent),
        "cost_usd": round(sum(a["cost_usd"] for a in by_agent), 8),
        # 按智能体拆开 —— "委派花了多少"是委派值不值的唯一依据
        "by_agent": sorted(by_agent, key=lambda a: -a["total_tokens"]),
    }


async def list_runs(
    db: AsyncSession, *, session_id: str | None = None, limit: int = 50
) -> list[Run]:
    q = select(Run).order_by(Run.started_at.desc()).limit(min(limit, 200))
    if session_id:
        q = q.where(Run.session_id == session_id)
    return list((await db.execute(q)).scalars())


async def get_run(db: AsyncSession, run_id: str) -> Run | None:
    return (
        await db.execute(select(Run).where(Run.id == run_id))
    ).scalar_one_or_none()


async def get_span_tree(db: AsyncSession, run_id: str) -> list[SpanNode]:
    """
    还原 span 树。

    一次查全部再在内存里建树 —— 不做递归查询。span 数量是十几条量级，
    递归 CTE 的复杂度不值得。
    """
    rows = list(
        (
            await db.execute(
                select(Span)
                .where(Span.run_id == run_id)
                .order_by(Span.started_at.asc())
            )
        ).scalars()
    )
    nodes: dict[str, SpanNode] = {r.id: SpanNode(span=r, children=[]) for r in rows}
    roots: list[SpanNode] = []
    for r in rows:
        node = nodes[r.id]
        parent = nodes.get(r.parent_span_id) if r.parent_span_id else None
        if parent is None:
            # 父 span 不在本 run 里（跨 run 的情况）或本身就是根。
            # 挂到根上而不是丢掉 —— 丢掉会让树缺一整枝，看起来像没执行。
            roots.append(node)
        else:
            parent.children.append(node)
    return roots


def tree_to_dict(nodes: list[SpanNode]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n in nodes:
        s = n.span
        out.append(
            {
                "span_id": s.id,
                "parent_span_id": s.parent_span_id,
                "depth": s.depth,
                "kind": s.kind,
                "name": s.name,
                "agent_name": s.agent_name,
                "status": s.status,
                "started_at": s.started_at,
                "duration_ms": s.duration_ms,
                "model_id": s.model_id,
                "total_tokens": s.total_tokens,
                "cost_usd": s.cost_usd,
                # 单价一起给，好让前端能区分"零成本"和"没配价"
                "has_price": s.price_in_per_1m is not None,
                "input_preview": s.input_preview,
                "input_truncated": s.input_truncated,
                "input_bytes": s.input_bytes,
                "output_preview": s.output_preview,
                "output_truncated": s.output_truncated,
                "output_bytes": s.output_bytes,
                "error": s.error,
                "children": tree_to_dict(n.children),
            }
        )
    return out


async def cleanup(db: AsyncSession, *, retain_days: int = DEFAULT_RETAIN_DAYS) -> dict[str, int]:
    """
    删掉过期的 span 和 run。

    先删 span 再删 run —— 反了的话中间失败会留下没有 run 的孤儿 span，
    查询时它们永远不会出现，但一直占空间。
    """
    cutoff = now_ms() - retain_days * 86_400_000
    span_n = (
        await db.execute(delete(Span).where(Span.created_at < cutoff))
    ).rowcount or 0
    run_n = (
        await db.execute(delete(Run).where(Run.created_at < cutoff))
    ).rowcount or 0
    await db.commit()
    if span_n or run_n:
        log.info("trace_cleanup", spans=span_n, runs=run_n, retain_days=retain_days)
    return {"spans": span_n, "runs": run_n}


async def stats(db: AsyncSession) -> dict[str, Any]:
    span_count = (await db.execute(select(func.count()).select_from(Span))).scalar() or 0
    run_count = (await db.execute(select(func.count()).select_from(Run))).scalar() or 0
    total_cost = (await db.execute(select(func.sum(Run.cost_usd)))).scalar() or 0.0
    total_tokens = (await db.execute(select(func.sum(Run.total_tokens)))).scalar() or 0
    oldest = (await db.execute(select(func.min(Run.started_at)))).scalar()
    return {
        "runs": run_count,
        "spans": span_count,
        "total_tokens": int(total_tokens),
        "total_cost_usd": round(float(total_cost), 6),
        "oldest_run_at": oldest,
        "retain_days": DEFAULT_RETAIN_DAYS,
    }
