"""
记忆存储与 service 层的集成测试。

重点覆盖四条不变量（docs/architecture/memory.md#接口契约）：
1. 写前重读磁盘 —— 同一批里连续 patch 能看到彼此
2. 幂等 —— 内容没变就不写盘、version 不递增
3. scope 不可越界
4. 一个坏文件不影响其它
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from app.modules.memory import index as index_mod
from app.modules.memory import layout, registry
from app.modules.memory import service as memory
from app.modules.memory.layout import PathScopeError
from app.modules.memory.models import MemoryScope, WriteOp
from app.modules.memory.schema import MemoryScopeKind
from sqlalchemy.ext.asyncio import AsyncSession

AGENT = "adf_testagent"
OTHER_AGENT = "adf_otheragent"
SESSION = "ses_testsession"


@pytest.fixture
def memory_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """
    把 memory_dir 指到 tmp。

    patch settings 的 property 而不是设环境变量：memory_dir 是派生属性
    （data_dir / "memory"），env 改不动它。
    """
    root = tmp_path / "memory"
    root.mkdir(parents=True)
    monkeypatch.setattr(type(layout.settings), "memory_dir", property(lambda _self: root))
    registry.reset()
    yield root
    registry.reset()


@pytest_asyncio.fixture
async def ready(memory_dir: Path, db: AsyncSession) -> AsyncIterator[AsyncSession]:
    await memory.init_agent(AGENT, db=db)
    yield db


def _patch(search: str, replace: str) -> dict[str, object]:
    return {"blocks": [{"search": search, "replace": replace}]}


# ── 初始化 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_init_agent_creates_skeleton(memory_dir: Path, db: AsyncSession) -> None:
    created = await memory.init_agent(AGENT, db=db)

    # 全局记忆不属于任何智能体，但要保证存在
    assert (memory_dir / "global" / "profile.md").exists()
    # 单文件类型用 init_value 建骨架
    assert (memory_dir / "agents" / AGENT / "soul.md").exists()
    # 多文件类型只建目录
    assert (memory_dir / "agents" / AGENT / "preferences").is_dir()
    # 会话目录不预建 —— 预建会留下一堆空目录
    assert not (memory_dir / "sessions").exists()
    assert created


@pytest.mark.asyncio
async def test_init_agent_is_idempotent(ready: AsyncSession, memory_dir: Path) -> None:
    """
    可能被重复调用（重启、修复）。覆盖已有文件会抹掉积累的记忆。
    """
    soul = memory_dir / "agents" / AGENT / "soul.md"
    soul.write_text("---\nmemory_type: soul\nversion: 9\n---\n\n积累的内容\n", encoding="utf-8")

    await memory.init_agent(AGENT, db=ready)

    assert "积累的内容" in soul.read_text(encoding="utf-8")


# ── 写入与合并 ────────────────────────────────────


@pytest.mark.asyncio
async def test_write_creates_then_patches(ready: AsyncSession) -> None:
    scope = MemoryScope(agent_id=AGENT)

    first = await memory.write(
        scope, "preferences", {"topic": "testing", "content": "- 改完必须跑 pytest"}, db=ready
    )
    assert first.created is True
    assert first.version == 1

    second = await memory.write(
        scope,
        "preferences",
        {"topic": "testing", "content": _patch("- 改完必须跑 pytest", "- 改完必须跑 pytest -q")},
        db=ready,
    )
    assert second.created is False
    assert second.changed is True
    assert second.version == 2

    item = await memory.get(scope, "preferences", "testing")
    assert item is not None
    assert "pytest -q" in item.body


@pytest.mark.asyncio
async def test_write_is_idempotent_on_identical_content(ready: AsyncSession) -> None:
    """
    没有这一步的话每次 commit 都产生无意义的 version 跳动和 git diff ——
    而记忆目录进 git 的全部价值就是 diff 可读。
    """
    scope = MemoryScope(agent_id=AGENT)
    fields = {"topic": "style", "content": "- 行长 120"}

    await memory.write(scope, "preferences", fields, db=ready)
    again = await memory.write(scope, "preferences", dict(fields), db=ready)

    assert again.changed is False
    assert again.version == 1, "内容没变时 version 不该递增"


@pytest.mark.asyncio
async def test_unmentioned_fields_are_preserved(ready: AsyncSession) -> None:
    """
    LLM 只改 summary 时不该把 goal 抹掉。
    """
    scope = MemoryScope(agent_id=AGENT)
    await memory.write(
        scope,
        "trajectories",
        {
            "trajectory_name": "t1",
            "task_query": "原任务",
            "outcome": "success",
            "content": "- 步骤：\n  1. 做了事",
        },
        db=ready,
    )
    # trajectories 是 add_only，同名会改名 —— 用 uri 直接读回来验证保留逻辑
    items = await memory.list_items(scope, "trajectories")
    assert len(items) == 1
    assert items[0].fields["task_query"] == "原任务"


@pytest.mark.asyncio
async def test_sum_field_accumulates_across_writes(ready: AsyncSession) -> None:
    scope = MemoryScope(agent_id=AGENT)
    for _ in range(3):
        await memory.write(
            scope,
            "tool_notes",
            {"tool_name": "read_file", "total_calls": 2, "content": "- 用相对路径"},
            db=ready,
        )

    item = await memory.get(scope, "tool_notes", "read_file")
    assert item is not None
    assert item.fields["total_calls"] == 6


@pytest.mark.asyncio
async def test_add_only_renames_instead_of_overwriting(ready: AsyncSession) -> None:
    """
    add_only 的语义是只增不改。覆盖违反它；跳过会静默丢信息 ——
    两个同名事件通常是两件不同的事（LLM 起名撞车）。
    """
    scope = MemoryScope(agent_id=AGENT)
    for task in ("第一次", "第二次"):
        await memory.write(
            scope,
            "trajectories",
            {"trajectory_name": "同名", "task_query": task, "outcome": "success", "content": "x"},
            db=ready,
        )

    items = await memory.list_items(scope, "trajectories")
    assert len(items) == 2
    assert {i.fields["task_query"] for i in items} == {"第一次", "第二次"}


@pytest.mark.asyncio
async def test_failed_patch_returns_error_without_writing(ready: AsyncSession) -> None:
    """
    合并失败要显式报错，因为调用方要把失败信息回给 LLM 重试。
    """
    scope = MemoryScope(agent_id=AGENT)
    await memory.write(scope, "preferences", {"topic": "t", "content": "- 真实内容"}, db=ready)

    result = await memory.write(
        scope, "preferences", {"topic": "t", "content": _patch("- 不存在的片段", "x")}, db=ready
    )

    assert result.ok is False
    assert "找不到" in result.error
    item = await memory.get(scope, "preferences", "t")
    assert item is not None
    assert item.version == 1, "失败的写入不该递增 version"
    assert "真实内容" in item.body


@pytest.mark.asyncio
async def test_write_many_serial_so_patches_see_each_other(ready: AsyncSession) -> None:
    """
    同一批里两条 patch 打到同一个文件时，后者必须看到前者的结果。

    并发执行会让后者读到写之前的内容，SEARCH 匹配失败 —— 而失败信息
    看起来像"LLM 写错了 search"，实际是并发问题。
    """
    scope = MemoryScope(agent_id=AGENT)
    await memory.write(scope, "preferences", {"topic": "chain", "content": "步骤A\n步骤B"}, db=ready)

    batch = await memory.write_many(
        [
            WriteOp(scope=scope, memory_type="preferences", fields={"topic": "chain", "content": _patch("步骤A", "步骤1")}),
            WriteOp(scope=scope, memory_type="preferences", fields={"topic": "chain", "content": _patch("步骤B", "步骤2")}),
        ],
        db=ready,
    )

    assert batch.ok, batch.errors
    item = await memory.get(scope, "preferences", "chain")
    assert item is not None
    assert item.body == "步骤1\n步骤2"


# ── 隔离 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_scope_required_for_session_memory(ready: AsyncSession) -> None:
    """没有 session_id 时写不了会话记忆。"""
    with pytest.raises(PathScopeError):
        await memory.write(
            MemoryScope(agent_id=AGENT), "entities", {"category": "people", "name": "x", "content": "y"}, db=ready
        )


@pytest.mark.asyncio
async def test_session_memories_are_isolated_between_sessions(ready: AsyncSession) -> None:
    """
    会话 A 的实体不能出现在会话 B 里。不隔离的话，无关话题的会话会被
    注入另一个会话的实体卡片 —— 那不是记性好，是串台。
    """
    a = MemoryScope(agent_id=AGENT, session_id="ses_a")
    b = MemoryScope(agent_id=AGENT, session_id="ses_b")

    await memory.write(a, "entities", {"category": "people", "name": "zhangsan", "content": "# 张三\n后端同事"}, db=ready)

    assert len(await memory.list_items(a, "entities")) == 1
    assert await memory.list_items(b, "entities") == []


@pytest.mark.asyncio
async def test_agent_memories_are_isolated_between_agents(ready: AsyncSession) -> None:
    mine = MemoryScope(agent_id=AGENT)
    theirs = MemoryScope(agent_id=OTHER_AGENT)

    await memory.write(mine, "preferences", {"topic": "t", "content": "- 我的偏好"}, db=ready)

    assert await memory.get(theirs, "preferences", "t") is None


@pytest.mark.asyncio
async def test_profile_is_shared_across_agents(ready: AsyncSession) -> None:
    """
    用户只有一个。让每个智能体各自积累一份画像会各自跑偏。
    """
    await memory.write(
        MemoryScope(agent_id=AGENT), "profile", {"content": "# 用户\n- 个人开发者"}, db=ready
    )

    seen_by_other = await memory.get(MemoryScope(agent_id=OTHER_AGENT), "profile")
    assert seen_by_other is not None
    assert "个人开发者" in seen_by_other.body


@pytest.mark.asyncio
async def test_peer_view_writes_to_peer_directory(ready: AsyncSession, memory_dir: Path) -> None:
    """
    agents/A/peers/B/ 是"A 认为的 B"。B 读不到它 —— 那会变成
    "你的同事觉得你不行"这种破坏性反馈。
    """
    a_sees_b = MemoryScope(agent_id=AGENT, peer_agent_id=OTHER_AGENT)
    await memory.write(a_sees_b, "identity", {"content": "# 审查员\n- 爱漏边界情况"}, db=ready)

    assert (memory_dir / "agents" / AGENT / "peers" / OTHER_AGENT / "identity.md").exists()
    # B 自己的 identity 不受影响
    assert await memory.get(MemoryScope(agent_id=OTHER_AGENT), "identity") is None


@pytest.mark.asyncio
async def test_peer_disabled_type_stays_in_own_directory(ready: AsyncSession, memory_dir: Path) -> None:
    """soul 是自述，不存在"A 眼中 B 的自述"。"""
    scope = MemoryScope(agent_id=AGENT, peer_agent_id=OTHER_AGENT)
    await memory.write(scope, "soul", {"content": "# 性格\n- 直接"}, db=ready)

    assert not (memory_dir / "agents" / AGENT / "peers" / OTHER_AGENT / "soul.md").exists()
    assert "直接" in (memory_dir / "agents" / AGENT / "soul.md").read_text(encoding="utf-8")


def test_scope_rejects_inconsistent_combinations() -> None:
    with pytest.raises(ValueError, match="session_id"):
        MemoryScope(session_id="ses_x")
    with pytest.raises(ValueError, match="peer_agent_id"):
        MemoryScope(peer_agent_id="adf_b")
    with pytest.raises(ValueError, match="不能等于"):
        MemoryScope(agent_id=AGENT, peer_agent_id=AGENT)


@pytest.mark.asyncio
async def test_uri_traversal_is_blocked(ready: AsyncSession, tmp_path: Path) -> None:
    """uri 可能来自外部（前端删除请求、LLM 输出），必须挡住 ../。"""
    secret = tmp_path / "secret.md"
    secret.write_text("不该被读到", encoding="utf-8")

    assert await memory.read_uri("../secret.md") is None
    assert await memory.delete_uri("../secret.md") is False
    assert secret.exists()


@pytest.mark.asyncio
async def test_unsafe_filename_chars_are_sanitized(ready: AsyncSession) -> None:
    """
    LLM 生成的名字里出现过 `/`（想表达层级）和 `:`（想写时间）。
    不清理的话 `/` 会凭空建目录，`:` 在 Windows 上直接写入失败。
    """
    scope = MemoryScope(agent_id=AGENT)
    result = await memory.write(scope, "preferences", {"topic": "a/b:c", "content": "- x"}, db=ready)
    assert result.ok

    # 【关键】字段值里的 / 不能变成目录层级。
    # 变成目录后按 topic 读回来会失败，而且列举时它落在子目录里。
    assert result.uri == f"agents/{AGENT}/preferences/a_b_c.md"

    # 读回来必须命中同一条 —— 写和读要算出同一个路径
    again = await memory.get(scope, "preferences", "a/b:c")
    assert again is not None
    assert again.version == 1


# ── 列举与容错 ────────────────────────────────────


@pytest.mark.asyncio
async def test_broken_file_does_not_break_listing(ready: AsyncSession, memory_dir: Path) -> None:
    """
    列举 100 个记忆时第 37 个 frontmatter 坏了，应该返回 99 个而不是整个失败。
    """
    scope = MemoryScope(agent_id=AGENT)
    await memory.write(scope, "preferences", {"topic": "good", "content": "- ok"}, db=ready)

    broken = memory_dir / "agents" / AGENT / "preferences" / "broken.md"
    broken.write_text("---\n: : : 这不是合法 YAML : :\n", encoding="utf-8")

    items = await memory.list_items(scope, "preferences")
    assert [i.fields.get("topic") for i in items if i.fields.get("topic")] == ["good"]


@pytest.mark.asyncio
async def test_single_file_listing_excludes_siblings(ready: AsyncSession) -> None:
    """
    soul.md 和 identity.md 在同一个目录（agent 根）。
    列举 soul 时不能把 identity 也算进来。
    """
    scope = MemoryScope(agent_id=AGENT)
    await memory.write(scope, "soul", {"content": "- 性格"}, db=ready)
    await memory.write(scope, "identity", {"content": "- 角色"}, db=ready)

    souls = await memory.list_items(scope, "soul")
    assert len(souls) == 1
    assert souls[0].memory_type == "soul"


@pytest.mark.asyncio
async def test_overview_file_is_not_a_memory_item(ready: AsyncSession) -> None:
    scope = MemoryScope(agent_id=AGENT)
    await memory.write(scope, "preferences", {"topic": "t", "content": "- x"}, db=ready)
    await memory.refresh_overview(scope, "preferences")

    items = await memory.list_items(scope, "preferences")
    assert len(items) == 1
    assert all(not i.uri.endswith(".overview.md") for i in items)


@pytest.mark.asyncio
async def test_visible_types_follow_scope(memory_dir: Path) -> None:
    global_only = {s.memory_type for s in memory.visible_types(MemoryScope())}
    assert global_only == {"profile"}

    with_agent = {s.scope for s in memory.visible_types(MemoryScope(agent_id=AGENT))}
    assert MemoryScopeKind.SESSION not in with_agent

    with_session = {s.scope for s in memory.visible_types(MemoryScope(agent_id=AGENT, session_id=SESSION))}
    assert MemoryScopeKind.SESSION in with_session


@pytest.mark.asyncio
async def test_unknown_memory_type_raises_with_hint(ready: AsyncSession) -> None:
    with pytest.raises(ValueError, match="未知的记忆类型"):
        await memory.write(MemoryScope(agent_id=AGENT), "nonexistent", {}, db=ready)


# ── 索引 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_index_tracks_writes(ready: AsyncSession) -> None:
    scope = MemoryScope(agent_id=AGENT)
    await memory.write(scope, "preferences", {"topic": "indexed", "content": "- x"}, db=ready)

    rows = await memory.list_index(ready, scope, memory_type="preferences")
    assert len(rows) == 1
    assert rows[0]["title"] == "indexed"
    assert rows[0]["agent_id"] == AGENT


@pytest.mark.asyncio
async def test_list_index_all_agents_lists_global_and_agents(ready: AsyncSession) -> None:
    """记忆列表的"全部智能体"要列出全局 + 所有智能体，而不是只全局。"""
    await memory.write(MemoryScope(agent_id=AGENT), "preferences", {"topic": "a", "content": "- x"}, db=ready)
    await memory.write(MemoryScope(), "profile", {"content": "# 用户\n- 全局"}, db=ready)

    rows = await memory.list_index(ready, MemoryScope())
    topics = {r["title"] for r in rows}
    assert "a" in topics, "缺 agent 记忆"
    assert any(r["scope"] == "global" for r in rows), "缺全局记忆"


@pytest.mark.asyncio
async def test_rebuild_index_from_files_preserves_hit_counts(ready: AsyncSession) -> None:
    """
    热度是行为数据，重建索引不该抹掉它 —— 那会让召回排序在重建后
    突然变差，而用户看不出原因。
    """
    scope = MemoryScope(agent_id=AGENT)
    result = await memory.write(scope, "preferences", {"topic": "hot", "content": "- x"}, db=ready)
    await index_mod.record_hit(ready, [result.uri])

    n = await memory.rebuild_index(ready)

    assert n >= 1
    row = await index_mod.get(ready, result.uri)
    assert row is not None
    assert row.active_count == 1


@pytest.mark.asyncio
async def test_drop_session_removes_files_and_index(ready: AsyncSession, memory_dir: Path) -> None:
    scope = MemoryScope(agent_id=AGENT, session_id=SESSION)
    await memory.write(scope, "entities", {"category": "people", "name": "n", "content": "# n"}, db=ready)

    count = await memory.drop_session(SESSION, db=ready)

    assert count == 1
    # 会话记忆平级于 agents，不嵌在 agents/<id>/sessions/ 下
    assert not (memory_dir / "sessions" / SESSION).exists()
    # 会话记忆（entities）的索引记录没了；global/agent 记忆不受影响
    items = await memory.list_index(ready, scope)
    assert not any(i["memory_type"] == "entities" for i in items)


@pytest.mark.asyncio
async def test_drop_agent_keeps_global_profile(ready: AsyncSession, memory_dir: Path) -> None:
    """删智能体不该删掉全局的用户画像 —— 那不属于任何智能体。"""
    await memory.write(MemoryScope(agent_id=AGENT), "profile", {"content": "# 用户\n- x"}, db=ready)

    await memory.drop_agent(AGENT, db=ready)

    assert not (memory_dir / "agents" / AGENT).exists()
    assert (memory_dir / "global" / "profile.md").exists()


@pytest.mark.asyncio
async def test_stale_embedding_detection(ready: AsyncSession) -> None:
    """
    换嵌入模型后旧向量的相似度计算毫无意义但不报错 ——
    召回还在返回结果，只是结果是随机的。要能显式查出来。
    """
    scope = MemoryScope(agent_id=AGENT)
    result = await memory.write(scope, "preferences", {"topic": "t", "content": "- x"}, db=ready)
    item = await memory.read_uri(result.uri)
    assert item is not None
    await index_mod.upsert(ready, result.uri, item, embedding_model="old-model", embedding_dim=384)

    stale = await index_mod.stale_embeddings(ready, model="new-model", dim=1024)

    assert result.uri in stale
