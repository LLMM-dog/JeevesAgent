"""
记忆索引的读写。

索引是缓存，文件是真源。所有函数都遵守一条：【索引操作失败不影响记忆本身】。
写记忆成功但更新索引失败时，记录 warning 并继续 —— 记忆已经落盘了，
索引下次 rebuild 就能补回来。反过来（因为索引失败就认为写记忆失败）
会让调用方重试，而重试会产生重复的 version 递增。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.memory.models import MemoryItem
from app.modules.memory.models_db import MemoryIndex

log = structlog.get_logger(__name__)


def content_hash(item: MemoryItem) -> str:
    """
    正文 + 业务字段的哈希。幂等写入靠它判断"内容有没有变"。

    ## 为什么不哈希整个文件内容

    文件内容含 version 和 updated_at，它们每次写都变 —— 哈希整个文件的话
    永远判定为"变了"，幂等就失效了。

    ## 为什么排序 key

    dict 的迭代顺序在同一进程内稳定，但跨进程（重启后）不保证 ——
    Python 3.7+ 的 dict 保持插入顺序，而插入顺序取决于 LLM 输出 JSON 的
    字段顺序。不排序会让同样的内容在两次运行里算出不同的哈希。
    """
    # 用 merge_source 而非 body：body 是渲染结果，含模板套的壳。
    # 模板里若有时间戳这类每次都变的东西，拿 body 算哈希会让幂等永远失效。
    payload = {
        "body": item.merge_source,
        "fields": {k: v for k, v in sorted(item.fields.items()) if k != "content"},
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def get(db: AsyncSession, uri: str) -> MemoryIndex | None:
    return (await db.execute(select(MemoryIndex).where(MemoryIndex.uri == uri))).scalars().first()


async def upsert(
    db: AsyncSession,
    uri: str,
    item: MemoryItem,
    *,
    embedding_model: str = "",
    embedding_dim: int = 0,
    commit: bool = True,
) -> None:
    """写入或更新一行。active_count 保留旧值 —— 它是召回统计，与内容无关。"""
    row = await get(db, uri)
    digest = content_hash(item)

    if row is None:
        db.add(
            MemoryIndex(
                uri=uri,
                scope=item.scope.value,
                memory_type=item.memory_type,
                agent_id=item.agent_id,
                session_id=item.session_id,
                peer_agent_id=item.peer_agent_id,
                title=item.title,
                version=item.version,
                content_hash=digest,
                embedding_model=embedding_model,
                embedding_dim=embedding_dim,
                file_updated_at=item.updated_at,
            )
        )
    else:
        row.scope = item.scope.value
        row.memory_type = item.memory_type
        row.agent_id = item.agent_id
        row.session_id = item.session_id
        row.peer_agent_id = item.peer_agent_id
        row.title = item.title
        row.version = item.version
        row.content_hash = digest
        row.file_updated_at = item.updated_at
        if embedding_model:
            row.embedding_model = embedding_model
            row.embedding_dim = embedding_dim

    if commit:
        await db.commit()


async def set_embedding(
    db: AsyncSession,
    uri: str,
    *,
    vector: list[float],
    model: str,
    dim: int,
    content_hash: str,
    level: int = 2,
    commit: bool = False,
) -> bool:
    """
    存一条记忆的向量。行不存在返回 False（索引与文件不一致，调用方决定怎么办）。

    Args:
        level: 记忆层级（0=L0/Abstract, 1=L1/Overview, 2=L2/Details）

    ## 为什么默认 commit=False

    向量化是批量的。每条都 commit 会产生 N 次 fsync —— 实测 100 条记忆
    从 0.3 秒变成 4 秒。由调用方在批次末尾统一提交。

    ## 为什么同时存 content_hash

    它记的是"向量算的是哪一版内容"。与行上的 content_hash 比较能发现
    "记忆改过但向量没重算"——那时召回用的是旧语义，是个必须能被
    发现的状态，而不是静默的错误结果。
    """
    row = await db.get(MemoryIndex, uri)
    if row is None:
        return False

    from app.modules.memory.vectorize import pack

    row.embedding = pack(vector)
    row.embedding_model = model
    row.embedding_dim = dim
    row.embedded_hash = content_hash
    row.level = level

    if commit:
        await db.commit()
    return True


async def remove(db: AsyncSession, uri: str, *, commit: bool = True) -> None:
    await db.execute(delete(MemoryIndex).where(MemoryIndex.uri == uri))
    if commit:
        await db.commit()


async def remove_session(db: AsyncSession, session_id: str) -> int:
    result = await db.execute(
        delete(MemoryIndex).where(MemoryIndex.session_id == session_id)
    )
    await db.commit()
    return result.rowcount or 0


async def remove_agent(db: AsyncSession, agent_id: str) -> int:
    result = await db.execute(delete(MemoryIndex).where(MemoryIndex.agent_id == agent_id))
    await db.commit()
    return result.rowcount or 0


async def record_hit(db: AsyncSession, uris: list[str]) -> None:
    """
    召回命中，递增 active_count。热度分的频率分量。

    批量更新而非逐条：一次召回可能命中十几条，逐条 UPDATE + commit 会有
    十几次事务开销，而这发生在对话的关键路径上。
    """
    if not uris:
        return
    rows = (await db.execute(select(MemoryIndex).where(MemoryIndex.uri.in_(uris)))).scalars().all()
    for row in rows:
        row.active_count += 1
    await db.commit()


async def list_rows(
    db: AsyncSession,
    *,
    agent_id: str | None = None,
    session_id: str | None = None,
    memory_type: str = "",
    scope: str = "",
    limit: int = 0,
) -> list[MemoryIndex]:
    """
    按条件列举。

    agent_id / session_id 用 None 表示"不筛这一维"，空串表示"筛出空串的行"
    （global 域的记忆 agent_id 就是空串）。这个区分是必要的：
    传空串当"不筛"会让"列出全局记忆"变得无法表达。
    """
    stmt = select(MemoryIndex)
    if agent_id is not None:
        stmt = stmt.where(MemoryIndex.agent_id == agent_id)
    if session_id is not None:
        stmt = stmt.where(MemoryIndex.session_id == session_id)
    if memory_type:
        stmt = stmt.where(MemoryIndex.memory_type == memory_type)
    if scope:
        stmt = stmt.where(MemoryIndex.scope == scope)
    stmt = stmt.order_by(MemoryIndex.file_updated_at.desc())
    if limit > 0:
        stmt = stmt.limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def count(db: AsyncSession) -> int:
    return int((await db.execute(select(func.count()).select_from(MemoryIndex))).scalar() or 0)


async def stale_embeddings(db: AsyncSession, *, model: str, dim: int) -> list[str]:
    """
    嵌入模型漂移检测：向量是用别的模型算的那些记忆。

    换嵌入模型后维度变化，旧向量的相似度计算【毫无意义但不报错】——
    召回还在返回结果，只是结果是随机的。这是最难发现的一类 bug，
    所以要能显式查出来。
    """
    rows = (
        await db.execute(
            select(MemoryIndex.uri).where(
                (MemoryIndex.embedding_model != model) | (MemoryIndex.embedding_dim != dim)
            )
        )
    ).scalars().all()
    return list(rows)


async def replace_all(db: AsyncSession, entries: list[tuple[str, MemoryItem]]) -> int:
    """
    全量重建。

    ## 为什么保留 active_count

    热度统计是行为数据，重建索引不该抹掉它 —— 那会让所有记忆的热度归零，
    召回排序在重建后突然变差，而用户看不出原因。
    """
    old_hits: dict[str, int] = {
        row.uri: row.active_count
        for row in (await db.execute(select(MemoryIndex))).scalars().all()
    }
    old_embeddings: dict[str, tuple[str, int]] = {}
    for row in (await db.execute(select(MemoryIndex))).scalars().all():
        old_embeddings[row.uri] = (row.embedding_model, row.embedding_dim)

    await db.execute(delete(MemoryIndex))

    for uri, item in entries:
        model, dim = old_embeddings.get(uri, ("", 0))
        db.add(
            MemoryIndex(
                uri=uri,
                scope=item.scope.value,
                memory_type=item.memory_type,
                agent_id=item.agent_id,
                session_id=item.session_id,
                peer_agent_id=item.peer_agent_id,
                title=item.title,
                version=item.version,
                active_count=old_hits.get(uri, 0),
                content_hash=content_hash(item),
                embedding_model=model,
                embedding_dim=dim,
                file_updated_at=item.updated_at,
            )
        )
    await db.commit()
    return len(entries)


def row_to_dict(row: MemoryIndex) -> dict[str, Any]:
    """给接口返回用。"""
    return {
        "uri": row.uri,
        "scope": row.scope,
        "memory_type": row.memory_type,
        "agent_id": row.agent_id,
        "session_id": row.session_id,
        "peer_agent_id": row.peer_agent_id,
        "title": row.title,
        "version": row.version,
        "active_count": row.active_count,
        "updated_at": row.file_updated_at,
    }
