"""
记忆系统的唯一对外入口。

agent loop、路由、提取流程只 import 这个模块，不直接碰 file_store 或 index。

## 这一层负责什么

- 隔离校验：scope 能不能访问这个记忆类型
- 字段合并：按 merge_op 把 LLM 的输出合进已有内容
- 幂等：内容没变就不写盘
- 索引同步：写完文件顺手更新 memory_index

## 不负责什么

- 提取（LLM 编排）—— 之后的 extract.py
- 召回（向量搜索 + 预算裁剪）—— 之后的 recall.py
- 向量化 —— 之后的 vectorize.py

这三件都比本模块大，混进来会让这个文件失控（OpenViking 的 memory_updater.py
是 64KB，因为它把提取编排、并发合并、向量化、链接图全塞在一层）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.llm.port import ResolvedModel
from app.modules.memory import index as index_mod
from app.modules.memory import layout, registry, render
from app.modules.memory import vectorize as vectorize_mod
from app.modules.memory.file_store import FileMemoryStore
from app.modules.memory.layout import PathScopeError
from app.modules.memory.merge import MergeError, apply_merge
from app.modules.memory.models import (
    BatchResult,
    DeleteResult,
    MemoryItem,
    MemoryScope,
    WriteOp,
    WriteResult,
)
from app.modules.memory.schema import (
    MemoryScopeKind,
    MemoryTypeSchema,
    OperationMode,
)

log = structlog.get_logger(__name__)

_store = FileMemoryStore()

# 每个 uri 一把锁。
#
# ## 为什么需要锁
#
# 同一个文件的并发写会丢更新：两个协程都读到 version=3，各自合并后都写
# version=4，后写的覆盖前面的改动，而前面那次改动【没有任何痕迹】。
#
# ## 为什么不用 OpenViking 那套
#
# 它的 StreamingMemoryUpdater 有 72KB：攒批、二次 LLM 合并、租约。
# 那是为"多用户多会话同时 commit"设计的。Jeeves 是单用户单进程，
# 一个会话一次 commit，asyncio 锁足够。
#
# 用 dict 而非 WeakValueDictionary：锁对象很小，而 weak 引用会让
# "锁刚被创建还没被 acquire 时被 GC"变成一个需要考虑的竞态。
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(uri: str) -> asyncio.Lock:
    lock = _locks.get(uri)
    if lock is None:
        lock = asyncio.Lock()
        _locks[uri] = lock
    return lock


# ── 类型可见性 ────────────────────────────────────


def visible_types(scope: MemoryScope) -> list[MemoryTypeSchema]:
    """这个 scope 能读写的记忆类型。"""
    return [s for s in registry.get_schemas().enabled() if scope.allows(s.scope)]


def get_schema(memory_type: str) -> MemoryTypeSchema | None:
    return registry.get_schemas().get(memory_type)


def _require_schema(scope: MemoryScope, memory_type: str) -> MemoryTypeSchema:
    schema = get_schema(memory_type)
    if schema is None:
        known = ", ".join(registry.get_schemas().names())
        raise ValueError(f"未知的记忆类型：{memory_type}。已注册：{known}")
    if not schema.enabled:
        raise ValueError(f"记忆类型 {memory_type} 已被禁用（enabled: false）")
    if not scope.allows(schema.scope):
        raise PathScopeError(
            f"当前 scope 无权访问 {schema.scope.value} 域的 {memory_type}："
            f"agent_id={scope.agent_id!r} session_id={scope.session_id!r}"
        )
    return schema


# ── 读 ──────────────────────────────────────────


async def get(scope: MemoryScope, memory_type: str, key: str = "") -> MemoryItem | None:
    """
    读一条记忆。

    key 对单文件类型（profile / soul / identity）无意义，留空。
    多文件类型的 key 是文件名的主体部分（preferences 的 topic、
    tool_notes 的 tool_name）。
    """
    schema = _require_schema(scope, memory_type)

    if schema.single_file:
        return await _store.read(scope, schema, schema.filename_template)

    if not key:
        raise ValueError(f"{memory_type} 是多文件类型，必须给 key")

    # 用 key 反填模板变量。多文件类型的 filename_template 通常只有一个变量
    # （{{ topic }}.md），所以填第一个非 system 字段就够。
    # events 那种多变量模板（含 extract_context）走不到这里 —— 它是 add_only，
    # 按 key 精确读没有意义。
    rel_path = await _rel_path_from_key(schema, key)
    return await _store.read(scope, schema, rel_path)


async def _rel_path_from_key(schema: MemoryTypeSchema, key: str) -> str:
    from app.modules.memory.schema import template_variables

    variables = template_variables(schema.filename_template) - {"extract_context"}
    if len(variables) != 1:
        raise ValueError(
            f"{schema.memory_type} 的 filename_template 有 {len(variables)} 个变量，无法按单个 key 定位。"
            "请用 list_items 或 read_uri。"
        )
    var = next(iter(variables))
    return await _store.resolve_path(MemoryScope(), schema, {var: key})


async def read_uri(uri: str) -> MemoryItem | None:
    return await _store.read_uri(uri)


async def list_items(scope: MemoryScope, memory_type: str = "") -> list[MemoryItem]:
    """
    列举记忆。memory_type 为空时列举这个 scope 下所有类型。

    不走索引而是直接扫文件：文件是真源，而这个方法的调用方（设置页、
    提取阶段的 prefetch）需要正文，索引里没有正文。
    索引的价值在"只要元数据"的场景（list_index）。
    """
    if memory_type:
        schema = _require_schema(scope, memory_type)
        return await _store.list_items(scope, schema)

    out: list[MemoryItem] = []
    for schema in visible_types(scope):
        out.extend(await _store.list_items(scope, schema))
    out.sort(key=lambda it: (-it.updated_at, it.uri))
    return out


async def list_index(
    db: AsyncSession,
    scope: MemoryScope,
    *,
    memory_type: str = "",
    limit: int = 0,
) -> list[dict[str, Any]]:
    """只要元数据的列举。设置页的记忆列表用这个，不读文件。"""
    rows = await index_mod.list_rows(
        db,
        agent_id=scope.agent_id or None,
        session_id=scope.session_id or None,
        memory_type=memory_type,
        limit=limit,
    )
    return [index_mod.row_to_dict(r) for r in rows]


# ── 写 ──────────────────────────────────────────


async def write(
    scope: MemoryScope,
    memory_type: str,
    fields: dict[str, Any],
    *,
    db: AsyncSession | None = None,
    extraction_id: str = "",
    trace_id: str = "",
    extract_context: Any = None,
) -> WriteResult:
    """
    写一条记忆。已存在则按 merge_op 合并。

    db 可以为 None —— 那时只写文件不更新索引。提取流程会传 db，
    而单元测试和脚本可以不传。
    """
    schema = _require_schema(scope, memory_type)
    rel_path = await _store.resolve_path(scope, schema, fields, extract_context=extract_context)
    uri_hint = f"{schema.memory_type}:{rel_path}"

    async with _lock_for(uri_hint):
        return await _write_locked(
            scope,
            schema,
            rel_path,
            fields,
            db=db,
            extraction_id=extraction_id,
            trace_id=trace_id,
            extract_context=extract_context,
        )


async def _write_locked(
    scope: MemoryScope,
    schema: MemoryTypeSchema,
    rel_path: str,
    fields: dict[str, Any],
    *,
    db: AsyncSession | None,
    extraction_id: str,
    trace_id: str,
    extract_context: Any,
) -> WriteResult:
    # 【写前重读磁盘】，不用调用方传来的旧内容。
    #
    # 理由（OpenViking memory_updater.py:1048 写明）：同一批操作里可能有多条
    # patch 打到同一个 URI，后一条必须看到前一条的结果。用缓存会让第二条
    # patch 的 SEARCH 匹配失败。
    old = await _store.read(scope, schema, rel_path)

    if schema.operation_mode is OperationMode.UPDATE_ONLY and old is None:
        return WriteResult(
            uri="",
            memory_type=schema.memory_type,
            changed=False,
            created=False,
            version=0,
            error="update_only：目标不存在，跳过",
        )

    if schema.operation_mode is OperationMode.ADD_ONLY and old is not None:
        # 只增不改：换个名字而不是覆盖或跳过。见 file_store.next_available_path。
        rel_path = await _store.next_available_path(scope, schema, rel_path)
        old = None

    try:
        merged = _merge_fields(schema, old, fields)
    except MergeError as e:
        log.warning(
            "memory_merge_failed",
            memory_type=schema.memory_type,
            field=e.field_name,
            error=str(e),
        )
        return WriteResult(
            uri=old.uri if old else "",
            memory_type=schema.memory_type,
            changed=False,
            created=False,
            version=old.version if old else 0,
            error=str(e),
            before=old.body if old else "",
        )

    body = render.render_body(schema, merged, extract_context=extract_context)
    item = render.build_item(schema, scope, merged, body, old=old)

    # 幂等：内容没变就不写盘、version 不递增。
    #
    # 没有这一步的话每次 commit 都产生一堆无意义的 version 跳动和 git diff ——
    # 而记忆目录进 git 的全部价值就是 diff 可读。
    if old is not None and index_mod.content_hash(item) == index_mod.content_hash(old):
        return WriteResult(
            uri=old.uri,
            memory_type=schema.memory_type,
            changed=False,
            created=False,
            version=old.version,
            before=old.body,
            after=old.body,
        )

    uri = await _store.write(
        scope, schema, rel_path, item, extraction_id=extraction_id, trace_id=trace_id
    )
    item.uri = uri

    # 每次有效写入都留一条结构化日志。
    #
    # 【不打正文】—— 记忆里有用户的个人信息，日志会进文件、可能被贴到 issue 里。
    # 正文在 memory_diff 里（那个文件和记忆同域，权限一致）。
    # 这里只打"改了哪个、从第几版到第几版、正文长度变化"，
    # 足够回答"这次提取动了什么"，不泄露内容。
    log.info(
        "memory_written",
        uri=uri,
        memory_type=schema.memory_type,
        created=old is None,
        version=item.version,
        chars_before=len(old.body) if old else 0,
        chars_after=len(item.body),
        extraction_id=extraction_id,
    )

    if db is not None:
        # 索引失败不影响记忆本身 —— 文件已经落盘了，下次 rebuild 能补回来。
        # 反过来（因为索引失败就报写入失败）会让调用方重试，
        # 而重试会再递增一次 version。
        try:
            await index_mod.upsert(db, uri, item)
        except Exception as e:  # noqa: BLE001
            log.warning("memory_index_upsert_failed", uri=uri, error=str(e))

    return WriteResult(
        uri=uri,
        memory_type=schema.memory_type,
        changed=True,
        created=old is None,
        version=item.version,
        before=old.body if old else "",
        after=item.body,
    )


def _merge_fields(
    schema: MemoryTypeSchema, old: MemoryItem | None, incoming: dict[str, Any]
) -> dict[str, Any]:
    """
    按 merge_op 逐字段合并。

    ## 为什么 content 的 current 取 merge_source 而不是 body

    content 字段的值在写入时被渲染成正文，frontmatter 里不存它
    （见 render.serialize）。所以下一次合并必须把正文当 current，
    否则 SEARCH 匹配的是空值。

    但【不能直接用 body】—— content_template 会在外面套一层壳
    （tool_notes 的 "# 工具：xxx" + 计数行）。拿渲染结果当输入再渲染一次，
    壳会被重复叠加。实测在试验场里 run_shell.md 长出了两个标题和两组计数行，
    而 version 每涨一次多一层。

    merge_source 优先返回 raw_content（渲染前的原始值），
    只有没有模板时才回落 body。

    OpenViking 靠 MemoryFile.content 始终是原始内容来避免这件事
    （模板只在 serialize 时套用，dataclass.py:252 的 plain_content
    只剥链接不剥模板）。我们把原始值单独存了一份。
    """
    merged: dict[str, Any] = {}

    for field in schema.fields:
        if field.name in incoming:
            if old is None:
                current = None
            elif field.name == "content":
                current = old.merge_source
            else:
                current = old.fields.get(field.name)
            merged[field.name] = apply_merge(field, current, incoming[field.name])
        elif old is not None:
            # LLM 没提到这个字段 → 保留旧值。
            # 不保留的话一次只改 summary 的更新会把 goal 抹掉。
            merged[field.name] = (
                old.merge_source if field.name == "content" else old.fields.get(field.name)
            )
        elif field.init_value is not None:
            merged[field.name] = field.init_value

    return {k: v for k, v in merged.items() if v is not None}


async def write_many(
    ops: list[WriteOp],
    *,
    db: AsyncSession | None = None,
    extraction_id: str = "",
    trace_id: str = "",
) -> BatchResult:
    """
    批量写。

    ## 为什么串行而不是 gather

    同一批里可能有多条操作打到同一个文件（两条 patch 改 profile 的不同段落）。
    并发执行时后者读到的是前者写之前的内容，SEARCH 会匹配失败 —— 而失败信息
    看起来像"LLM 写错了 search"，实际是并发问题。

    锁能挡住数据丢失，但挡不住这类假失败。串行执行让"后一条看到前一条的结果"
    成为确定的行为。记忆写入不在热路径上，串行的代价可以接受。
    """
    result = BatchResult(extraction_id=extraction_id, trace_id=trace_id)
    for op in ops:
        try:
            result.results.append(
                await write(
                    op.scope,
                    op.memory_type,
                    op.fields,
                    db=db,
                    extraction_id=op.extraction_id or extraction_id,
                    trace_id=op.trace_id or trace_id,
                    extract_context=op.extract_context,
                )
            )
        except (ValueError, PathScopeError) as e:
            result.results.append(
                WriteResult(
                    uri="",
                    memory_type=op.memory_type,
                    changed=False,
                    created=False,
                    version=0,
                    error=str(e),
                )
            )
    return result


async def delete_uri(uri: str, *, db: AsyncSession | None = None) -> bool:
    """删一条。痕迹需求见 delete_with_trace —— 这个函数只回布尔。"""
    return (await delete_with_trace(uri, db=db)).ok


async def delete_with_trace(uri: str, *, db: AsyncSession | None = None) -> DeleteResult:
    """
    删一条并保留正文。

    ## 为什么删除前要先读

    删掉的记忆【没有别处可查】。不留正文的话，"模型把一条重要经验删了"
    这件事只剩一行 uri，无法判断该不该恢复，也无法恢复。

    多一次读的成本换可追溯性，值得 —— 删除是低频操作。
    """
    item = await _store.read_uri(uri)
    ok = await _store.delete_uri(uri)

    if not ok:
        return DeleteResult(uri=uri, error="文件不存在或删除失败")

    if db is not None:
        try:
            await index_mod.remove(db, uri)
        except Exception as e:  # noqa: BLE001
            log.warning("memory_index_remove_failed", uri=uri, error=str(e))

    return DeleteResult(
        uri=uri,
        memory_type=item.memory_type if item else "",
        deleted_content=item.body if item else "",
    )


# ── 目录索引 ──────────────────────────────────────


async def resolve_embedding_model(db: AsyncSession) -> ResolvedModel | None:
    """
    解析当前配置的嵌入模型。没配返回 None。

    ## 为什么没配是 None 而不是异常

    嵌入模型是【可选】的。没配时向量召回关闭、回落关键词搜索，
    系统仍然完全可用。抛异常会让"没配嵌入模型"变成"记忆功能不可用"，
    而那是过度耦合。

    不回落到 chat 模型：对话模型没有 /embeddings 端点，
    调用它只会得到 404，而那个错误看起来像配置错误。
    """
    from app.modules.endpoint import service as endpoint_service

    try:
        resolved = await endpoint_service.resolve(db, purpose="embedding")
    except Exception as e:  # noqa: BLE001
        log.debug("memory_embedding_model_unavailable", error=str(e))
        return None

    # resolve 的兜底链会回落到 chat。嵌入必须是【显式配置】的 ——
    # 拿一个对话模型去调 /embeddings 得到的 404 会被误读成配置错误。
    if resolved.purpose != "embedding":
        log.debug("memory_embedding_model_not_configured", fell_back_to=resolved.purpose)
        return None
    return resolved


async def vectorize(db: AsyncSession, uris: list[str]) -> vectorize_mod.VectorizeReport:
    """给这些 uri 算向量。嵌入模型没配时整体跳过。"""
    model = await resolve_embedding_model(db)
    return await vectorize_mod.vectorize_uris(db, uris, model=model, read_item=read_uri)


async def search_semantic(
    db: AsyncSession,
    scope: MemoryScope,
    query: str,
    *,
    limit: int = 10,
    memory_type: str = "",
) -> list[vectorize_mod.SearchHit]:
    """语义搜索。搜索范围按三层隔离筛，见 vectorize.visible_scopes。"""
    model = await resolve_embedding_model(db)
    return await vectorize_mod.search(
        db, scope, query, model=model, limit=limit, memory_type=memory_type
    )


async def vector_status(db: AsyncSession) -> dict[str, int]:
    """向量的新鲜度统计。给设置页显示"有 N 条已失效"。"""
    model = await resolve_embedding_model(db)
    return await vectorize_mod.stale_count(db, model=model)


async def revectorize(db: AsyncSession, *, only_stale: bool = True) -> vectorize_mod.VectorizeReport:
    """
    一键重算向量。用户换嵌入模型后手动触发。

    不自动跑的理由见 vectorize.revectorize_all 的说明 ——
    那可能是几千次 API 调用。
    """
    model = await resolve_embedding_model(db)
    return await vectorize_mod.revectorize_all(
        db, model=model, read_item=read_uri, only_stale=only_stale
    )


async def clear_vectors(db: AsyncSession) -> int:
    """清空所有向量，让召回干净地回落关键词。"""
    return await vectorize_mod.clear_all(db)


async def write_diff(batch: BatchResult, *, scope: MemoryScope) -> str:
    """
    把一批改动的痕迹落盘。返回文件路径（相对 data/memory/）。

    ## 为什么落盘而不是只打日志

    日志会滚动、会被过滤、混在几万行里。而"上周它还知道我用 uv，怎么忘了"
    这类问题需要按时间回溯【记忆本身的变更史】—— 那要求痕迹和记忆放在一起、
    活得和记忆一样久。

    OpenViking 把它写成归档目录里的 memory_diff.json。我们同样落盘，
    但放在 .trace/ 下按 extraction_id 命名 —— 我们没有"归档"这个概念，
    而按提取批次分文件让"这一次提取做了什么"是一个文件而非一段日志。
    """
    diff = batch.to_diff()
    name = batch.extraction_id or f"anon_{diff['extracted_at']}"
    rel = f"{name}.json"
    path = layout.trace_dir(scope) / rel
    await asyncio.to_thread(_write_json, path, diff)

    log.info(
        "memory_diff_written",
        extraction_id=batch.extraction_id,
        adds=diff["summary"]["total_adds"],
        updates=diff["summary"]["total_updates"],
        deletes=diff["summary"]["total_deletes"],
        unchanged=diff["summary"]["total_unchanged"],
        errors=diff["summary"]["total_errors"],
        path=str(path),
    )
    return rel


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def refresh_overview(scope: MemoryScope, memory_type: str) -> str:
    """
    重建某类记忆的 .overview.md。schema 没有 overview_template 时是 no-op。

    在写入之后调用，不是写入的一部分 —— 一批写入结束后重建一次比每写一条
    重建一次省得多（重建要读整个目录）。
    """
    schema = _require_schema(scope, memory_type)
    if not schema.overview_template:
        return ""

    items = await _store.list_items(scope, schema)
    directory = layout.type_dir(scope, schema)
    content = render.render_overview(schema, items, directory.name)
    return await _store.write_overview(scope, schema, "", content)


# ── 生命周期 ──────────────────────────────────────


async def init_agent(agent_id: str, *, db: AsyncSession | None = None) -> list[str]:
    """
    建目录骨架 + 写单文件类型的初值。智能体创建时调用一次。

    幂等：已存在的文件不覆盖，可以重复调用（重启、修复）。
    """
    created = await _store.init_agent(agent_id, registry.get_schemas().enabled())

    if db is not None and created:
        for uri in created:
            item = await _store.read_uri(uri)
            if item is not None:
                try:
                    await index_mod.upsert(db, uri, item)
                except Exception as e:  # noqa: BLE001
                    log.warning("memory_index_upsert_failed", uri=uri, error=str(e))

    log.info("memory_agent_initialized", agent_id=agent_id, created=len(created))
    return created


async def drop_agent(agent_id: str, *, db: AsyncSession | None = None) -> int:
    count = await _store.drop_agent(agent_id)
    if db is not None:
        await index_mod.remove_agent(db, agent_id)
    log.info("memory_agent_dropped", agent_id=agent_id, files=count)
    return count


async def drop_session(agent_id: str, session_id: str, *, db: AsyncSession | None = None) -> int:
    count = await _store.drop_session(agent_id, session_id)
    if db is not None:
        await index_mod.remove_session(db, agent_id, session_id)
    log.info("memory_session_dropped", agent_id=agent_id, session_id=session_id, files=count)
    return count


async def rebuild_index(db: AsyncSession) -> int:
    """
    从文件全量重建索引。

    什么时候需要：手动改过记忆文件、索引表被清空、或怀疑索引与文件不一致。
    保留 active_count（热度是行为数据，不该被重建抹掉）。
    """
    entries = await _store.iter_all()
    n = await index_mod.replace_all(db, entries)
    log.info("memory_index_rebuilt", count=n)
    return n


def diagnostics() -> list[dict[str, str]]:
    """schema 加载期的问题。设置页显示用。"""
    return [
        {"level": d.level, "message": d.message, "source": d.source}
        for d in registry.get_schemas().diagnostics
    ]


__all__ = [
    "MemoryScope",
    "MemoryScopeKind",
    "delete_uri",
    "delete_with_trace",
    "diagnostics",
    "write_diff",
    "drop_agent",
    "drop_session",
    "get",
    "get_schema",
    "init_agent",
    "list_index",
    "list_items",
    "read_uri",
    "rebuild_index",
    "refresh_overview",
    "visible_types",
    "write",
    "write_many",
]
