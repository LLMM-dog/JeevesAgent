"""
提取期工具调用（lazy prefetch 模式）。

对齐 OpenViking 的 eager_prefetch=false 路径：只预取轻量索引，
给模型 list/read/search 让它自己按需拉取。

## 为什么必须支持这条路径

记忆多到装不下窗口时，全量预取会挤掉对话内容 —— 而对话才是提取的原料。
那时只能让模型自己决定"我需要看哪几条"。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from app.core.config import settings
from app.modules.agent.messages import Msg
from app.modules.memory import layout, registry
from app.modules.memory import service as memory
from app.modules.memory.commit import commit_session
from app.modules.memory.extract_context import from_messages
from app.modules.memory.extract_loop import ExtractLoop
from app.modules.memory.extract_tools import ToolCall, ToolRunner, tool_schemas
from app.modules.memory.models import MemoryScope
from app.modules.memory.prefetch import prefetch
from sqlalchemy.ext.asyncio import AsyncSession

from tests.seed import seed_session

AGENT = "adf_tools"


@pytest.fixture
def memory_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "memory"
    root.mkdir(parents=True)
    monkeypatch.setattr(type(layout.settings), "memory_dir", property(lambda _s: root))
    registry.reset()
    yield root
    registry.reset()


@pytest_asyncio.fixture
async def ready(memory_dir: Path, db: AsyncSession) -> AsyncIterator[AsyncSession]:
    await memory.init_agent(AGENT, db=db)
    scope = MemoryScope(agent_id=AGENT)
    await memory.write(scope, "preferences",
                       {"topic": "testing", "content": "- 用 `pytest -x` 跑测试\n- 失败就停"}, db=db)
    await memory.write(scope, "preferences",
                       {"topic": "code_style", "content": "- 提交前过 ruff check"}, db=db)
    await memory.write(scope, "experiences", {
        "experience_name": "alembic_sqlite_batch",
        "content": "## Situation\n- SQLite 改列\n\n## Approach\n- 用 batch_alter_table\n\n## Reflect\n- 不要直接 ALTER",
    }, db=db)
    yield db


@pytest_asyncio.fixture
async def runner(ready: AsyncSession) -> ToolRunner:
    pre = await prefetch(MemoryScope(agent_id=AGENT, session_id="ses_t"), eager=False)
    return ToolRunner(scope=MemoryScope(agent_id=AGENT, session_id="ses_t"), pages=pre.pages)


class ToolLLM:
    """
    能返回工具调用的假 LLM。

    脚本里每一项要么是 str（最终 JSON），要么是 list[ToolCall]（要调工具）。
    """

    def __init__(self, *script: Any):
        self._script = list(script)
        self.calls: list[list[dict[str, Any]]] = []
        self.tools_seen: list[Any] = []

    async def __call__(self, messages: list[dict[str, Any]], tools: Any = None) -> Any:
        self.calls.append([dict(m) for m in messages])
        self.tools_seen.append(tools)
        item = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(item, list):
            return ("", item)
        return (item, [])

    @property
    def rounds(self) -> int:
        return len(self.calls)


def _tc(name: str, **args: Any) -> ToolCall:
    return ToolCall(call_id=f"c_{name}", name=name, arguments=json.dumps(args))


async def _loop(llm: Any, runner: ToolRunner, pre: Any, **kw: Any) -> Any:
    return await ExtractLoop(
        llm_call=llm,
        schemas=registry.get_schemas().enabled(),
        prefetched=pre,
        extract_context=from_messages([Msg(role="user", content="改用 pytest -q")], [1786608000000]),
        tool_runner=runner,
        **kw,
    ).run()


# ══════════════════════════════════════════════════
# 工具本身
# ══════════════════════════════════════════════════


def test_tool_schemas_use_page_id_not_uri() -> None:
    """
    参数用 page_id 而非 uri：模型会抄错长路径，而抄错的后果是
    静默读到空内容而不是报错。
    """
    names = {t["function"]["name"] for t in tool_schemas()}
    assert names == {"list_memories", "read_memory", "search_memories"}

    read = next(t for t in tool_schemas() if t["function"]["name"] == "read_memory")
    assert "page_id" in read["function"]["parameters"]["properties"]
    assert "uri" not in read["function"]["parameters"]["properties"]


@pytest.mark.asyncio
async def test_list_memories_returns_titles_and_page_ids(runner: ToolRunner) -> None:
    out = await runner.execute(_tc("list_memories", memory_type="preferences"))

    assert "page_id=" in out
    assert "testing" in out
    assert "code_style" in out
    # 列表不该带正文 —— 那是 read 的职责，列表要轻
    assert "pytest -x" not in out


@pytest.mark.asyncio
async def test_list_unknown_type_lists_available(runner: ToolRunner) -> None:
    """错误信息要告诉模型有哪些可用，否则它只能再猜一次。"""
    out = await runner.execute(_tc("list_memories", memory_type="nonexistent"))
    assert "没有 nonexistent" in out
    assert "preferences" in out


@pytest.mark.asyncio
async def test_read_memory_returns_full_body(runner: ToolRunner) -> None:
    """
    read 要给完整正文 —— 它是 SEARCH 片段的唯一可靠来源。
    """
    listing = await runner.execute(_tc("list_memories", memory_type="preferences"))
    pid = int(listing.split("page_id=")[1].split(" ")[0])

    out = await runner.execute(_tc("read_memory", page_id=pid))

    assert "pytest -x" in out or "ruff" in out
    assert "逐字符一致" in out, "要提醒模型 SEARCH 必须精确"


@pytest.mark.asyncio
async def test_read_marks_uri_as_read(runner: ToolRunner) -> None:
    """
    read 过的记忆要记下来 —— refetch 检查靠它判断模型是否在凭猜测写。
    """
    listing = await runner.execute(_tc("list_memories", memory_type="preferences"))
    pid = int(listing.split("page_id=")[1].split(" ")[0])

    assert runner.read_uris == set()
    await runner.execute(_tc("read_memory", page_id=pid))
    assert len(runner.read_uris) == 1


@pytest.mark.asyncio
async def test_read_invalid_page_id_guides_recovery(runner: ToolRunner) -> None:
    out = await runner.execute(_tc("read_memory", page_id=9999))
    assert "不存在" in out
    assert "list_memories" in out, "要指出怎么拿到正确的 page_id"


@pytest.mark.asyncio
async def test_search_finds_relevant_memory(runner: ToolRunner) -> None:
    """
    搜索是去重的手段：模型想记一件事时先搜，搜到了就改而不是新建。
    """
    out = await runner.execute(_tc("search_memories", query="pytest 测试"))

    assert "page_id=" in out
    assert "testing" in out
    assert "相关度" in out


@pytest.mark.asyncio
async def test_search_no_match_says_it_is_new(runner: ToolRunner) -> None:
    """
    搜不到时要明确说"这件事是新的"，而不是返回空 —— 空结果会让模型
    不确定是搜索坏了还是真的没有。
    """
    out = await runner.execute(_tc("search_memories", query="量子computing区块链"))
    assert "没有匹配" in out
    assert "新的" in out


@pytest.mark.asyncio
async def test_search_scoped_by_memory_type(runner: ToolRunner) -> None:
    out = await runner.execute(_tc("search_memories", query="alembic", memory_type="experiences"))
    assert "alembic_sqlite_batch" in out


@pytest.mark.asyncio
async def test_search_matches_part_of_snake_case_name(runner: ToolRunner) -> None:
    """
    【回归】tokenize 把 `_` 当词内字符（它为代码标识符设计），于是
    `alembic_sqlite_batch` 是一个 token，搜 "alembic" 永远匹配不上。

    而记忆标题【全是 snake_case】（experience_name / tool_name / topic
    都要求小写下划线），不拆的话按名字搜索基本失效。
    """
    for q in ("alembic", "sqlite", "batch"):
        out = await runner.execute(_tc("search_memories", query=q))
        assert "alembic_sqlite_batch" in out, f"搜 {q} 应该能找到"


@pytest.mark.asyncio
async def test_unknown_tool_returns_error_and_is_flagged(runner: ToolRunner) -> None:
    """
    未知工具名要返回错误文本而非抛异常，并被标记 ——
    调用方据此在下一轮关掉工具，防止耗尽迭代预算。
    """
    out = await runner.execute(ToolCall(call_id="x", name="delete_everything", arguments="{}"))

    assert "没有名为 delete_everything 的工具" in out
    assert runner.has_unknown_call is True


@pytest.mark.asyncio
async def test_malformed_arguments_do_not_crash(runner: ToolRunner) -> None:
    """
    参数不是合法 JSON 时返回错误文本，让模型自我纠正。
    抛异常会让整次提取失败。
    """
    out = await runner.execute(ToolCall(call_id="x", name="read_memory", arguments="{不是 JSON"))
    assert "错误" in out


# ══════════════════════════════════════════════════
# lazy 预取
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_lazy_prefetch_gives_index_without_bodies(ready: AsyncSession) -> None:
    """
    lazy 只给标题与 page_id。正文由模型 read 拉取 ——
    这才是"记忆多到装不下窗口"时唯一可行的做法。
    """
    pre = await prefetch(MemoryScope(agent_id=AGENT, session_id="ses_t"), eager=False)
    rendered = pre.render()

    assert pre.eager is False
    assert "page_id=" in rendered
    assert "read_memory" in rendered, "要提示模型怎么拿正文"
    assert "- 用 `pytest -x` 跑测试" not in rendered, "lazy 不该给正文"


@pytest.mark.asyncio
async def test_lazy_prefetch_does_not_mark_uris_as_read(ready: AsyncSession) -> None:
    """
    read_uris 的语义是"模型已看过正文"。lazy 只给了标题，
    填进去会让 refetch 检查失效 —— 模型就能在没读正文的情况下 patch，
    而那必然匹配失败。
    """
    pre = await prefetch(MemoryScope(agent_id=AGENT, session_id="ses_t"), eager=False)
    assert pre.read_uris == set()
    assert pre.total > 0


@pytest.mark.asyncio
async def test_prefetch_labels_the_scope_tier(ready: AsyncSession) -> None:
    """
    【我们自己的架构要求】记忆按 global / agent / session 三层隔离，
    预取渲染必须标出每类属于哪一层。

    不标的后果有两个：
    - 模型不知道 session 级的东西下次会话看不到 → 该记进 agent 级的
      记成了会话级，等于没记
    - 预取只含【本会话】的 session 级记忆，模型不知道这点，
      会把"没看到"当成"从没记过"

    OpenViking 不需要这个（它按 user_space 隔离，一次提取只涉及一个用户）。
    """
    scope = MemoryScope(agent_id=AGENT, session_id="ses_t")
    await memory.write(MemoryScope(), "profile", {"content": "# 用户\n- 画像"}, db=ready)
    await memory.write(
        scope, "entities",
        {"category": "service", "name": "billing", "content": "- 本会话提到的服务"},
        db=ready,
    )

    rendered = (await prefetch(scope)).render()

    assert "跨会话长期有效" in rendered, "agent 级要标明跨会话有效"
    assert "仅本次会话" in rendered, "session 级要标明只在本会话"
    assert "所有智能体共享" in rendered, "global 级要标明共享"


@pytest.mark.asyncio
async def test_prefetch_reads_agent_and_session_tiers_separately(
    ready: AsyncSession,
) -> None:
    """
    会话提取要【同时】拿到 agent 级和 session 级的记忆，各从自己的目录读。

    两者路径不同（agents/X/preferences vs agents/X/sessions/Y/entities），
    漏掉任一层都会让模型看不到已有记忆而新建重复的。
    """
    scope = MemoryScope(agent_id=AGENT, session_id="ses_t")
    await memory.write(
        scope, "entities",
        {"category": "service", "name": "billing", "content": "- 会话级"},
        db=ready,
    )

    pre = await prefetch(scope)

    uris = {i.uri for items in pre.by_type.values() for i in items}
    assert any("/sessions/ses_t/" in u for u in uris), "缺 session 级记忆"
    assert any("/preferences/" in u and "/sessions/" not in u for u in uris), "缺 agent 级记忆"


@pytest.mark.asyncio
async def test_other_sessions_memories_are_not_prefetched(ready: AsyncSession) -> None:
    """会话级记忆按会话隔离 —— 预取绝不能带进别的会话的。"""
    await memory.write(
        MemoryScope(agent_id=AGENT, session_id="ses_other"),
        "entities",
        {"category": "service", "name": "other_only", "content": "- 属于别的会话"},
        db=ready,
    )

    pre = await prefetch(MemoryScope(agent_id=AGENT, session_id="ses_t"))

    uris = {i.uri for items in pre.by_type.values() for i in items}
    assert not any("ses_other" in u for u in uris)


@pytest.mark.asyncio
async def test_eager_prefetch_is_capped_per_type(ready: AsyncSession) -> None:
    """
    【回归】eager 模式原来不限量。实测一个有 120 条偏好的智能体
    （用半年就会有）让预取吃掉 13572 token，把对话内容挤出窗口 ——
    而对话才是提取的原料。

    OpenViking 的 eager 也只读搜索结果的 top-N
    （session_extract_context_provider.py:571），从来不是读全部。
    """
    scope = MemoryScope(agent_id=AGENT)
    for i in range(40):
        await memory.write(
            scope, "preferences", {"topic": f"t{i:03d}", "content": "- " + "x" * 200}, db=ready
        )

    pre = await prefetch(MemoryScope(agent_id=AGENT, session_id="ses_t"), eager=True)

    assert len(pre.by_type["preferences"]) <= settings.memory.prefetch_topn
    assert len(pre.render()) <= settings.memory.prefetch_max_chars


@pytest.mark.asyncio
async def test_prefetch_respects_total_char_budget(ready: AsyncSession) -> None:
    """
    分类型限量之后仍可能超总量（十个类型各 5 条 × 1500 字符 = 75000）。
    总预算是最后一道防线。
    """
    scope = MemoryScope(agent_id=AGENT)
    for i in range(5):
        await memory.write(
            scope, "preferences", {"topic": f"p{i}", "content": "- " + "x" * 1400}, db=ready
        )
        await memory.write(
            scope, "experiences",
            {"experience_name": f"e{i}", "content": "## Situation\n- " + "y" * 1400},
            db=ready,
        )

    pre = await prefetch(
        MemoryScope(agent_id=AGENT, session_id="ses_t"), eager=True, topn=5
    )
    # 人为压低预算，验证裁剪真的生效
    pre.trim_to_budget(3_000)

    assert len(pre.render()) <= 3_000
    assert pre.dropped > 0, "裁剪掉的条数要被记下来，否则无法排查"
    # 不能把某个类型整类丢空 —— 那会让模型把它当"从没记过"而新建重复的
    assert all(items for items in pre.by_type.values())


@pytest.mark.asyncio
async def test_eager_prefetch_includes_bodies(ready: AsyncSession) -> None:
    pre = await prefetch(MemoryScope(agent_id=AGENT, session_id="ses_t"), eager=True)

    assert pre.eager is True
    assert "- 用 `pytest -x` 跑测试" in pre.render()
    assert len(pre.read_uris) == pre.total


@pytest.mark.asyncio
async def test_lazy_prefetch_caps_index_size(ready: AsyncSession) -> None:
    """索引本身也要有上限，否则记忆上千条时索引就把窗口占满了。"""
    pre = await prefetch(MemoryScope(agent_id=AGENT, session_id="ses_t"), eager=False, topn=1)
    assert all(len(v) <= 1 for v in pre.by_type.values())


# ══════════════════════════════════════════════════
# 循环里的工具调用
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_loop_executes_tool_then_produces_result(ready: AsyncSession) -> None:
    """
    完整的 ReAct：先调工具看记忆，再输出最终 JSON。
    """
    pre = await prefetch(MemoryScope(agent_id=AGENT, session_id="ses_t"), eager=False)
    runner = ToolRunner(scope=MemoryScope(agent_id=AGENT, session_id="ses_t"), pages=pre.pages)
    pid = pre.pages.assign(
        next(i.uri for i in pre.by_type["preferences"] if i.fields.get("topic") == "testing")
    )

    llm = ToolLLM(
        [_tc("read_memory", page_id=pid)],
        json.dumps({"reasoning": "读过了，改写它", "preferences": [
            {"page_id": pid, "topic": "testing",
             "content": {"blocks": [{"search": "- 用 `pytest -x` 跑测试", "replace": "- 用 `pytest -q` 跑全量"}]}}
        ]}),
    )

    outcome = await _loop(llm, runner, pre)

    assert [s.kind for s in outcome.steps] == ["tool_call", "ok"]
    assert outcome.total_items == 1
    assert len(outcome.tools_used) == 1
    assert outcome.tools_used[0]["name"] == "read_memory"


@pytest.mark.asyncio
async def test_tool_call_messages_are_properly_paired(ready: AsyncSession) -> None:
    """
    assistant(tool_calls) 后面每个 call 都要有对应的 tool 消息。
    缺一条的话下一轮请求会被上游拒绝（400），而错误只说"格式不对"。
    """
    pre = await prefetch(MemoryScope(agent_id=AGENT, session_id="ses_t"), eager=False)
    runner = ToolRunner(scope=MemoryScope(agent_id=AGENT, session_id="ses_t"), pages=pre.pages)

    llm = ToolLLM(
        [_tc("list_memories", memory_type="preferences"), _tc("list_memories", memory_type="experiences")],
        json.dumps({"reasoning": "r"}),
    )
    await _loop(llm, runner, pre)

    second_round = llm.calls[1]
    assistant = next(m for m in second_round if m.get("role") == "assistant" and m.get("tool_calls"))
    tool_msgs = [m for m in second_round if m.get("role") == "tool"]

    assert len(assistant["tool_calls"]) == 2
    assert len(tool_msgs) == 2
    assert {m["tool_call_id"] for m in tool_msgs} == {c["id"] for c in assistant["tool_calls"]}


@pytest.mark.asyncio
async def test_parallel_tool_calls_all_execute(ready: AsyncSession) -> None:
    pre = await prefetch(MemoryScope(agent_id=AGENT, session_id="ses_t"), eager=False)
    runner = ToolRunner(scope=MemoryScope(agent_id=AGENT, session_id="ses_t"), pages=pre.pages)

    llm = ToolLLM(
        [
            _tc("list_memories", memory_type="preferences"),
            _tc("search_memories", query="alembic"),
            _tc("list_memories", memory_type="experiences"),
        ],
        json.dumps({"reasoning": "r"}),
    )
    outcome = await _loop(llm, runner, pre)

    assert len(outcome.tools_used) == 3
    assert len(runner.calls) == 3


@pytest.mark.asyncio
async def test_tool_call_extends_iteration_budget(ready: AsyncSession) -> None:
    """
    调工具不是"产出结果"的一轮，不该占用正常预算。
    max_iterations=1 时调一次工具后仍应有机会输出结果。
    """
    pre = await prefetch(MemoryScope(agent_id=AGENT, session_id="ses_t"), eager=False)
    runner = ToolRunner(scope=MemoryScope(agent_id=AGENT, session_id="ses_t"), pages=pre.pages)

    llm = ToolLLM(
        [_tc("list_memories", memory_type="preferences")],
        json.dumps({"reasoning": "r", "preferences": [
            {"page_id": None, "topic": "new", "content": {"blocks": [{"search": "", "replace": "- 新"}]}}
        ]}),
    )
    outcome = await _loop(llm, runner, pre, max_iterations=1)

    assert [s.kind for s in outcome.steps] == ["tool_call", "ok"]
    assert outcome.total_items == 1


@pytest.mark.asyncio
async def test_unknown_tool_disables_tools_next_round(ready: AsyncSession) -> None:
    """
    照抄 OpenViking（extract_loop.py:271）：模型调了不存在的工具后，
    下一轮不再给它工具 —— 否则它会持续尝试而耗尽预算。
    """
    pre = await prefetch(MemoryScope(agent_id=AGENT, session_id="ses_t"), eager=False)
    runner = ToolRunner(scope=MemoryScope(agent_id=AGENT, session_id="ses_t"), pages=pre.pages)

    llm = ToolLLM(
        [ToolCall(call_id="x", name="rm_rf", arguments="{}")],
        json.dumps({"reasoning": "r"}),
    )
    await _loop(llm, runner, pre)

    assert llm.tools_seen[0] is not None, "第一轮该给工具"
    assert llm.tools_seen[1] is None, "调了未知工具后必须收回工具"


@pytest.mark.asyncio
async def test_final_iteration_has_no_tools(ready: AsyncSession) -> None:
    """
    最后一轮必须关掉工具，否则模型可能继续探索而永远不产出结果。
    """
    pre = await prefetch(MemoryScope(agent_id=AGENT, session_id="ses_t"), eager=False)
    runner = ToolRunner(scope=MemoryScope(agent_id=AGENT, session_id="ses_t"), pages=pre.pages)

    llm = ToolLLM(json.dumps({"reasoning": "r"}))
    await _loop(llm, runner, pre, max_iterations=1)

    assert llm.tools_seen[0] is None


@pytest.mark.asyncio
async def test_eager_mode_still_provides_tools(ready: AsyncSession) -> None:
    """
    eager 模式仍然提供工具（对齐 OpenViking 的实际实现）。
    即使预取了全文，LLM 也可能需要：
    - 读取预取中被截断的记忆全文
    - 搜索预取范围之外的记忆
    - 读取预取列表中它认为需要的其他记忆
    """
    pre = await prefetch(MemoryScope(agent_id=AGENT, session_id="ses_t"), eager=True)
    runner = ToolRunner(scope=MemoryScope(agent_id=AGENT, session_id="ses_t"), pages=pre.pages)
    llm = ToolLLM(json.dumps({"reasoning": "r"}))

    await ExtractLoop(
        llm_call=llm,
        schemas=registry.get_schemas().enabled(),
        prefetched=pre,
        extract_context=from_messages([Msg(role="user", content="x")], [1]),
        tool_runner=runner,
    ).run()

    # 工具应该可用
    assert llm.tools_seen[0] is not None
    assert len(llm.tools_seen[0]) > 0
    # 提示词应该提到工具
    assert "read_memory" in llm.calls[0][0]["content"]


@pytest.mark.asyncio
async def test_lazy_mode_prompt_documents_tools(ready: AsyncSession) -> None:
    pre = await prefetch(MemoryScope(agent_id=AGENT, session_id="ses_t"), eager=False)
    runner = ToolRunner(scope=MemoryScope(agent_id=AGENT, session_id="ses_t"), pages=pre.pages)

    llm = ToolLLM(json.dumps({"reasoning": "r"}))
    await _loop(llm, runner, pre)

    system = llm.calls[0][0]["content"]
    assert "read_memory" in system
    assert "必须先 read" in system


@pytest.mark.asyncio
async def test_commit_uses_tools_when_lazy(
    ready: AsyncSession, workspace_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    端到端：配置成 lazy 时，commit 要建 ToolRunner 并让模型能调工具。
    """
    monkeypatch.setattr(layout.settings.memory, "eager_prefetch", False)
    sid = await seed_session(ready, "ses_accumulated", workspace_id=workspace_id, agent_id=AGENT)

    seen: dict[str, Any] = {}

    class Recorder(ToolLLM):
        async def __call__(self, messages: Any, tools: Any = None) -> Any:
            seen.setdefault("first_tools", tools)
            return await super().__call__(messages, tools)

    llm = Recorder(
        [_tc("list_memories", memory_type="preferences")],
        json.dumps({"reasoning": "r", "preferences": [
            {"page_id": None, "topic": "brand_new", "content": {"blocks": [{"search": "", "replace": "- 新偏好"}]}}
        ]}),
    )

    report = await commit_session(ready, session_id=sid, agent_id=AGENT, llm_call=llm)

    assert report.ok, report.warnings
    assert seen["first_tools"] is not None, "lazy 模式必须给工具"
    assert report.outcome is not None
    assert [s.kind for s in report.outcome.steps] == ["tool_call", "ok"]
    assert len(report.outcome.tools_used) == 1
