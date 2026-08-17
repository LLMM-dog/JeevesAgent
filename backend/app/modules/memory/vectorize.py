"""
记忆的向量化与语义搜索。

## 与 OpenViking 的关键差异：搜索范围

它的目录是 `viking://user/{{ user_space }}/memories/...`，隔离维度是
【用户】和【peer】（多租户 SaaS）。搜索时按 `user_space` 拼路径。

Jeeves 的隔离是【三层】：global / agent / session（见 layout.py）。
所以搜索范围不能照抄它的拼法，要按我们的 scope 语义：

    session 查询  → 看 global + 该 agent + 该 session
    agent 查询    → 看 global + 该 agent（不看任何 session）
    global 查询   → 只看 global

这个顺序不是随意的：会话级记忆对其他会话【不可见】，否则 A 会话的
临时上下文会污染 B 会话。而 agent 级对所有会话可见 —— 那是"这个智能体
学到的东西"。

## 只增类型也要向量化

events / trajectories 是 add_only，预取时【跳过】它们（既然不会改，
回顾只是白烧 token），但向量化【不能跳过】—— 两件事目的不同：

- 预取：为了让模型改已有记忆而不是新建重复的
- 向量化：为了以后能召回

OpenViking 同样对全部 written + edited 向量化，只排除 .overview.md
和 .abstract.md（memory_updater.py:1352）。我排除 overview 的理由相同：
它是派生的索引文件，召回它没有意义，而且它的内容是其他记忆的标题拼接，
会与那些记忆本身竞争相似度。

## 换模型后的兼容

用户可以随时换嵌入模型。维度一变，旧向量全部失效 ——
但【不自动重算】：那可能是几千次 API 调用，用户没同意就烧钱。
做法是把失效的行标出来，由 `stale_count()` 报告、`revectorize_all()`
手动触发。见 memory 路由的 /revectorize 接口。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ProviderError
from app.infra.llm.embedding import cosine, embed_texts
from app.infra.llm.port import ResolvedModel
from app.modules.memory import index as index_mod
from app.modules.memory import registry, render
from app.modules.memory.models import MemoryItem, MemoryScope
from app.modules.memory.models_db import MemoryIndex
from app.modules.memory.schema import MemoryScopeKind

log = structlog.get_logger(__name__)

# 不向量化的文件。
#
# overview 是派生的索引（其他记忆的标题拼接）。向量化它会让它与
# 被它索引的那些记忆竞争相似度 —— 而命中一个目录索引对召回毫无价值。
SKIP_SUFFIXES = (".overview.md", "/.abstract.md")

# 层级识别（对齐 OpenViking LEVEL_URI_SUFFIX）
# L0 (Abstract): .abstract.md - 一句话摘要
# L1 (Overview): .overview.md - 概览索引
# L2 (Details): 普通 .md 文件 - 完整详情
LEVEL_SUFFIXES = {
    ".abstract.md": 0,
    ".overview.md": 1,
}


def get_level_from_uri(uri: str) -> int:
    """
    从 URI 推断记忆层级。

    对齐 OpenViking hierarchical_retriever.py:58 LEVEL_URI_SUFFIX

    Returns:
        0 - L0 (Abstract)
        1 - L1 (Overview)
        2 - L2 (Details, 默认)
    """
    for suffix, level in LEVEL_SUFFIXES.items():
        if uri.endswith(suffix):
            return level
    return 2  # 默认 L2


def pack(vector: list[float]) -> bytes:
    """float 列表 → float32 紧凑二进制。"""
    return struct.pack(f"<{len(vector)}f", *vector) if vector else b""


def unpack(blob: bytes | None) -> list[float]:
    """
    float32 二进制 → float 列表。

    长度不是 4 的倍数时返回空列表而非报错 —— 那意味着数据损坏，
    而一条记忆的向量坏掉不该让整次召回失败。
    """
    if not blob or len(blob) % 4 != 0:
        return []
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def should_vectorize(uri: str) -> bool:
    return not any(uri.endswith(s) for s in SKIP_SUFFIXES)


@dataclass
class VectorizeReport:
    """一次向量化的结果。"""

    attempted: int = 0
    succeeded: int = 0
    skipped: int = 0
    model: str = ""
    dim: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        parts = [f"向量化 {self.succeeded}/{self.attempted}"]
        if self.skipped:
            parts.append(f"跳过 {self.skipped}")
        if self.model:
            parts.append(f"{self.model}({self.dim}维)")
        if self.errors:
            parts.append(f"失败 {len(self.errors)}")
        return " | ".join(parts)


async def vectorize_uris(
    db: AsyncSession,
    uris: list[str],
    *,
    model: ResolvedModel | None,
    read_item: Any,
) -> VectorizeReport:
    """
    给指定的 uri 算向量并存进索引。

    read_item 是 `async (uri) -> MemoryItem | None`，由调用方注入 ——
    这个模块不该知道文件怎么读（那是 file_store 的事），
    注入让它能被独立测试。

    model 为 None 表示用户没配嵌入模型 → 整体跳过而不是报错。
    向量召回是增强，没有它系统仍然能用关键词搜索。
    """
    report = VectorizeReport()
    targets = [u for u in uris if should_vectorize(u)]
    report.skipped = len(uris) - len(targets)

    if not targets:
        return report
    if model is None:
        report.skipped += len(targets)
        log.info("memory_vectorize_skipped_no_model", count=len(targets))
        return report

    # 读正文并渲染 embedding_template
    items: list[tuple[str, MemoryItem, str, int]] = []
    for uri in targets:
        item = await read_item(uri)
        if item is None:
            report.errors.append(f"{uri}: 文件不存在")
            continue
        schema = registry.get_schemas().get(item.memory_type)
        if schema is None:
            report.errors.append(f"{uri}: 未知记忆类型 {item.memory_type}")
            continue

        # 识别记忆层级（在渲染前，因为截断逻辑需要它）
        level = get_level_from_uri(uri)

        # 渲染向量化文本（L2 层级会自动截断）
        text = render.render_embedding_text(schema, item, level=level)
        if not text.strip():
            report.skipped += 1
            continue
        items.append((uri, item, text, level))

    if not items:
        return report

    report.attempted = len(items)
    try:
        result = await embed_texts(model, [t for _, _, t, _ in items])
    except ProviderError as e:
        # 嵌入失败【不能让提取失败】—— 记忆已经写进文件了，
        # 缺的只是向量。下次 revectorize 能补上。
        report.errors.append(f"嵌入服务失败：{e}")
        log.warning("memory_vectorize_failed", error=str(e), count=len(items))
        return report

    report.model, report.dim = result.model, result.dim

    for (uri, item, _, level), vector in zip(items, result.vectors, strict=True):

        await index_mod.set_embedding(
            db,
            uri,
            vector=vector,
            model=result.model,
            dim=result.dim,
            content_hash=index_mod.content_hash(item),
            level=level,
        )
        report.succeeded += 1

    await db.commit()
    log.info("memory_vectorize_done", summary=report.summary())
    return report


# ── 搜索 ─────────────────────────────────────────


@dataclass
class SearchHit:
    uri: str
    memory_type: str
    title: str
    score: float
    scope: str


def visible_scopes(scope: MemoryScope) -> list[tuple[str, str, str, str]]:
    """
    这个 scope 能看到哪些索引行。
    返回 (scope, agent_id, session_id, peer_agent_id) 四元组列表。

    ## 为什么不能照抄 OpenViking 的路径拼接

    它按 user_space 拼目录（多租户）。我们按三层隔离筛索引行：

        session 查询 → global + 该 agent + 该 session
        agent 查询   → global + 该 agent
        global 查询  → 只 global

    会话级记忆对其他会话不可见 —— 否则 A 会话的临时上下文会污染 B。
    这是"按智能体和会话隔离"这条设计的直接体现。

    ## peer 视角必须参与筛选

    `agents/A/peers/B/` 是「A 眼中的 B」，与 A 自己的记忆是两套东西。
    不按 peer_agent_id 筛的话：

    - 普通查询会把「A 眼中的 B」混进 A 自己的记忆里
    - peer 视角查询会拿到 A 自己的记忆

    两个方向都错。peer 目前不会被创建（见 docs 的说明），但筛选条件
    要先正确 —— 等它被用起来时这类污染极难发现，因为结果"看起来合理"。
    """
    peer = scope.peer_agent_id or ""
    out: list[tuple[str, str, str, str]] = [(MemoryScopeKind.GLOBAL.value, "", "", "")]
    if scope.agent_id:
        out.append((MemoryScopeKind.AGENT.value, scope.agent_id, "", peer))
        if scope.session_id:
            # session 域记忆的 agent_id 恒为空（会话记忆不按智能体隔离）。
            out.append(
                (MemoryScopeKind.SESSION.value, "", scope.session_id, "")
            )
    return out


async def search(
    db: AsyncSession,
    scope: MemoryScope,
    query: str,
    *,
    model: ResolvedModel | None,
    limit: int = 10,
    memory_type: str = "",
    min_score: float | None = None,
    level: list[int] | None = None,
) -> list[SearchHit]:
    """
    语义搜索。model 为 None 或查询向量算不出来时返回空列表 ——
    调用方负责回落关键词搜索。

    Args:
        level: 过滤记忆层级（对齐 OpenViking）
            - None: 搜索所有层级（默认）
            - [0, 1]: 只搜索 L0/L1（目录层，用于分层召回）
            - [2]: 只搜索 L2（详细内容）

    ## 为什么在 Python 里算余弦而不用 SQL

    SQLite 没有向量运算。引 sqlite-vss 需要编译安装，破坏"clone 下来能跑"。
    而个人项目的记忆量级（几千条）用 Python 算足够：
    实测 3000 条 1024 维约 25ms，而一次 LLM 调用是几十秒。

    量级涨到十万条时该换方案 —— 那时 `visible_scopes` 的筛选条件
    仍然有效，只是把余弦下推到向量库。
    """
    if model is None or not query.strip():
        return []

    # 阈值默认读配置。合适的值【强依赖嵌入模型】——
    # bge-m3 的 0.4 和另一个模型的 0.4 不是一回事，所以默认 0（不过滤），
    # 由用户按自己的模型调。
    if min_score is None:
        min_score = settings.memory.search_min_score

    try:
        qresult = await embed_texts(model, [query])
    except ProviderError as e:
        log.warning("memory_search_embed_failed", error=str(e))
        return []
    if not qresult.vectors:
        return []
    qvec = qresult.vectors[0]

    rows = await _candidates(db, scope, memory_type=memory_type, level=level)

    log.debug(
        "memory_search_candidates",
        query=query[:50],
        memory_type=memory_type,
        candidates=len(rows),
        model=qresult.model,
        level=level,
    )

    hits: list[SearchHit] = []
    skipped_model_mismatch = 0

    # 收集所有候选的 URI 和密集向量分数
    dense_scores: dict[str, float] = {}
    uri_to_row: dict[str, Any] = {}  # 保存 row 信息用于构建 SearchHit

    for row in rows:
        # 【模型不一致的行直接跳过】而不是参与比较。
        #
        # 维度相同但模型不同的向量之间算余弦会得到一个"看起来合理"的数值,
        # 而那个数值毫无意义 —— 这正是最难发现的一类 bug：
        # 召回还在返回结果，只是结果没有意义。
        if row.embedding_model != qresult.model or row.embedding_dim != qresult.dim:
            skipped_model_mismatch += 1
            continue
        vec = unpack(row.embedding)
        if not vec:
            continue
        score = cosine(qvec, vec)
        if score >= min_score:
            dense_scores[row.uri] = score
            uri_to_row[row.uri] = row

    # ── 混合搜索：密集向量 + BM25（如果启用）──
    final_scores = dense_scores

    if settings.memory.recall_enable_hybrid_search and dense_scores:
        try:
            from app.infra.bm25 import BM25Config, BM25Index, normalize_scores
            from app.infra.hybrid_search import (
                HybridSearchConfig,
                adaptive_hybrid_search,
                hybrid_search,
            )

            # 构建临时 BM25 索引（只索引候选文档）
            bm25_config = BM25Config(
                k1=settings.memory.bm25_k1,
                b=settings.memory.bm25_b,
            )
            bm25_index = BM25Index(bm25_config)

            # 为每个候选文档添加到 BM25 索引
            for uri, row in uri_to_row.items():
                # 使用标题作为文档内容（简化，生产环境应该包含 body）
                text = row.title or ""
                bm25_index.add_document(uri, text)

            # BM25 搜索
            bm25_scores_raw = bm25_index.search(query, list(dense_scores.keys()))

            # 归一化 BM25 分数到 [0, 1]
            bm25_scores = normalize_scores(bm25_scores_raw)

            # 混合策略
            if settings.memory.hybrid_search_strategy == "adaptive":
                final_scores = await adaptive_hybrid_search(
                    query=query,
                    dense_scores=dense_scores,
                    sparse_scores=bm25_scores,
                )
            else:  # "query_based" 或 "balanced"
                hybrid_config = HybridSearchConfig(
                    default_dense_weight=settings.memory.hybrid_default_dense_weight,
                    default_sparse_weight=settings.memory.hybrid_default_sparse_weight,
                    keyword_dense_weight=settings.memory.hybrid_keyword_dense_weight,
                    keyword_sparse_weight=settings.memory.hybrid_keyword_sparse_weight,
                    semantic_dense_weight=settings.memory.hybrid_semantic_dense_weight,
                    semantic_sparse_weight=settings.memory.hybrid_semantic_sparse_weight,
                )
                final_scores = await hybrid_search(
                    query=query,
                    dense_scores=dense_scores,
                    sparse_scores=bm25_scores,
                    config=hybrid_config,
                )

            log.debug(
                "hybrid_search_applied",
                dense_hits=len(dense_scores),
                bm25_hits=len(bm25_scores),
                mixed_hits=len(final_scores),
            )
        except Exception as e:
            log.warning(
                "hybrid_search_failed",
                error=str(e),
                fallback_to_dense=True,
            )
            # 失败时回退到纯密集向量
            final_scores = dense_scores

    # 构建 SearchHit 列表
    for uri, score in final_scores.items():
        row = uri_to_row[uri]
        hits.append(
            SearchHit(
                uri=row.uri,
                memory_type=row.memory_type,
                title=row.title,
                score=score,
                scope=row.scope,
            )
        )

    log.debug(
        "memory_search_done",
        hits=len(hits),
        candidates=len(rows),
        skipped_model=skipped_model_mismatch,
        min_score=min_score,
    )

    hits.sort(key=lambda h: (-h.score, h.uri))
    return hits[:limit]


async def _candidates(
    db: AsyncSession,
    scope: MemoryScope,
    *,
    memory_type: str = "",
    level: list[int] | None = None,
) -> list[MemoryIndex]:
    """
    按三层隔离取候选行。只取有向量的。

    Args:
        level: 过滤记忆层级（对齐 OpenViking）
            - None: 搜索所有层级
            - [0, 1]: 只搜索 L0/L1（目录层）
            - [2]: 只搜索 L2（详细内容）
    """
    from sqlalchemy import and_, or_

    scopes_list = visible_scopes(scope)

    log.debug(
        "memory_candidates_query",
        scope_agent=scope.agent_id,
        scope_session=scope.session_id,
        scope_peer=scope.peer_agent_id,
        memory_type=memory_type,
        level=level,
    )

    conditions = []
    for scope_kind, agent_id, session_id, peer_agent_id in scopes_list:
        conditions.append(
            and_(
                MemoryIndex.scope == scope_kind,
                MemoryIndex.agent_id == agent_id,
                MemoryIndex.session_id == session_id,
                # 空串 = 非 peer 视角。必须显式比较，否则「A 眼中的 B」
                # 会混进 A 自己的记忆里。
                MemoryIndex.peer_agent_id == peer_agent_id,
            )
        )

    stmt = select(MemoryIndex).where(
        or_(*conditions),
        MemoryIndex.embedding.is_not(None),
    )
    if memory_type:
        stmt = stmt.where(MemoryIndex.memory_type == memory_type)

    # 添加 level 过滤（对齐 OpenViking）
    if level is not None:
        stmt = stmt.where(MemoryIndex.level.in_(level))

    return list((await db.execute(stmt)).scalars())


# ── 换模型后的重算 ────────────────────────────────


async def stale_count(db: AsyncSession, *, model: ResolvedModel | None) -> dict[str, int]:
    """
    统计需要重算的行数。给设置页显示"有 N 条记忆的向量已失效"。

    三种失效：
    - never   从没算过（新记忆，或用户刚配上嵌入模型）
    - model   模型/维度变了（用户换了嵌入模型）
    - content 记忆改过但向量没跟上（embedded_hash != content_hash）
    """
    rows = list((await db.execute(select(MemoryIndex))).scalars())
    out = {"total": len(rows), "never": 0, "model": 0, "content": 0, "fresh": 0}

    for row in rows:
        if not should_vectorize(row.uri):
            continue
        if row.embedding is None or not row.embedding_model:
            out["never"] += 1
        elif model is not None and (
            row.embedding_model != model.model_id
            or (model.extra.get("embedding_dim") and row.embedding_dim != model.extra["embedding_dim"])
        ):
            out["model"] += 1
        elif row.embedded_hash != row.content_hash:
            out["content"] += 1
        else:
            out["fresh"] += 1
    return out


async def revectorize_all(
    db: AsyncSession,
    *,
    model: ResolvedModel | None,
    read_item: Any,
    only_stale: bool = True,
) -> VectorizeReport:
    """
    一键重算。用户换嵌入模型后手动触发。

    ## 为什么不自动重算

    换模型可能意味着几千次 API 调用。自动跑会在用户不知情的情况下烧钱，
    而且期间召回质量是混乱的（一半新向量一半旧向量）。

    所以：换模型后旧向量【立即停止参与召回】（search 里按 model 筛掉），
    但重算要用户显式点。这样最坏情况是"召回暂时没有语义结果"，
    而不是"扣了一笔意外的费用"。

    only_stale=False 时全量重算 —— 用于"我怀疑向量算错了"这种情况。
    """
    rows = list((await db.execute(select(MemoryIndex))).scalars())
    targets: list[str] = []

    for row in rows:
        if not should_vectorize(row.uri):
            continue
        if not only_stale:
            targets.append(row.uri)
            continue
        fresh = (
            row.embedding is not None
            and model is not None
            and row.embedding_model == model.model_id
            and row.embedded_hash == row.content_hash
        )
        if not fresh:
            targets.append(row.uri)

    log.info("memory_revectorize_start", targets=len(targets), only_stale=only_stale)
    return await vectorize_uris(db, targets, model=model, read_item=read_item)


async def clear_all(db: AsyncSession) -> int:
    """
    清空所有向量。换模型且不想立刻重算时用 —— 让召回干净地回落关键词，
    而不是留着一批永远不参与比较的死数据占空间。
    """
    result = await db.execute(
        update(MemoryIndex).values(embedding=None, embedding_model="", embedding_dim=0, embedded_hash="")
    )
    await db.commit()
    return int(result.rowcount or 0)
