"""
关键词搜索召回（Embedding 模型未配置时的降级方案）。

搜索策略：
1. 标题精确匹配（高权重）
2. 时间范围过滤（最近 30 天优先）
3. 按 updated_at 排序

性能：约为向量搜索的 60%
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.memory.layout import MemoryScope
from app.modules.memory.models_db import MemoryIndex
from app.modules.memory.schema import MemoryScopeKind
from app.modules.memory.vectorize import SearchHit, visible_scopes

log = structlog.get_logger(__name__)


async def keyword_search(
    db: AsyncSession,
    scope: MemoryScope,
    query: str,
    *,
    limit: int = 10,
    memory_type: str = "",
) -> list[SearchHit]:
    """
    关键词搜索召回（降级方案）。

    搜索策略：
    1. 标题包含查询关键词
    2. 按匹配度和时间衰减混合排序
    3. 优先返回最近更新的记忆

    Args:
        db: 数据库 session
        scope: 搜索范围
        query: 查询字符串
        limit: 返回数量
        memory_type: 过滤记忆类型

    Returns:
        SearchHit 列表（按相关度排序）
    """
    if not query.strip():
        return []

    # 提取关键词（简单分词：按空格分割，过滤短词）
    keywords = [kw.lower() for kw in query.split() if len(kw) >= 2]
    if not keywords:
        return []

    log.debug("keyword_search_started", query=query, keywords=keywords, limit=limit)

    # 构建 scope 过滤条件
    scopes_list = visible_scopes(scope)
    conditions = []
    for scope_kind, agent_id, session_id, peer_agent_id in scopes_list:
        conditions.append(
            and_(
                MemoryIndex.scope == scope_kind,
                MemoryIndex.agent_id == agent_id,
                MemoryIndex.session_id == session_id,
                MemoryIndex.peer_agent_id == peer_agent_id,
            )
        )

    # 构建查询
    stmt = select(MemoryIndex).where(or_(*conditions))

    # 记忆类型过滤
    if memory_type:
        stmt = stmt.where(MemoryIndex.memory_type == memory_type)

    # 关键词过滤（标题包含任一关键词）
    title_conditions = [MemoryIndex.title.ilike(f"%{kw}%") for kw in keywords]
    stmt = stmt.where(or_(*title_conditions))

    # 按更新时间排序（最近的优先）
    stmt = stmt.order_by(MemoryIndex.file_updated_at.desc())
    stmt = stmt.limit(limit * 3)  # 多取一些，后续重新打分

    result = await db.execute(stmt)
    rows = result.scalars().all()

    log.debug("keyword_search_candidates", candidates=len(rows))

    # 简单打分（标题匹配度 + 时间衰减）
    hits: list[SearchHit] = []
    now_ts = datetime.now(UTC).timestamp()

    for row in rows:
        title_lower = row.title.lower()

        # 计算匹配度（包含几个关键词）
        matched_count = sum(1 for kw in keywords if kw in title_lower)
        match_score = matched_count / len(keywords)  # 0.0 ~ 1.0

        # 完全匹配加成
        if all(kw in title_lower for kw in keywords):
            match_score = min(match_score + 0.2, 1.0)

        # 时间衰减（最近 30 天）
        age_days = (now_ts - row.file_updated_at) / 86400
        recency_boost = max(0, 1 - age_days / 30)  # 0 ~ 1

        # 最终分数：70% 匹配度 + 30% 时间衰减
        final_score = 0.7 * match_score + 0.3 * recency_boost

        if final_score > 0:
            hit = SearchHit(
                uri=row.uri,
                score=final_score,
                title=row.title,
                memory_type=row.memory_type,
                scope=MemoryScopeKind(row.scope),
            )
            hits.append(hit)

    # 按分数排序
    hits.sort(key=lambda x: -x.score)

    log.info(
        "keyword_search_completed",
        query=query[:50],
        candidates=len(rows),
        hits=len(hits),
        returned=min(limit, len(hits)),
    )

    return hits[:limit]
