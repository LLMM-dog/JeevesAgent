"""
向量化与语义搜索。

## 三个必须验证的点

1. **只增类型也向量化** —— events / trajectories 预取时跳过，
   但向量化不能跳过（两件事目的不同）
2. **搜索范围按三层隔离** —— 会话级记忆对其他会话不可见
3. **换嵌入模型后旧向量停止参与召回**，且能一键重算

嵌入模型用假实现：真实模型的向量不确定，测不出"维度不一致时跳过"
这类断言。真实模型的验证走 scripts/verify_memory.py --embedding。
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from app.core.exceptions import ProviderError
from app.infra.llm.embedding import EmbeddingResult, cosine
from app.infra.llm.port import ResolvedModel
from app.modules.memory import layout, registry
from app.modules.memory import service as memory
from app.modules.memory import vectorize as vec
from app.modules.memory.models import MemoryScope
from app.modules.memory.models_db import MemoryIndex
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

AGENT = "adf_vec"
OTHER_AGENT = "adf_other"


class FakeEmbedder:
    """
    确定性假嵌入：按关键词命中生成向量。

    这样"含 pytest 的文本"和"查询 pytest"会有高相似度，
    而无关文本相似度低 —— 让语义搜索的断言可复现。
    """

    KEYS = ("pytest", "ruff", "dataclass", "billing", "alembic", "frozen")

    def __init__(self, *, model: str = "fake-embed", dim: int = 0, fail: bool = False):
        self.model = model
        self.dim = dim or len(self.KEYS) + 1
        self.fail = fail
        self.calls: list[list[str]] = []

    async def __call__(self, model: ResolvedModel, texts: list[str]) -> EmbeddingResult:
        self.calls.append(list(texts))
        if self.fail:
            raise ProviderError("假的嵌入失败", code="embedding_http_error")

        vectors: list[list[float]] = []
        for text in texts:
            low = text.lower()
            v = [1.0 if k in low else 0.0 for k in self.KEYS]
            # 补一维常数，保证零向量也有模长（否则余弦全是 0，测不出排序）
            v.append(0.1)
            # 补到目标维度
            v.extend([0.0] * (self.dim - len(v)))
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            vectors.append([x / norm for x in v[: self.dim]])
        return EmbeddingResult(vectors=vectors, model=self.model, dim=self.dim)


@pytest.fixture
def memory_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "memory"
    root.mkdir(parents=True)
    monkeypatch.setattr(type(layout.settings), "memory_dir", property(lambda _s: root))
    registry.reset()
    yield root
    registry.reset()


@pytest.fixture
def embedder(monkeypatch: pytest.MonkeyPatch) -> FakeEmbedder:
    """替掉真实嵌入调用，并让 resolve_embedding_model 返回一个假模型。"""
    fake = FakeEmbedder()
    monkeypatch.setattr(vec, "embed_texts", fake)

    async def resolve(_db: Any) -> ResolvedModel:
        return ResolvedModel(
            model_id=fake.model, base_url="http://fake", api_key="k", purpose="embedding"
        )

    monkeypatch.setattr(memory, "resolve_embedding_model", resolve)
    return fake


@pytest_asyncio.fixture
async def ready(memory_dir: Path, db: AsyncSession) -> AsyncIterator[AsyncSession]:
    await memory.init_agent(AGENT, db=db)
    yield db


async def _row(db: AsyncSession, uri: str) -> MemoryIndex | None:
    return (
        await db.execute(select(MemoryIndex).where(MemoryIndex.uri == uri))
    ).scalars().one_or_none()


# ══════════════════════════════════════════════════
# 打包与相似度
# ══════════════════════════════════════════════════


def test_pack_roundtrip_uses_float32() -> None:
    """
    float32 BLOB 而非 JSON：1024 维存 JSON 约 12KB，存 BLOB 是 4KB。
    """
    v = [0.1, -0.25, 0.5, 0.0]
    blob = vec.pack(v)

    assert len(blob) == len(v) * 4
    restored = vec.unpack(blob)
    assert len(restored) == len(v)
    assert all(abs(a - b) < 1e-6 for a, b in zip(v, restored, strict=True))


def test_unpack_tolerates_corrupt_blob() -> None:
    """
    数据损坏时返回空列表而非报错 —— 一条记忆的向量坏掉
    不该让整次召回失败。
    """
    assert vec.unpack(b"abc") == []  # 长度不是 4 的倍数
    assert vec.unpack(None) == []
    assert vec.unpack(b"") == []


def test_cosine_returns_zero_for_mismatched_dims() -> None:
    """
    维度不等意味着来自不同模型，任何比较都无意义。
    返回 0 让它排到末尾，而不是报错。
    """
    assert cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0
    assert cosine([], [1.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0  # 零向量
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_every_declared_embedding_template_is_actually_used() -> None:
    """
    【元测试】schema 声明的 embedding_template 必须真的被消费。

    ## 为什么需要这条

    十个 YAML 都声明了 embedding_template，而 render_embedding_text
    有零个调用点 —— 全是死配置，持续了整个开发过程没被发现。

    原因是我的测试只断言"我实现了的东西"，没断言"声明了的东西都被实现"。
    这条测试补的正是那个缺口：它检查渲染函数能对每个类型产出非空文本，
    而 vectorize 里有对它的真实调用。
    """
    import inspect

    from app.modules.memory import render

    # 1. 渲染函数存在且被 vectorize 调用
    source = inspect.getsource(vec)
    assert "render_embedding_text" in source, "向量化必须走 embedding_template"

    # 2. 每个声明了模板的类型都能渲染出非空文本
    for schema in registry.get_schemas().enabled():
        if not schema.embedding_template:
            continue
        fields = {f.name: f"值_{f.name}" for f in schema.fields}
        item = _fake_item(schema.memory_type, fields)
        text = render.render_embedding_text(schema, item)
        assert text.strip(), f"{schema.memory_type} 的 embedding_template 渲染成了空串"


def _fake_item(memory_type: str, fields: dict[str, Any]) -> Any:
    from app.modules.memory.models import MemoryItem
    from app.modules.memory.schema import MemoryScopeKind

    return MemoryItem(
        uri=f"x/{memory_type}.md",
        memory_type=memory_type,
        scope=MemoryScopeKind.AGENT,
        fields=fields,
        body="正文",
        raw_content="正文",
    )


def test_overview_files_are_not_vectorized() -> None:
    """
    overview 是派生索引（其他记忆的标题拼接）。向量化它会让它与
    被它索引的记忆竞争相似度，而命中一个目录索引对召回毫无价值。
    """
    assert vec.should_vectorize("agents/a/preferences/testing.md") is True
    assert vec.should_vectorize("agents/a/preferences/.overview.md") is False
    assert vec.should_vectorize("agents/a/.abstract.md") is False


# ══════════════════════════════════════════════════
# 写入时向量化
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_write_then_vectorize_stores_vector(
    ready: AsyncSession, embedder: FakeEmbedder
) -> None:
    scope = MemoryScope(agent_id=AGENT)
    result = await memory.write(
        scope, "preferences", {"topic": "testing", "content": "- 用 pytest -q"}, db=ready
    )

    report = await memory.vectorize(ready, [result.uri])

    assert report.succeeded == 1
    assert report.model == "fake-embed"
    row = await _row(ready, result.uri)
    assert row is not None
    assert row.embedding is not None
    assert row.embedding_model == "fake-embed"
    assert row.embedding_dim == report.dim
    # embedded_hash 记的是"向量算的是哪一版内容"
    assert row.embedded_hash == row.content_hash


@pytest.mark.asyncio
async def test_add_only_types_are_vectorized(
    ready: AsyncSession, embedder: FakeEmbedder
) -> None:
    """
    【核心断言】events / trajectories 是 add_only，预取时跳过它们，
    但向量化【不能】跳过。

    两件事目的不同：预取是为了让模型改已有记忆而不是新建重复的，
    向量化是为了以后能召回。混淆这两件事会让只增类型永远无法被语义召回。

    OpenViking 同样对全部 written + edited 向量化，只排除
    overview / abstract（memory_updater.py:1352）。
    """
    scope = MemoryScope(agent_id=AGENT)
    traj = await memory.write(scope, "trajectories", {
        "trajectory_name": "alembic_batch_fix",
        "task_query": "修 alembic 迁移",
        "outcome": "success",
        "retrieval_anchor": "场景：SQLite 改列",
        "content": "- 步骤：\n  1. 用 batch_alter_table",
    }, db=ready)

    report = await memory.vectorize(ready, [traj.uri])

    assert report.succeeded == 1, "只增类型必须向量化，否则永远无法被语义召回"
    row = await _row(ready, traj.uri)
    assert row is not None and row.embedding is not None


@pytest.mark.asyncio
async def test_embedding_uses_the_template_not_raw_body(
    ready: AsyncSession, embedder: FakeEmbedder
) -> None:
    """
    向量化要走 embedding_template。trajectories 的模板只用
    retrieval_anchor 而不用 content —— 那是"检索文本与执行文本分开"
    这条设计的意义所在。
    """
    scope = MemoryScope(agent_id=AGENT)
    traj = await memory.write(scope, "trajectories", {
        "trajectory_name": "t",
        "task_query": "任务描述不该进向量",
        "outcome": "success",
        "retrieval_anchor": "场景：这句该进向量",
        "content": "- 步骤：\n  1. 这句也不该进",
    }, db=ready)

    await memory.vectorize(ready, [traj.uri])

    embedded_text = embedder.calls[-1][0]
    assert "这句该进向量" in embedded_text
    assert "任务描述不该进向量" not in embedded_text


@pytest.mark.asyncio
async def test_vectorize_skips_overview_files(
    ready: AsyncSession, embedder: FakeEmbedder
) -> None:
    scope = MemoryScope(agent_id=AGENT)
    await memory.write(scope, "preferences", {"topic": "t", "content": "- x"}, db=ready)
    await memory.refresh_overview(scope, "preferences")

    report = await memory.vectorize(
        ready, ["agents/adf_vec/preferences/.overview.md", "agents/adf_vec/preferences/t.md"]
    )

    assert report.attempted == 1
    assert report.skipped == 1


@pytest.mark.asyncio
async def test_no_embedding_model_skips_silently(
    ready: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    嵌入模型是可选的。没配时向量召回关闭、回落关键词，
    系统仍然完全可用 —— 不该抛异常。
    """
    async def no_model(_db: Any) -> None:
        return None

    monkeypatch.setattr(memory, "resolve_embedding_model", no_model)
    result = await memory.write(
        MemoryScope(agent_id=AGENT), "preferences", {"topic": "t", "content": "- x"}, db=ready
    )

    report = await memory.vectorize(ready, [result.uri])

    assert report.ok, "没配模型不是错误"
    assert report.succeeded == 0
    assert report.skipped == 1


@pytest.mark.asyncio
async def test_embedding_failure_does_not_lose_the_memory(
    ready: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    嵌入服务失败时记忆【已经写进文件了】，缺的只是向量。
    必须报告错误但不能让写入回滚 —— 下次 revectorize 能补上。
    """
    fake = FakeEmbedder(fail=True)
    monkeypatch.setattr(vec, "embed_texts", fake)

    async def resolve(_db: Any) -> ResolvedModel:
        return ResolvedModel(model_id="m", base_url="u", api_key="k", purpose="embedding")

    monkeypatch.setattr(memory, "resolve_embedding_model", resolve)

    result = await memory.write(
        MemoryScope(agent_id=AGENT), "preferences", {"topic": "t", "content": "- x"}, db=ready
    )
    report = await memory.vectorize(ready, [result.uri])

    assert not report.ok
    assert report.succeeded == 0
    # 记忆文件仍然存在
    assert await memory.read_uri(result.uri) is not None


# ══════════════════════════════════════════════════
# 搜索范围：三层隔离
# ══════════════════════════════════════════════════


def test_visible_scopes_follows_three_tier_isolation() -> None:
    """
    【与 OpenViking 的关键差异】它按 user_space 拼路径（多租户），
    我们按 global / agent / session 三层筛。

    会话级记忆对其他会话不可见 —— 否则 A 会话的临时上下文会污染 B。

    session 记忆的 agent_id 恒为空：会话记忆属于会话本身（只被第一个
    智能体代表会话修改），不按智能体隔离。
    """
    session = vec.visible_scopes(MemoryScope(agent_id="a1", session_id="s1"))
    assert session == [
        ("global", "", "", ""),
        ("agent", "a1", "", ""),
        ("session", "", "s1", ""),
    ]

    agent_only = vec.visible_scopes(MemoryScope(agent_id="a1"))
    assert agent_only == [("global", "", "", ""), ("agent", "a1", "", "")]
    assert not any(s[0] == "session" for s in agent_only), "agent 查询不该看到任何会话记忆"

    global_only = vec.visible_scopes(MemoryScope())
    assert global_only == [("global", "", "", "")]


def test_peer_view_is_isolated_from_the_agents_own_memory() -> None:
    """
    `agents/A/peers/B/` 是「A 眼中的 B」，与 A 自己的记忆是两套东西。

    不按 peer_agent_id 筛的话两个方向都错：普通查询会把「A 眼中的 B」
    混进 A 自己的记忆，peer 视角查询会拿到 A 自己的记忆。

    peer 目前不会被创建，但筛选条件要先正确 —— 等它被用起来时
    这类污染极难发现，因为结果"看起来合理"。
    """
    own = vec.visible_scopes(MemoryScope(agent_id="a1"))
    peer = vec.visible_scopes(MemoryScope(agent_id="a1", peer_agent_id="b1"))

    assert ("agent", "a1", "", "") in own
    assert ("agent", "a1", "", "b1") in peer
    # 两者互不包含 —— 这正是隔离的含义
    assert ("agent", "a1", "", "b1") not in own
    assert ("agent", "a1", "", "") not in peer


@pytest.mark.asyncio
async def test_search_finds_semantically_related_memory(
    ready: AsyncSession, embedder: FakeEmbedder
) -> None:
    scope = MemoryScope(agent_id=AGENT)
    a = await memory.write(scope, "preferences", {"topic": "testing", "content": "- 用 pytest 跑测试"}, db=ready)
    b = await memory.write(scope, "preferences", {"topic": "style", "content": "- 用 ruff 检查"}, db=ready)
    await memory.vectorize(ready, [a.uri, b.uri])

    hits = await memory.search_semantic(ready, scope, "pytest 怎么跑")

    assert hits
    assert hits[0].uri == a.uri, "最相关的该排第一"
    assert hits[0].score > 0


@pytest.mark.asyncio
async def test_session_memory_is_invisible_to_other_sessions(
    ready: AsyncSession, embedder: FakeEmbedder
) -> None:
    """
    【核心隔离断言】A 会话的记忆不能出现在 B 会话的搜索结果里。
    """
    scope_a = MemoryScope(agent_id=AGENT, session_id="ses_A")
    ev = await memory.write(scope_a, "events", {
        "event_name": "billing_owner",
        "goal": "确认负责人",
        "summary": "小明接手 billing 服务发布。",
        "outcome": "success",
        "ranges": "0",
    }, db=ready, extract_context=_ctx())
    await memory.vectorize(ready, [ev.uri])

    from_a = await memory.search_semantic(ready, scope_a, "billing")
    from_b = await memory.search_semantic(
        ready, MemoryScope(agent_id=AGENT, session_id="ses_B"), "billing"
    )

    assert any(h.uri == ev.uri for h in from_a), "本会话应该能搜到"
    assert not any(h.uri == ev.uri for h in from_b), "其他会话绝不能搜到"


@pytest.mark.asyncio
async def test_agent_memory_is_invisible_to_other_agents(
    ready: AsyncSession, embedder: FakeEmbedder
) -> None:
    await memory.init_agent(OTHER_AGENT, db=ready)
    mine = await memory.write(
        MemoryScope(agent_id=AGENT), "preferences", {"topic": "t", "content": "- pytest"}, db=ready
    )
    await memory.vectorize(ready, [mine.uri])

    hits = await memory.search_semantic(ready, MemoryScope(agent_id=OTHER_AGENT), "pytest")

    assert not any(h.uri == mine.uri for h in hits)


@pytest.mark.asyncio
async def test_global_memory_is_visible_from_every_scope(
    ready: AsyncSession, embedder: FakeEmbedder
) -> None:
    """全局记忆（profile）对所有智能体和会话可见。"""
    g = await memory.write(MemoryScope(), "profile", {"content": "# 用户\n- 用 pytest"}, db=ready)
    await memory.vectorize(ready, [g.uri])

    for scope in (
        MemoryScope(),
        MemoryScope(agent_id=AGENT),
        MemoryScope(agent_id=AGENT, session_id="ses_X"),
        MemoryScope(agent_id=OTHER_AGENT),
    ):
        hits = await memory.search_semantic(ready, scope, "pytest")
        assert any(h.uri == g.uri for h in hits), f"{scope} 应该能看到全局记忆"


@pytest.mark.asyncio
async def test_search_can_be_scoped_to_one_memory_type(
    ready: AsyncSession, embedder: FakeEmbedder
) -> None:
    scope = MemoryScope(agent_id=AGENT)
    p = await memory.write(scope, "preferences", {"topic": "t", "content": "- pytest"}, db=ready)
    e = await memory.write(scope, "experiences", {
        "experience_name": "pytest_tip", "content": "## Situation\n- pytest 挂住"
    }, db=ready)
    await memory.vectorize(ready, [p.uri, e.uri])

    hits = await memory.search_semantic(ready, scope, "pytest", memory_type="experiences")

    assert [h.uri for h in hits] == [e.uri]


@pytest.mark.asyncio
async def test_search_without_model_returns_empty(
    ready: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_model(_db: Any) -> None:
        return None

    monkeypatch.setattr(memory, "resolve_embedding_model", no_model)
    assert await memory.search_semantic(ready, MemoryScope(agent_id=AGENT), "x") == []


# ══════════════════════════════════════════════════
# 换模型与一键重算
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_stale_vectors_stop_participating_in_search(
    ready: AsyncSession, embedder: FakeEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    【换模型的核心行为】旧向量立即停止参与召回，而不是参与一个无意义的比较。

    维度相同但模型不同的向量之间算余弦会得到"看起来合理"的数值，
    而那个数值毫无意义 —— 这正是最难发现的一类 bug：
    召回还在返回结果，只是结果没有意义。
    """
    scope = MemoryScope(agent_id=AGENT)
    p = await memory.write(scope, "preferences", {"topic": "t", "content": "- pytest"}, db=ready)
    await memory.vectorize(ready, [p.uri])
    assert await memory.search_semantic(ready, scope, "pytest")

    # 用户换了嵌入模型
    other = FakeEmbedder(model="another-embed")
    monkeypatch.setattr(vec, "embed_texts", other)

    async def resolve(_db: Any) -> ResolvedModel:
        return ResolvedModel(
            model_id="another-embed", base_url="u", api_key="k", purpose="embedding"
        )

    monkeypatch.setattr(memory, "resolve_embedding_model", resolve)

    assert await memory.search_semantic(ready, scope, "pytest") == [], (
        "换模型后旧向量必须停止参与召回"
    )


@pytest.mark.asyncio
async def test_vector_status_classifies_staleness(
    ready: AsyncSession, embedder: FakeEmbedder
) -> None:
    """
    三种失效原因分开报告，因为用户的处理方式不同。
    """
    scope = MemoryScope(agent_id=AGENT)
    fresh = await memory.write(scope, "preferences", {"topic": "a", "content": "- x"}, db=ready)
    await memory.vectorize(ready, [fresh.uri])
    # 没算过向量的
    await memory.write(scope, "preferences", {"topic": "b", "content": "- y"}, db=ready)

    stats = await memory.vector_status(ready)

    assert stats["fresh"] >= 1
    assert stats["never"] >= 1


@pytest.mark.asyncio
async def test_content_change_marks_vector_stale(
    ready: AsyncSession, embedder: FakeEmbedder
) -> None:
    """
    记忆改过但向量没重算 → embedded_hash != content_hash。
    那时召回用的是旧语义，必须能被发现。
    """
    scope = MemoryScope(agent_id=AGENT)
    p = await memory.write(scope, "preferences", {"topic": "t", "content": "- 旧内容"}, db=ready)
    await memory.vectorize(ready, [p.uri])

    await memory.write(
        scope, "preferences",
        {"topic": "t", "content": {"blocks": [{"search": "- 旧内容", "replace": "- 新内容"}]}},
        db=ready,
    )

    stats = await memory.vector_status(ready)
    assert stats["content"] >= 1, "内容变了但向量没重算，该被标为失效"


@pytest.mark.asyncio
async def test_revectorize_only_stale_by_default(
    ready: AsyncSession, embedder: FakeEmbedder
) -> None:
    """
    只算失效的。全量重算可能是几千次 API 调用 ——
    默认行为不该烧那个钱。
    """
    scope = MemoryScope(agent_id=AGENT)
    # 先把【所有】已存在的记忆算好（init_agent 建的骨架也算），
    # 否则"只算失效的"会把骨架一起算进来，断言数字就对不上了。
    await memory.revectorize(ready, only_stale=False)
    already_fresh = (await memory.vector_status(ready))["fresh"]

    await memory.write(scope, "preferences", {"topic": "b", "content": "- y"}, db=ready)
    report = await memory.revectorize(ready, only_stale=True)

    assert report.succeeded == 1, "只该算那条新写的，已新鲜的不重复算"
    assert (await memory.vector_status(ready))["fresh"] == already_fresh + 1


@pytest.mark.asyncio
async def test_revectorize_all_recomputes_everything(
    ready: AsyncSession, embedder: FakeEmbedder
) -> None:
    """only_stale=False 用于"我怀疑向量算错了"。"""
    scope = MemoryScope(agent_id=AGENT)
    a = await memory.write(scope, "preferences", {"topic": "a", "content": "- x"}, db=ready)
    b = await memory.write(scope, "preferences", {"topic": "b", "content": "- y"}, db=ready)
    await memory.vectorize(ready, [a.uri, b.uri])

    report = await memory.revectorize(ready, only_stale=False)

    # 全量重算 = 所有可向量化的行（含 init_agent 建的骨架），不只是这两条。
    # 断言"重算后没有任何行是失效的"比断言一个具体数字更能表达意图。
    after = await memory.vector_status(ready)
    assert report.succeeded >= 2
    assert after["never"] == 0
    assert after["content"] == 0
    assert after["model"] == 0


@pytest.mark.asyncio
async def test_revectorize_after_model_switch_restores_search(
    ready: AsyncSession, embedder: FakeEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    一键重算后召回恢复。这是整条"可切换嵌入模型"链路的终点。
    """
    scope = MemoryScope(agent_id=AGENT)
    p = await memory.write(scope, "preferences", {"topic": "t", "content": "- pytest"}, db=ready)
    await memory.vectorize(ready, [p.uri])

    other = FakeEmbedder(model="new-embed")
    monkeypatch.setattr(vec, "embed_texts", other)

    async def resolve(_db: Any) -> ResolvedModel:
        return ResolvedModel(model_id="new-embed", base_url="u", api_key="k", purpose="embedding")

    monkeypatch.setattr(memory, "resolve_embedding_model", resolve)
    assert await memory.search_semantic(ready, scope, "pytest") == []

    report = await memory.revectorize(ready)

    assert report.succeeded >= 1
    assert report.model == "new-embed"
    hits = await memory.search_semantic(ready, scope, "pytest")
    assert any(h.uri == p.uri for h in hits), "重算后召回必须恢复"


@pytest.mark.asyncio
async def test_clear_vectors_falls_back_cleanly(
    ready: AsyncSession, embedder: FakeEmbedder
) -> None:
    """
    清空让召回干净地回落关键词，而不是留一批永远不参与比较的死数据。
    """
    scope = MemoryScope(agent_id=AGENT)
    p = await memory.write(scope, "preferences", {"topic": "t", "content": "- pytest"}, db=ready)
    await memory.vectorize(ready, [p.uri])

    cleared = await memory.clear_vectors(ready)

    assert cleared >= 1
    row = await _row(ready, p.uri)
    assert row is not None
    assert row.embedding is None
    assert row.embedding_model == ""
    assert await memory.search_semantic(ready, scope, "pytest") == []
    # 记忆文件本身不受影响
    assert await memory.read_uri(p.uri) is not None


def _ctx() -> Any:
    from app.modules.agent.messages import Msg
    from app.modules.memory.extract_context import from_messages

    return from_messages([Msg(role="user", content="小明接手 billing")], [1786608000000])
