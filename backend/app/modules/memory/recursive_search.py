"""
递归搜索：OpenViking 风格的层级检索。

## 核心思路

1. 从初始向量搜索结果（起始点）开始
2. 使用优先队列，按分数优先深入搜索
3. 查找相关记忆（通过标签、引用的实体等）
4. 分数传播：父节点的高分会"传播"给子节点
5. 去重：每个 URI 只保留最高分

## 与 OpenViking 的区别

OpenViking 有 L0（目录）→ L1（章节）→ L2（文件）的层级结构。
Jeeves 的记忆是扁平的，但可以通过以下方式找到"相关"记忆：

- 共享相同标签
- 引用相同实体
- 时间上接近（同一天的记忆）
- 内容相似但不在初始结果中

这些"相关性"类似于 OpenViking 的目录层级关系。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.memory.layout import MemoryScope
from app.modules.memory.models import MemoryItem
from app.modules.memory.models_db import MemoryIndex
from app.modules.memory.vectorize import SearchHit

log = structlog.get_logger(__name__)


def _create_search_hit(
    uri: str,
    score: float,
    title: str = "",
    memory_type: str = "",
    scope: str = "",
) -> SearchHit:
    """
    创建 SearchHit 的辅助函数。

    自动从 URI 提取 memory_type 和 scope（如果未提供）。
    """
    if not memory_type:
        memory_type = _extract_memory_type_from_uri(uri)

    if not scope:
        scope = _extract_scope_from_uri(uri)

    return SearchHit(
        uri=uri,
        score=score,
        title=title,
        memory_type=memory_type,
        scope=scope,
    )


def _extract_memory_type_from_uri(uri: str) -> str:
    """
    从 URI 中提取记忆类型。

    URI 格式：memories/{type}/{id}
    例如：memories/preferences/pref_123 -> preferences
    """
    parts = uri.split("/")
    if len(parts) >= 2 and parts[0] == "memories":
        return parts[1]
    return "unknown"


def _extract_scope_from_uri(uri: str) -> str:
    """
    从 URI 中提取 scope。

    简化版：根据 URI 结构推断 scope。
    实际应该从数据库读取，但这里为了性能简化处理。
    """
    # TODO: 这是简化实现，生产环境应该查数据库
    if "session" in uri:
        return "session"
    elif "agent" in uri:
        return "agent"
    else:
        return "global"


@dataclass
class RecursiveSearchConfig:
    """递归搜索配置。"""

    max_depth: int = 3
    """最大递归深度（防止无限递归）"""

    score_propagation_alpha: float = 0.7
    """
    分数传播系数。

    子节点最终分数 = alpha * 子节点原始分数 + (1 - alpha) * 父节点分数

    - alpha = 1.0: 完全不传播，子节点分数独立
    - alpha = 0.7: 子节点主要靠自己分数，但父节点贡献 30%
    - alpha = 0.5: 父子平均
    - alpha = 0.0: 完全继承父节点分数

    OpenViking 默认 0.7，我们沿用。
    """

    expansion_per_node: int = 5
    """每个节点最多扩展多少个相关记忆"""

    min_propagated_score: float = 0.3
    """传播后的最低分数阈值（低于此分数不再继续递归）"""


async def recursive_search(
    db: AsyncSession,
    scope: MemoryScope,
    starting_points: list[SearchHit],
    config: RecursiveSearchConfig | None = None,
) -> list[SearchHit]:
    """
    从起始点递归搜索相关记忆。

    Args:
        db: 数据库会话
        scope: 搜索范围
        starting_points: 初始高分记忆（来自向量搜索）
        config: 递归搜索配置

    Returns:
        按最终分数排序的记忆列表（包含起始点和递归发现的相关记忆）
    """
    if not starting_points:
        return []

    config = config or RecursiveSearchConfig()

    # 状态跟踪
    visited: set[str] = set()  # 已访问的 URI
    results: dict[str, SearchHit] = {}  # URI -> SearchHit（保留最高分）

    # 优先队列：(负分数, depth, hit)
    # heapq 是最小堆，我们用负分数实现最大堆
    queue: list[tuple[float, int, SearchHit]] = []

    # 初始化：所有起始点加入队列
    for hit in starting_points:
        heapq.heappush(queue, (-hit.score, 0, hit))
        results[hit.uri] = hit

    log.debug(
        "recursive_search_start",
        starting_points=len(starting_points),
        max_depth=config.max_depth,
    )

    # BFS 递归搜索
    expanded_count = 0

    while queue:
        neg_score, depth, current = heapq.heappop(queue)
        current_score = -neg_score

        # 跳过已访问的节点
        if current.uri in visited:
            continue
        visited.add(current.uri)

        # 达到最大深度，不再递归
        if depth >= config.max_depth:
            continue

        # 分数太低，不值得继续递归
        if current_score < config.min_propagated_score:
            continue

        # 查找相关记忆
        try:
            related_hits = await _find_related_memories(
                db, scope, current, config.expansion_per_node
            )
            expanded_count += 1
        except Exception as e:
            log.warning(
                "recursive_search_expansion_failed",
                uri=current.uri,
                error=str(e),
            )
            continue

        # 处理相关记忆
        for related in related_hits:
            if related.uri in visited:
                continue

            # 分数传播
            propagated_score = (
                config.score_propagation_alpha * related.score
                + (1 - config.score_propagation_alpha) * current_score
            )

            # 更新或插入结果
            existing = results.get(related.uri)
            if existing is None or propagated_score > existing.score:
                # 创建新的 SearchHit 带传播后的分数
                propagated_hit = _create_search_hit(
                    uri=related.uri,
                    score=propagated_score,
                    title=related.title,
                    memory_type=related.memory_type,
                    scope=related.scope,
                )
                results[related.uri] = propagated_hit

                # Phase 5: 只在 L0/L1 层继续递归，L2 是终点
                # 对齐 OpenViking hierarchical_retriever.py:527-529
                # "Only recurse into directories (L0/L1). L2 files are terminal hits."
                is_directory_level = related.uri.endswith((".overview.md", ".abstract.md"))

                if is_directory_level:
                    # 加入队列继续递归
                    heapq.heappush(queue, (-propagated_score, depth + 1, propagated_hit))
                else:
                    # L2 文件是终点，不再递归
                    log.debug(
                        "recursive_search_l2_terminal",
                        uri=related.uri,
                        score=f"{propagated_score:.3f}",
                    )

    # 按最终分数排序
    final_results = sorted(results.values(), key=lambda h: -h.score)

    log.info(
        "recursive_search_completed",
        starting_points=len(starting_points),
        expanded_nodes=expanded_count,
        final_results=len(final_results),
        max_depth_reached=any(
            depth >= config.max_depth for _, depth, _ in queue
        ),
    )

    return final_results


async def _find_related_memories(
    db: AsyncSession,
    scope: MemoryScope,
    current: SearchHit,
    limit: int,
) -> list[SearchHit]:
    """
    查找与当前记忆相关的其他记忆。

    相关性判断（按优先级）：
    1. 共享标签（最强相关）
    2. 引用相同实体
    3. 时间接近（同一天或前后几天）
    4. 内容相似（通过标题关键词）

    Args:
        db: 数据库会话
        scope: 搜索范围
        current: 当前记忆
        limit: 最多返回多少个相关记忆

    Returns:
        相关记忆列表（按相关度排序）
    """
    # 读取当前记忆的完整信息
    current_item = await _read_memory_item(db, current.uri)
    if current_item is None:
        return []

    # 构建候选池
    candidates: list[tuple[float, SearchHit]] = []  # (相关度分数, hit)

    # 1. 查找共享标签的记忆
    if current_item.tags:
        tag_related = await _find_by_tags(
            db, scope, current.uri, current_item.tags, limit * 2
        )
        for hit in tag_related:
            # 标签相关性：按共享标签数量打分
            shared_tags = _count_shared_tags(current_item.tags, hit.title)
            tag_score = min(1.0, shared_tags / max(1, len(current_item.tags)))
            candidates.append((tag_score, hit))

    # 2. 查找引用相同实体的记忆（从内容中提取实体引用）
    entities = _extract_entity_references(current_item.body)
    if entities:
        entity_related = await _find_by_entities(
            db, scope, current.uri, entities, limit * 2
        )
        for hit in entity_related:
            # 实体相关性：按共享实体数量打分
            shared_entities = _count_shared_entities(entities, hit.body)
            entity_score = min(0.8, shared_entities / max(1, len(entities)))
            candidates.append((entity_score, hit))

    # 3. 查找时间接近的记忆
    if current_item.updated_at:
        time_related = await _find_by_time_proximity(
            db, scope, current.uri, current_item.updated_at, limit
        )
        for hit, days_diff in time_related:
            # 时间相关性：7天内线性衰减
            time_score = max(0, 0.6 * (1 - days_diff / 7))
            candidates.append((time_score, hit))

    # 去重并按相关度排序
    seen = {current.uri}  # 排除当前记忆本身
    unique_candidates: dict[str, tuple[float, SearchHit]] = {}

    for score, hit in candidates:
        if hit.uri in seen:
            continue
        seen.add(hit.uri)

        # 保留最高相关度
        if hit.uri not in unique_candidates or score > unique_candidates[hit.uri][0]:
            unique_candidates[hit.uri] = (score, hit)

    # 按相关度排序并返回 top-k
    sorted_candidates = sorted(
        unique_candidates.values(),
        key=lambda x: -x[0]
    )

    return [hit for _, hit in sorted_candidates[:limit]]


async def _read_memory_item(db: AsyncSession, uri: str) -> MemoryItem | None:
    """读取记忆项的完整信息。"""
    from app.modules.memory import service

    try:
        return await service.read_uri(uri)
    except Exception as e:
        log.warning("read_memory_item_failed", uri=uri, error=str(e))
        return None


async def _find_by_tags(
    db: AsyncSession,
    scope: MemoryScope,
    exclude_uri: str,
    tags: list[str],
    limit: int,
) -> list[SearchHit]:
    """查找共享标签的记忆。"""
    if not tags:
        return []

    # 构建 scope 过滤条件
    scope_filters = _build_scope_filters(scope)

    # 查询标签重叠的记忆（简化：在 tags 列表中包含任意一个标签）
    # 生产环境应该用 JSON 查询或单独的标签表
    stmt = (
        select(MemoryIndex)
        .where(
            and_(
                MemoryIndex.uri != exclude_uri,
                *scope_filters,
            )
        )
        .limit(limit)
    )

    result = await db.execute(stmt)
    rows = result.scalars().all()

    hits = []
    for row in rows:
        # 读取完整内容以检查标签（因为 MemoryIndex 不存储 tags）
        item = await _read_memory_item(db, row.uri)
        if item and item.tags:
            shared = set(tags) & set(item.tags)
            if shared:
                hits.append(
                    _create_search_hit(
                        uri=row.uri,
                        score=len(shared) / len(tags),  # 临时分数
                        title=row.title or "",
                        memory_type=row.memory_type,
                        scope=row.scope,
                    )
                )

    return hits


async def _find_by_entities(
    db: AsyncSession,
    scope: MemoryScope,
    exclude_uri: str,
    entities: set[str],
    limit: int,
) -> list[SearchHit]:
    """查找引用相同实体的记忆。"""
    if not entities:
        return []

    scope_filters = _build_scope_filters(scope)

    # 查询包含这些实体的记忆（通过全文检索）
    # 简化：直接用 LIKE（生产环境应该用 FTS 或向量搜索）
    stmt = (
        select(MemoryIndex)
        .where(
            and_(
                MemoryIndex.uri != exclude_uri,
                *scope_filters,
            )
        )
        .limit(limit * 2)  # 多取一些，因为要过滤
    )

    result = await db.execute(stmt)
    rows = result.scalars().all()

    hits = []
    for row in rows:
        item = await _read_memory_item(db, row.uri)
        if item:
            # 检查内容中是否包含实体
            content_lower = (item.title + " " + item.body).lower()
            found_entities = [e for e in entities if e in content_lower]

            if found_entities:
                hits.append(
                    _create_search_hit(
                        uri=row.uri,
                        score=len(found_entities) / len(entities),
                        title=item.title,
                        memory_type=row.memory_type,
                        scope=row.scope,
                    )
                )

    return hits[:limit]


async def _find_by_time_proximity(
    db: AsyncSession,
    scope: MemoryScope,
    exclude_uri: str,
    reference_time: datetime,
    limit: int,
) -> list[tuple[SearchHit, float]]:
    """
    查找时间接近的记忆。

    Returns:
        (SearchHit, days_difference) 列表
    """
    scope_filters = _build_scope_filters(scope)

    # 查询前后 7 天内的记忆
    time_window = timedelta(days=7)
    start_time = reference_time - time_window
    end_time = reference_time + time_window

    stmt = (
        select(MemoryIndex)
        .where(
            and_(
                MemoryIndex.uri != exclude_uri,
                MemoryIndex.updated_at.between(start_time, end_time),
                *scope_filters,
            )
        )
        .limit(limit)
    )

    result = await db.execute(stmt)
    rows = result.scalars().all()

    hits = []
    for row in rows:
        if row.updated_at:
            days_diff = abs((row.updated_at - reference_time).total_seconds() / 86400)
            hits.append((
                _create_search_hit(
                    uri=row.uri,
                    score=0.0,  # 临时分数，外层会重新计算
                    title=row.title or "",
                    memory_type=row.memory_type,
                    scope=row.scope,
                ),
                days_diff,
            ))

    return hits


def _build_scope_filters(scope: MemoryScope) -> list:
    """
    构建 scope 过滤条件（用于 SQLAlchemy WHERE）。

    visible_scopes 返回 (scope_kind, agent_id, session_id, peer_agent_id)
    四元组列表，其中 session 记忆的 agent_id 恒为空串（会话记忆不按智能体
    隔离）。索引里存的是空串而非 NULL，所以这里用 == "" 而不是 is_(None)。
    """
    from sqlalchemy import or_

    from app.modules.memory.vectorize import visible_scopes

    scopes = visible_scopes(scope)

    filters = [
        and_(
            MemoryIndex.scope == kind,
            MemoryIndex.agent_id == agent_id,
            MemoryIndex.session_id == session_id,
            MemoryIndex.peer_agent_id == peer_id,
        )
        for kind, agent_id, session_id, peer_id in scopes
    ]

    # 用 OR 连接所有 scope
    return [or_(*filters)] if filters else []


def _count_shared_tags(tags1: list[str], text: str) -> int:
    """统计 text 中包含多少个 tags1 中的标签（简化版）。"""
    text_lower = text.lower()
    return sum(1 for tag in tags1 if tag.lower() in text_lower)


def _extract_entity_references(text: str) -> set[str]:
    """
    从文本中提取实体引用（简化版）。

    生产环境应该用 NER 或从 entities 记忆类型中查找。
    这里简化为：提取首字母大写的连续词组（可能是人名、地名等）。
    """
    import re

    # 简单正则：连续的首字母大写单词（如 "John Smith", "OpenAI"）
    pattern = r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b'
    matches = re.findall(pattern, text)

    # 去重并过滤太短的（单字母的可能是缩写）
    entities = {m for m in matches if len(m) > 1}

    return entities


def _count_shared_entities(entities: set[str], text: str) -> int:
    """统计 text 中包含多少个 entities。"""
    text_lower = text.lower()
    return sum(1 for entity in entities if entity.lower() in text_lower)


