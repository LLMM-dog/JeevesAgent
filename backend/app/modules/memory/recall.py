"""
记忆召回：在对话轮次中注入相关记忆。

## 召回流程（改进版，参考 OpenViking）

1. 向量搜索（分类型并行）- 获取初始高分记忆
2. 递归搜索 - 从初始结果出发，查找相关记忆（标签、实体、时间）
3. 按分数排序 + 热度加权
4. 预算截断
5. 渲染成文本

## 与 OpenViking 的对齐

✅ 已实现：
- 递归搜索（hierarchical_retriever.py 的核心算法）
- 分数传播（score propagation）
- 热度加权

🚧 待实现：
- Rerank 重排序
- 稀疏向量 + 密集向量混合搜索
- 查询扩展
- LLM 摘要
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.memory import service, vectorize
from app.modules.memory.models import MemoryItem, MemoryScope
from app.modules.memory.vectorize import SearchHit

log = structlog.get_logger(__name__)


@dataclass
class RecallResult:
    """召回结果。"""

    memories: list[tuple[str, MemoryItem]] = field(default_factory=list)
    rendered: str = ""
    total_chars: int = 0
    stats: dict[str, Any] = field(default_factory=dict)


async def recall_memories(
    db: AsyncSession,
    *,
    session_id: str,
    query: str,
    agent_id: str,
    max_chars: int | None = None,
    embedding_model: Any = None,
) -> RecallResult:
    """
    召回相关记忆。

    在对话轮次开始前调用，将相关记忆注入到系统提示词中。

    支持自动降级：
    - Level 4 (Full): LLM + Embedding + Rerank，完整功能
    - Level 3 (Standard): LLM + Embedding，向量搜索
    - Level 2 (Basic): 仅 LLM，关键词搜索
    - Level 1 (None): 无配置，返回空

    Args:
        db: 数据库会话
        session_id: 会话 ID
        query: 用户查询（用于语义搜索）
        agent_id: 智能体 ID
        max_chars: 召回内容的字符数上限（默认读配置）
        embedding_model: 嵌入模型（可选，用于向量搜索）

    Returns:
        RecallResult 包含召回的记忆和渲染后的文本
    """
    from app.modules.memory.capability import MemoryCapabilityLevel, detect_capability

    # ── 0. 检测当前能力并决定降级策略 ──
    capability = await detect_capability(db)

    # Level 1: 无配置，直接返回空
    if capability.level == MemoryCapabilityLevel.NONE:
        log.info(
            "recall_degraded_to_none",
            reason=capability.degradation_reason,
            session_id=session_id,
            agent_id=agent_id,
        )
        return RecallResult(
            memories=[],
            rendered="",
            stats={"total": 0, "degraded": True, "level": "none", "reason": capability.degradation_reason},
        )

    max_chars = max_chars or settings.memory.recall_max_chars
    cfg = settings.memory

    # 构建搜索 scope（session 级，可以看到 global + agent + session）
    scope = MemoryScope(agent_id=agent_id, session_id=session_id)

    # ── 1. 并行搜索所有记忆类型（根据能力选择搜索策略） ──
    searches = [
        ("events", cfg.recall_limit_events),
        ("entities", cfg.recall_limit_entities),
        ("preferences", cfg.recall_limit_preferences),
        ("experiences", cfg.recall_limit_experiences),
    ]

    # 选择搜索策略：向量搜索（Level 3/4）或关键词搜索（Level 2）
    if capability.can_vector_search:
        # Level 3/4: 使用向量搜索
        async def search_one_type(memory_type: str, limit: int) -> list[SearchHit]:
            """为一个记忆类型创建独立 session 并搜索。"""
            try:
                # 从当前 session 获取 async engine
                engine = db.bind
                from sqlalchemy.ext.asyncio import AsyncSession as AsyncSessionClass
                from sqlalchemy.orm import sessionmaker

                # 创建新的 session（共享同一个 connection pool）
                async_session_factory = sessionmaker(  # type: ignore[call-overload]
                    engine, class_=AsyncSessionClass, expire_on_commit=False
                )

                async with async_session_factory() as temp_db:
                    # Phase 5: 只搜索 L0/L1 层（目录层），对齐 OpenViking
                    hits = await vectorize.search(
                        db=temp_db,
                        scope=scope,
                        query=query,
                        model=embedding_model,
                        limit=limit,
                        memory_type=memory_type,
                        min_score=cfg.search_min_score,
                        level=[0, 1],  # 只搜索目录层
                    )
                    return hits
            except Exception as e:
                log.warning("recall_search_type_failed", memory_type=memory_type, error=str(e))
                return []
    else:
        # Level 2: 降级到关键词搜索
        log.info(
            "recall_degraded_to_keyword",
            reason=capability.degradation_reason,
            session_id=session_id,
            agent_id=agent_id,
        )

        from app.modules.memory.keyword_search import keyword_search

        async def search_one_type(memory_type: str, limit: int) -> list[SearchHit]:
            """关键词搜索（降级方案）"""
            try:
                return await keyword_search(
                    db=db,
                    scope=scope,
                    query=query,
                    limit=limit,
                    memory_type=memory_type,
                )
            except Exception as e:
                log.warning("recall_keyword_search_failed", memory_type=memory_type, error=str(e))
                return []

    # 并行执行所有搜索
    tasks = [search_one_type(memory_type, limit) for memory_type, limit in searches]
    results = await asyncio.gather(*tasks, return_exceptions=False)

    # ── 2. 合并初始搜索结果 ──
    initial_hits: list[SearchHit] = []
    stats_by_type = {}

    for (memory_type, _limit), hits in zip(searches, results, strict=False):
        stats_by_type[memory_type] = len(hits)
        initial_hits.extend(hits)

    if not initial_hits:
        log.debug("recall_no_results", query=query[:100])
        return RecallResult(stats={"total": 0, "by_type": stats_by_type})

    # ── 2.5. 递归搜索（OpenViking 核心功能，仅 Level 3/4）──
    if capability.can_recursive and cfg.recall_enable_recursive_search:
        from app.modules.memory.recursive_search import (
            RecursiveSearchConfig,
            recursive_search,
        )

        recursive_config = RecursiveSearchConfig(
            max_depth=cfg.recall_recursive_max_depth,
            score_propagation_alpha=cfg.recall_recursive_alpha,
            expansion_per_node=cfg.recall_recursive_expansion,
            min_propagated_score=cfg.recall_recursive_min_score,
        )

        try:
            # 递归搜索会返回包含初始结果 + 递归发现的所有记忆
            expanded_hits = await recursive_search(
                db=db,
                scope=scope,
                starting_points=initial_hits,
                config=recursive_config,
            )

            log.info(
                "recall_recursive_search_completed",
                initial_count=len(initial_hits),
                expanded_count=len(expanded_hits),
            )

            # 用递归搜索的结果替换初始结果
            all_hits_flat = expanded_hits
        except Exception as e:
            log.warning(
                "recall_recursive_search_failed",
                error=str(e),
                fallback_to_initial=True,
            )
            # 失败时回退到初始结果
            all_hits_flat = initial_hits
    else:
        # 未启用或不支持递归搜索
        if not capability.can_recursive:
            log.debug("recall_recursive_skipped", reason="Level 2 不支持递归搜索")
        all_hits_flat = initial_hits

    # ── 2.6. Rerank 重排序（OpenViking 精筛，仅 Level 4）──
    if capability.can_rerank:
        try:
            all_hits_flat = await _apply_rerank(
                db=db,
                query=query,
                hits=all_hits_flat,
                config=cfg,
            )
            log.info(
                "recall_rerank_completed",
                candidates=len(all_hits_flat),
            )
        except Exception as e:
            log.warning(
                "recall_rerank_failed",
                error=str(e),
                fallback_to_vector_scores=True,
            )
            # 失败时保持原有分数
    else:
        if not capability.can_rerank:
            log.debug("recall_rerank_skipped", reason="Rerank 未配置或不支持")

    # ── 3. 重新分类（递归搜索打乱了类型信息，需要重新识别）──
    all_hits: list[tuple[str, SearchHit]] = []

    for hit in all_hits_flat:
        # 从 URI 中提取记忆类型（格式：memories/{type}/{id}）
        memory_type = _extract_memory_type_from_uri(hit.uri)
        all_hits.append((memory_type, hit))

    # ── 3.5. 应用热度加权（OpenViking memory_lifecycle，仅 Level 3/4）──
    if capability.can_hotness and cfg.hotness_weight > 0:
        all_hits = await _apply_hotness_boost(
            db=db,
            hits=all_hits,
            alpha=cfg.hotness_weight,
            half_life_days=cfg.hotness_half_life_days,
        )
        log.info(
            "recall_hotness_applied",
            alpha=cfg.hotness_weight,
            half_life_days=cfg.hotness_half_life_days,
        )
    else:
        if not capability.can_hotness:
            log.debug("recall_hotness_skipped", reason="Level 2 不支持热度评分")

    # ── 4. 按分数排序（递归搜索已经排序，但为了保险再排一次）──
    all_hits.sort(key=lambda x: -x[1].score)

    # ── 4.5. 加载 L2 详细内容（Phase 5：分层召回）──
    # 对齐 OpenViking：先用 L0/L1 快速筛选，然后加载 L2
    all_hits = await _load_l2_details(db, all_hits)

    # 重新排序（L2 可能有多个文件对应一个 L0/L1）
    all_hits.sort(key=lambda x: -x[1].score)

    # ── 5. 读取完整内容并应用预算截断 ──
    selected: list[tuple[str, MemoryItem]] = []
    total_chars = 0

    for memory_type, hit in all_hits:
        # 读取完整记忆内容
        item = await service.read_uri(hit.uri)
        if item is None:
            log.warning("recall_item_not_found", uri=hit.uri)
            continue

        # 粗略估算字符数（标题 + 正文）
        item_chars = len(item.title) + len(item.body)

        if total_chars + item_chars <= max_chars:
            selected.append((memory_type, item))
            total_chars += item_chars
        else:
            # 预算用完，停止
            break

    # ── 5. 渲染 ──
    rendered = _render_memories(selected)

    # ── 6. 更新访问计数（异步，不阻塞返回，仅 Level 3/4）──
    if selected and capability.can_hotness:
        asyncio.create_task(_increment_active_counts(db, [item.uri for _, item in selected]))

    log.info(
        "recall_completed",
        session_id=session_id,
        agent_id=agent_id,
        level=capability.level.name,
        total=len(selected),
        chars=total_chars,
        by_type=stats_by_type,
        degraded=capability.level < MemoryCapabilityLevel.FULL,
    )

    return RecallResult(
        memories=selected,
        rendered=rendered,
        total_chars=total_chars,
        stats={
            "total": len(selected),
            "candidates": len(all_hits),
            "by_type": stats_by_type,
            "chars": total_chars,
            "budget": max_chars,
            "level": capability.level.name.lower(),
            "degraded": capability.level < MemoryCapabilityLevel.FULL,
            "degradation_reason": capability.degradation_reason if capability.level < MemoryCapabilityLevel.FULL else "",
        },
    )


def _render_memories(memories: list[tuple[str, MemoryItem]]) -> str:
    """
    渲染记忆为文本，注入到系统提示词。

    格式：
    ## Events

    ### 标题1
    内容1

    ### 标题2
    内容2

    ## Preferences

    ### 标题3
    内容3
    """
    if not memories:
        return ""

    # 按类型分组
    by_type = defaultdict(list)
    for memory_type, item in memories:
        by_type[memory_type].append(item)

    sections = []

    # 按固定顺序渲染（偏好优先，因为最重要）
    type_order = ["preferences", "experiences", "entities", "events"]

    for memory_type in type_order:
        if memory_type not in by_type:
            continue

        items = by_type[memory_type]
        section_lines = [f"## {memory_type.title()}\n"]

        for item in items:
            section_lines.append(f"### {item.title}")
            section_lines.append(item.body)
            section_lines.append("")  # 空行分隔

        sections.append("\n".join(section_lines))

    return "\n".join(sections)


def _extract_memory_type_from_uri(uri: str) -> str:
    """
    从 URI 中提取记忆类型。

    URI 格式：memories/{type}/{id}
    例如：memories/preferences/pref_123 -> preferences

    如果解析失败，返回 "unknown"。
    """
    parts = uri.split("/")
    if len(parts) >= 2 and parts[0] == "memories":
        return parts[1]
    return "unknown"


async def _apply_rerank(
    db: AsyncSession,
    query: str,
    hits: list[SearchHit],
    config: Any,
) -> list[SearchHit]:
    """
    使用 rerank 模型对候选记忆重新打分。

    流程：
    1. 从数据库解析 rerank 模型配置
    2. 提取所有记忆的标题作为文档
    3. 调用 rerank API 获取新分数
    4. 混合向量分数和 rerank 分数
    5. 返回更新后的 SearchHit 列表

    Args:
        db: 数据库会话
        query: 用户查询
        hits: 候选记忆列表（已有向量分数）
        config: 配置对象（用于权重参数）

    Returns:
        更新了分数的 SearchHit 列表
    """
    from app.infra.rerank import create_rerank_provider
    from app.modules.memory import service as memory_service

    if not hits:
        return hits

    # 从数据库解析 rerank 模型配置
    model = await memory_service.resolve_rerank_model(db)
    if model is None:
        log.debug("rerank_model_not_configured")
        return hits

    # 创建 rerank 提供商
    provider = create_rerank_provider(
        provider="auto",  # 根据 base_url 自动识别
        api_key=model.api_key,
        model=model.model_id,
        api_base=model.base_url,
    )

    if provider is None:
        log.warning(
            "rerank_provider_creation_failed",
            provider=config.rerank_provider,
        )
        return hits

    try:
        # 准备文档列表（标题 + 摘要）
        # 需要读取完整内容以获取摘要
        documents = []
        for hit in hits:
            # 简化：只用标题作为文档
            # 如果需要更完整的上下文，这里应该读取 body
            doc = hit.title
            documents.append(doc)

        # 调用 rerank API
        rerank_scores = await provider.rerank(
            query=query,
            documents=documents,
            top_k=None,  # 返回所有结果
        )

        # 混合向量分数和 rerank 分数
        vector_weight = config.rerank_vector_weight
        rerank_weight = config.rerank_rerank_weight

        updated_hits = []
        for hit, rerank_score in zip(hits, rerank_scores, strict=False):
            # 保存原始向量分数
            original_score = hit.score

            # 混合分数
            mixed_score = (
                vector_weight * original_score + rerank_weight * rerank_score
            )

            # 创建新的 SearchHit（dataclass 是不可变的）
            from dataclasses import replace

            updated_hit = replace(hit, score=mixed_score)
            updated_hits.append(updated_hit)

            log.debug(
                "rerank_score_updated",
                uri=hit.uri,
                vector_score=f"{original_score:.3f}",
                rerank_score=f"{rerank_score:.3f}",
                mixed_score=f"{mixed_score:.3f}",
            )

        # 按新分数排序
        updated_hits.sort(key=lambda h: -h.score)

        return updated_hits

    finally:
        # 清理资源
        await provider.close()


async def _apply_hotness_boost(
    db: AsyncSession,
    hits: list[tuple[str, SearchHit]],
    alpha: float,
    half_life_days: float,
) -> list[tuple[str, SearchHit]]:
    """
    应用热度加权到搜索结果。

    热度 = 频率分量 * 时间衰减分量
    最终分数 = (1 - alpha) * semantic_score + alpha * hotness

    对齐 OpenViking 的实现：
    - openviking/retrieve/memory_lifecycle.py:19-64
    - openviking/retrieve/hierarchical_retriever.py:567-622

    Args:
        db: 数据库 session
        hits: 搜索结果列表 [(memory_type, SearchHit)]
        alpha: 热度权重（0.0 - 1.0）
        half_life_days: 时间衰减半衰期（天）

    Returns:
        更新了分数的搜索结果列表
    """
    from datetime import datetime

    from sqlalchemy import select

    from app.infra.hotness import blend_with_hotness, hotness_score
    from app.modules.memory.models_db import MemoryIndex

    if not hits or alpha <= 0:
        return hits

    # 收集所有 URI
    uris = [hit.uri for _, hit in hits]

    # 批量查询 active_count 和 file_updated_at
    stmt = select(
        MemoryIndex.uri,
        MemoryIndex.active_count,
        MemoryIndex.file_updated_at,
    ).where(MemoryIndex.uri.in_(uris))

    result = await db.execute(stmt)
    rows = result.all()

    # 构建 URI -> (active_count, updated_at) 映射
    hotness_data: dict[str, tuple[int, datetime | None]] = {}
    for row in rows:
        uri = row.uri
        active_count = row.active_count
        # file_updated_at 是 Unix 时间戳（秒）
        updated_at = (
            datetime.fromtimestamp(row.file_updated_at, tz=UTC)
            if row.file_updated_at > 0
            else None
        )
        hotness_data[uri] = (active_count, updated_at)

    # 计算热度并混合分数
    now = datetime.now(UTC)
    updated_hits: list[tuple[str, SearchHit]] = []

    for memory_type, hit in hits:
        if hit.uri not in hotness_data:
            # 找不到元数据，保持原分数
            updated_hits.append((memory_type, hit))
            continue

        active_count, updated_at = hotness_data[hit.uri]

        # 计算热度
        hotness = hotness_score(
            active_count=active_count,
            updated_at=updated_at,
            now=now,
            half_life_days=half_life_days,
        )

        # 混合语义分数和热度
        original_score = hit.score
        blended_score = blend_with_hotness(original_score, hotness, alpha)

        # 创建新的 SearchHit
        from dataclasses import replace

        updated_hit = replace(hit, score=blended_score)

        log.debug(
            "hotness_boost_applied",
            uri=hit.uri,
            semantic_score=f"{original_score:.3f}",
            hotness=f"{hotness:.3f}",
            active_count=active_count,
            blended_score=f"{blended_score:.3f}",
        )

        updated_hits.append((memory_type, updated_hit))

    return updated_hits


async def _increment_active_counts(db: AsyncSession, uris: list[str]) -> None:
    """
    增加记忆的访问计数（active_count）。

    在召回完成后异步调用，不阻塞用户请求。
    每次召回命中，active_count +1，用于热度计算。

    Args:
        db: 数据库 session
        uris: 被召回的记忆 URI 列表
    """
    from sqlalchemy import update

    from app.modules.memory.models_db import MemoryIndex

    if not uris:
        return

    try:
        # 批量更新 active_count
        stmt = (
            update(MemoryIndex)
            .where(MemoryIndex.uri.in_(uris))
            .values(active_count=MemoryIndex.active_count + 1)
        )

        await db.execute(stmt)
        await db.commit()

        log.debug(
            "active_counts_incremented",
            count=len(uris),
        )
    except Exception as e:
        log.warning(
            "active_counts_increment_failed",
            error=str(e),
        )
        # 失败不影响召回结果，只记录日志


async def _load_l2_details(
    db: AsyncSession,
    l0_l1_hits: list[tuple[str, SearchHit]],
) -> list[tuple[str, SearchHit]]:
    """
    从 L0/L1 命中结果，加载对应的 L2 详细内容。

    对齐 OpenViking hierarchical_retriever.py:527-529：
    "Only recurse into directories (L0/L1). L2 files are terminal hits."

    映射规则：
    - L0 (.abstract.md): 查找同目录下的所有 L2 文件
    - L1 (.overview.md): 查找同目录下的所有 L2 文件
    - L2 (普通 .md): 直接保留

    Args:
        db: 数据库 session
        l0_l1_hits: L0/L1 层的搜索结果

    Returns:
        L2 层的搜索结果（继承 L0/L1 的分数）

    Example:
        L1: memories/preferences/.overview.md (score=0.85)
        → L2: memories/preferences/testing.md (score=0.85)
        → L2: memories/preferences/code_style.md (score=0.85)
    """
    from sqlalchemy import select

    from app.modules.memory.models_db import MemoryIndex
    from app.modules.memory.schema import MemoryScopeKind

    if not l0_l1_hits:
        return []

    l2_hits: list[tuple[str, SearchHit]] = []
    processed_uris: set[str] = set()  # 去重

    for memory_type, hit in l0_l1_hits:
        # 如果已经是 L2，直接保留
        if not hit.uri.endswith((".overview.md", ".abstract.md")):
            if hit.uri not in processed_uris:
                l2_hits.append((memory_type, hit))
                processed_uris.add(hit.uri)
            continue

        # L0/L1 → 查找对应的 L2 文件
        # 获取目录路径（移除 .overview.md 或 .abstract.md）
        if hit.uri.endswith("/.overview.md"):
            dir_uri = hit.uri[:-13]  # 移除 "/.overview.md"
        elif hit.uri.endswith("/.abstract.md"):
            dir_uri = hit.uri[:-13]  # 移除 "/.abstract.md"
        else:
            # 不应该到这里
            continue

        # 查询该目录下的所有 L2 文件
        # 使用 LIKE 匹配：memories/preferences/%.md 但不包括 .overview.md 和 .abstract.md
        stmt = (
            select(MemoryIndex)
            .where(
                MemoryIndex.uri.like(f"{dir_uri}/%"),
                MemoryIndex.level == 2,
            )
        )

        result = await db.execute(stmt)
        l2_rows = result.scalars().all()

        # 为每个 L2 文件创建 SearchHit（继承父目录的分数）
        for row in l2_rows:
            if row.uri in processed_uris:
                continue

            l2_hit = SearchHit(
                uri=row.uri,
                score=hit.score,  # 继承 L0/L1 分数
                title=row.title,
                memory_type=row.memory_type,
                scope=MemoryScopeKind(row.scope),
            )
            l2_hits.append((memory_type, l2_hit))
            processed_uris.add(row.uri)

            log.debug(
                "l0_l1_to_l2_mapped",
                parent_uri=hit.uri,
                parent_level="L0" if hit.uri.endswith(".abstract.md") else "L1",
                child_uri=row.uri,
                inherited_score=f"{hit.score:.3f}",
            )

    log.info(
        "l2_details_loaded",
        l0_l1_count=len(l0_l1_hits),
        l2_count=len(l2_hits),
    )

    return l2_hits
