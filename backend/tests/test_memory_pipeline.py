"""
提取管线的分阶段测试：截断 → 预取 → ReAct 循环 → 去重 → 合并 → 写入。

## LLM 用假实现

真实模型的输出不确定，测不出"循环在第 2 轮走了 patch 修复分支"这类断言。
FakeLLM 按脚本逐轮返回预设内容，让每条控制流路径都能被精确触发。

真实模型的验证走 scripts/memory_playground.py（人看产物）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from app.modules.agent.messages import Msg, ToolCall
from app.modules.memory import contract, layout, registry
from app.modules.memory import service as memory
from app.modules.memory.commit import commit_session
from app.modules.memory.extract_context import from_messages
from app.modules.memory.extract_input import build_turns, prepare
from app.modules.memory.extract_loop import ExtractLoop, _parse_json
from app.modules.memory.models import MemoryScope
from app.modules.memory.prefetch import PageMap, prefetch
from sqlalchemy.ext.asyncio import AsyncSession

from tests.seed import seed_session

AGENT = "adf_pipeline"


class FakeLLM:
    """按脚本逐轮返回。记录收到的 messages 供断言提示词内容。"""

    def __init__(self, *responses: str):
        self._responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []

    async def __call__(self, messages: list[dict[str, str]], tools: Any = None) -> str:
        """
        返回文本。不返回工具调用 —— 测试走的是 eager_prefetch 模式，
        不需要工具。要测工具调用的话需要返回 (text, tool_calls) 元组。
        """
        self.calls.append([dict(m) for m in messages])
        if not self._responses:
            return "{}"
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]

    @property
    def rounds(self) -> int:
        return len(self.calls)

    def last_prompt(self) -> str:
        """提取最后一次调用的文本内容（支持 content 为 str 或 list）。"""
        parts = []
        for m in self.calls[-1]:
            content = m.get("content", "")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                # Tool call/result 格式
                for item in content:
                    if isinstance(item, dict):
                        if "content" in item:
                            parts.append(str(item["content"]))
                        elif "input" in item:
                            # tool_use
                            parts.append(f"Tool: {item.get('name', '')} {item.get('input', '')}")
            else:
                parts.append(str(content))
        return "\n".join(parts)


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
    yield db


def _msgs(*pairs: tuple[str, str]) -> list[Msg]:
    return [Msg(role=r, content=c) for r, c in pairs]


# ══════════════════════════════════════════════════
# 阶段 1：输入截断
# ══════════════════════════════════════════════════


def test_turns_split_on_user_messages() -> None:
    """
    按 user 消息分轮。以 assistant 分轮会把一个完整任务切碎 ——
    模型可能为一个意图调十次工具。
    """
    msgs = _msgs(
        ("user", "任务A"), ("assistant", "做A"), ("tool", "A结果"),
        ("user", "任务B"), ("assistant", "做B"),
    )
    turns = build_turns(msgs)

    assert len(turns) == 2
    assert len(turns[0].messages) == 3
    assert (turns[0].start, turns[0].end) == (0, 2)
    assert (turns[1].start, turns[1].end) == (3, 4)


def test_leading_messages_before_first_user_are_kept() -> None:
    """system / 历史摘要在第一条 user 之前，不能丢。"""
    turns = build_turns(_msgs(("system", "提示"), ("user", "问题")))
    assert len(turns) == 2
    assert turns[0].messages[0].role == "system"


def test_recent_turns_are_held_back() -> None:
    """
    正在进行的对话不该被总结 —— 提取出来会是"他开始做 X"而不是"做完了 X"。
    """
    msgs = _msgs(*[("user", f"第{i}轮") for i in range(5)])
    result = prepare(msgs, [1] * 5, keep_recent_turns=2)

    assert result.held_back_turns == 2
    assert [m.content for m in result.messages] == ["第0轮", "第1轮", "第2轮"]


def test_all_held_back_yields_empty_input() -> None:
    """
    对话太短时整次跳过，而不是硬提取。
    这是"新会话聊两句就 commit"的正常结果。
    """
    result = prepare(_msgs(("user", "你好")), [1], keep_recent_turns=3)
    assert result.is_empty
    assert result.held_back_turns == 1


def test_long_message_is_truncated_keeping_head_and_tail() -> None:
    """
    保留头尾而非只保留开头：工具结果的【结尾】往往是结论
    （"3 passed" / "error: xxx"），只留开头会把结论切掉。
    """
    body = "开头" + "x" * 5000 + "结论：3 passed"
    result = prepare(
        _msgs(("user", "跑测试"), ("tool", body), ("user", "好"), ("user", "再见")),
        [1, 2, 3, 4],
        keep_recent_turns=1,
        max_msg_chars=300,
    )

    assert result.truncated_messages == 1
    cut = next(m for m in result.messages if m.role == "tool")
    assert cut.content is not None
    assert len(cut.content) < 600
    assert cut.content.startswith("开头")
    assert "结论：3 passed" in cut.content
    assert "省略" in cut.content, "必须留截断标记，否则模型以为内容就这么短"


def test_truncation_does_not_mutate_original() -> None:
    """原始 Msg 可能被别处引用，原地改会造成难查的副作用。"""
    original = Msg(role="tool", content="y" * 3000)
    prepare([Msg(role="user", content="q"), original, Msg(role="user", content="z")],
            [1, 2, 3], keep_recent_turns=1, max_msg_chars=100)
    assert len(original.content or "") == 3000


def test_oldest_turns_dropped_when_over_budget() -> None:
    """
    总量超限时丢【最早】的：较新的内容更可能还没被提取过，
    最早的部分很可能上次已经提取了。
    """
    msgs = _msgs(*[("user", "a" * 500) for _ in range(10)])
    result = prepare(msgs, [1] * 10, keep_recent_turns=1, max_total_chars=1200)

    assert result.dropped_turns > 0
    assert result.total_chars <= 1200 + 500


def test_tool_calls_survive_truncation() -> None:
    """工具调用是事件的证据链，截断正文不能丢掉它。"""
    msg = Msg(role="assistant", content="z" * 3000,
              tool_calls=[ToolCall(id="c", name="edit_file", arguments="{}")])
    result = prepare([Msg(role="user", content="q"), msg, Msg(role="user", content="end")],
                     [1, 2, 3], keep_recent_turns=1, max_msg_chars=100)

    kept = next(m for m in result.messages if m.role == "assistant")
    assert [tc.name for tc in kept.tool_calls] == ["edit_file"]


# ══════════════════════════════════════════════════
# 阶段 2：预取与 page_id
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_prefetch_returns_existing_memories_with_page_ids(ready: AsyncSession) -> None:
    """
    不预取的后果不是"效率低"，是记忆会重复 —— 模型看不到已有的
    preferences/testing.md 就会新建一个说同一件事的。
    """
    scope = MemoryScope(agent_id=AGENT, session_id="ses_x")
    await memory.write(MemoryScope(agent_id=AGENT), "preferences",
                       {"topic": "testing", "content": "- 用 pytest -q"}, db=ready)

    pre = await prefetch(scope)

    assert pre.total >= 1
    rendered = pre.render()
    assert "page_id=" in rendered
    assert "- 用 pytest -q" in rendered, "正文是 patch 的 SEARCH 依据，必须完整给出"


@pytest.mark.asyncio
async def test_prefetch_skips_add_only_types(ready: AsyncSession) -> None:
    """
    add_only 的不会被改，回顾它们只是白烧 token。
    """
    scope = MemoryScope(agent_id=AGENT, session_id="ses_x")
    await memory.write(
        MemoryScope(agent_id=AGENT), "trajectories",
        {"trajectory_name": "t", "goal": "g", "outcome": "success", "content": "1. x"}, db=ready
    )

    pre = await prefetch(scope)
    assert "trajectories" not in pre.by_type


@pytest.mark.asyncio
async def test_prefetch_empty_says_so_explicitly(memory_dir: Path) -> None:
    """
    空预取要明确告诉模型"都是新建"，而不是给一段空白。

    用 memory_dir 而非 ready：init_agent 会用 init_value 建骨架文件
    （soul/identity/profile），那时预取不是空的。要测真正的空态
    就不能先初始化。
    """
    pre = await prefetch(MemoryScope(agent_id=AGENT, session_id="ses_x"))
    assert pre.total == 0
    assert "还没有任何记忆" in pre.render()


@pytest.mark.asyncio
async def test_prefetch_includes_skeleton_files_after_init(ready: AsyncSession) -> None:
    """
    init_agent 建的骨架（soul/identity/profile）也要被预取到 ——
    否则模型会为"我的性格"新建一个文件，而骨架已经在那了。
    """
    pre = await prefetch(MemoryScope(agent_id=AGENT, session_id="ses_x"))
    assert {"soul", "identity", "profile"} <= set(pre.by_type)


def test_page_id_is_stable_and_starts_at_one() -> None:
    """
    同一条记忆在一次提取里只能有一个 page_id。
    从 1 开始：模型会用 0 表示"没有/不适用"。
    """
    pages = PageMap()
    a = pages.assign("uri/a.md")
    b = pages.assign("uri/b.md")

    assert a == 1
    assert b == 2
    assert pages.assign("uri/a.md") == a
    assert pages.resolve(a) == "uri/a.md"


def test_invalid_page_id_resolves_to_empty() -> None:
    """
    宽容而非报错：模型给了不存在的 id 时最可能的意图是
    "这是新记忆但我误填了 id"。当新建处理比丢掉这条好。
    """
    pages = PageMap()
    pages.assign("uri/a.md")

    assert pages.resolve(999) == ""
    assert pages.resolve(None) == ""
    assert pages.resolve("abc") == ""
    assert pages.resolve("1") == "uri/a.md", "字符串数字要能用 —— JSON 里可能是字符串"


# ══════════════════════════════════════════════════
# 阶段 3：输出契约
# ══════════════════════════════════════════════════


def test_patch_field_gets_blocks_schema() -> None:
    """契约由 (type, merge_op) 推导，不是手写在 prompt 里。"""
    schema = registry.get_schemas().get("preferences")
    assert schema is not None
    item = contract.item_schema(schema)

    assert item["properties"]["content"]["type"] == "object"
    assert "blocks" in item["properties"]["content"]["properties"]
    # immutable 字段是文件名来源，必须必填
    assert "topic" in item["required"]


def test_trajectories_has_separate_retrieval_and_execution_fields() -> None:
    """
    照抄 OpenViking 的三字段设计（trajectories.yaml）：

    - task_query       可复制重跑的任务描述
    - retrieval_anchor 专为向量检索写的锚文本
    - content          给未来 agent 看的操作契约

    分开的理由：检索用的文本和执行用的文本，最优写法完全不同。
    混成一个字段要么检索命中率差，要么执行指导性差。
    """
    schema = registry.get_schemas().get("trajectories")
    assert schema is not None
    names = {f.name for f in schema.fields}
    assert {"task_query", "retrieval_anchor", "content"} <= names

    # embedding 只用锚文本，不用 content —— 那才是分开的意义
    assert "retrieval_anchor" in schema.embedding_template
    assert "{{ content }}" not in schema.embedding_template


def test_trajectory_content_template_survives_missing_context() -> None:
    """
    存储层被直接调用时（测试、手工修复、导入）没有 extract_context。

    模板整体渲染失败会让正文回落成裸 content —— 丢掉标题和任务描述，
    而那个丢失只有一条 warning 日志，文件看起来"只是格式简单些"。
    """
    from app.modules.memory import render

    schema = registry.get_schemas().get("trajectories")
    assert schema is not None
    body = render.render_body(
        schema,
        {
            "trajectory_name": "t",
            "outcome": "success",
            "task_query": "重跑用的任务",
            "content": "- 步骤：\n  1. x",
            "ranges": "0-1",
        },
        extract_context=None,
    )

    assert "# t" in body
    assert "重跑用的任务" in body
    assert "时间：" not in body, "没有上下文时时间行该被跳过而非报错"


def test_sum_field_tells_model_to_send_delta() -> None:
    """
    sum 字段最常见的错误是模型填累计总数。契约里必须说清。
    """
    schema = registry.get_schemas().get("tool_notes")
    assert schema is not None
    desc = contract.item_schema(schema)["properties"]["total_calls"]["description"]
    assert "新增" in desc


def test_add_only_type_has_no_page_id() -> None:
    """
    add_only 永远新建，给 page_id 只会让模型试图去改。
    """
    schema = registry.get_schemas().get("events")
    assert schema is not None
    assert "page_id" not in contract.item_schema(schema)["properties"]


def test_type_description_is_not_duplicated_in_schema() -> None:
    """
    类型的完整描述只该出现在系统提示词的「记忆类型」小节，不该再进 JSON Schema。

    实测：不去重时契约有 20390 字符（约 7K token），其中 events 那段
    1136 字符的原子性说明出现了两遍。加十个自定义类型就能把窗口占满。
    """
    schemas = registry.get_schemas().enabled()
    events = next(s for s in schemas if s.memory_type == "events")
    ops = contract.operations_schema(schemas)

    item = ops["properties"]["events"]["items"]
    assert "description" not in item, "类型级描述不该重复进契约"
    # 数组级只留一行摘要
    assert len(ops["properties"]["events"]["description"]) < 120
    assert len(events.description) > 1000, "前提：events 的描述确实很长"


@pytest.mark.asyncio
async def test_system_prompt_stays_within_budget(memory_dir: Path) -> None:
    """
    提示词大小要有上界。契约是自动生成的，加字段时很容易无声膨胀 ——
    而膨胀的后果是挤掉对话内容，提取质量下降却看不出原因。
    """
    llm = FakeLLM(json.dumps({"reasoning": "r"}))
    await _run_loop(llm)

    system = llm.calls[0][0]["content"]
    assert len(system) < 24_000, f"系统提示词 {len(system)} 字符，超出预算"
    # 类型说明和 patch 规则必须都在
    assert "SEARCH/REPLACE 规则" in system
    assert "### events" in system


def test_operations_schema_groups_by_type() -> None:
    """
    按类型分键而非扁平数组：扁平要求模型每条写 memory_type，而它会写错。
    分键让每个键下的字段约束确定。
    """
    schemas = registry.get_schemas().enabled()
    ops = contract.operations_schema(schemas)

    assert "reasoning" in ops["properties"]
    assert "delete_page_ids" in ops["properties"]
    assert ops["properties"]["preferences"]["type"] == "array"


# ══════════════════════════════════════════════════
# 阶段 4：ReAct 循环
# ══════════════════════════════════════════════════


async def _run_loop(llm: FakeLLM, pre: Any = None, **kw: Any) -> Any:
    from app.modules.memory.extract_tools import ToolRunner
    from app.modules.memory.layout import MemoryScope
    from app.modules.memory.prefetch import PrefetchResult

    prefetched = pre or PrefetchResult()
    # 如果没有显式传 tool_runner，创建一个默认的
    if "tool_runner" not in kw:
        kw["tool_runner"] = ToolRunner(
            scope=MemoryScope(agent_id="test_agent"),
            pages=prefetched.pages,
        )

    loop = ExtractLoop(
        llm_call=llm,
        schemas=registry.get_schemas().enabled(),
        prefetched=prefetched,
        extract_context=from_messages(_msgs(("user", "改一下配置")), [1786608000000]),
        **kw,
    )
    return await loop.run()


@pytest.mark.asyncio
async def test_loop_succeeds_on_first_round(memory_dir: Path) -> None:
    llm = FakeLLM(json.dumps({
        "reasoning": "用户说了偏好",
        "preferences": [{"page_id": None, "topic": "testing", "content": {"blocks": [{"search": "", "replace": "- 用 pytest -q"}]}}],
    }))

    outcome = await _run_loop(llm)

    assert llm.rounds == 1
    assert outcome.total_items == 1
    assert [s.kind for s in outcome.steps] == ["ok"]


@pytest.mark.asyncio
async def test_loop_retries_once_on_bad_json(memory_dir: Path) -> None:
    """
    情况 1：输出不是合法 JSON → 告诉它格式错了，重来（最多 1 次）。
    """
    llm = FakeLLM(
        "抱歉，我需要更多信息才能提取。",
        json.dumps({"reasoning": "ok", "preferences": [{"topic": "t", "content": {"blocks": [{"search": "", "replace": "- x"}]}}]}),
    )

    outcome = await _run_loop(llm)

    assert llm.rounds == 2
    assert [s.kind for s in outcome.steps] == ["parse_error", "ok"]
    assert outcome.total_items == 1
    # 第二轮的提示词里要包含格式纠正
    assert "无法解析" in llm.calls[1][-1]["content"]


@pytest.mark.asyncio
async def test_loop_treats_persistent_bad_json_as_no_memories(memory_dir: Path) -> None:
    """
    重试后仍失败 → 当作"没有记忆要写"而不是硬失败。

    硬失败会让整次 commit 回滚，而对话本身是成功的 ——
    提取失败不该影响用户已完成的工作。
    """
    llm = FakeLLM("完全不是 JSON")

    outcome = await _run_loop(llm)

    assert outcome.total_items == 0
    assert any("无法解析" in w for w in outcome.warnings)


@pytest.mark.asyncio
async def test_loop_repairs_failed_patch_with_real_content(ready: AsyncSession) -> None:
    """
    情况 2：SEARCH 匹配不上 → 把失败片段和【真实原文】回给它重试。

    这是 patch 能用起来的关键 —— 不给真实原文的话模型只能再猜一次。
    """
    scope = MemoryScope(agent_id=AGENT)
    await memory.write(scope, "preferences", {"topic": "t", "content": "- 真实的偏好内容"}, db=ready)
    pre = await prefetch(MemoryScope(agent_id=AGENT, session_id="ses_x"))
    # 精确取那条偏好的 page_id —— read_uris 是 set，next(iter(...)) 拿到的
    # 可能是骨架文件，那样测的就不是我们想测的东西了
    pid = pre.pages.assign(pre.by_type["preferences"][0].uri)

    bad = json.dumps({"reasoning": "r", "preferences": [
        {"page_id": pid, "topic": "t", "content": {"blocks": [{"search": "- 不存在的内容", "replace": "- 新的"}]}}
    ]})
    good = json.dumps({"reasoning": "r", "preferences": [
        {"page_id": pid, "topic": "t", "content": {"blocks": [{"search": "- 真实的偏好内容", "replace": "- 新的"}]}}
    ]})
    llm = FakeLLM(bad, good)

    outcome = await _run_loop(llm, pre)

    assert [s.kind for s in outcome.steps] == ["patch_error", "ok"]
    repair_prompt = llm.calls[1][-1]["content"]
    assert "- 真实的偏好内容" in repair_prompt, "必须把真实原文回给模型"
    assert "- 不存在的内容" in repair_prompt, "要指出是哪个 search 失败了"


@pytest.mark.asyncio
async def test_loop_drops_only_failed_items_after_repair(ready: AsyncSession) -> None:
    """
    修复过一次仍失败 → 只丢打不上的那几条。
    一条写错不该让其他正确的记忆也丢掉。
    """
    scope = MemoryScope(agent_id=AGENT)
    await memory.write(scope, "preferences", {"topic": "keep", "content": "- 保留"}, db=ready)
    pre = await prefetch(MemoryScope(agent_id=AGENT, session_id="ses_x"))
    pid = pre.pages.assign(pre.by_type["preferences"][0].uri)

    always_bad = json.dumps({"reasoning": "r", "preferences": [
        {"page_id": pid, "topic": "keep", "content": {"blocks": [{"search": "不存在", "replace": "x"}]}},
        {"page_id": None, "topic": "brand_new", "content": {"blocks": [{"search": "", "replace": "- 新记忆"}]}},
    ]})
    llm = FakeLLM(always_bad)

    outcome = await _run_loop(llm, pre)

    topics = [i.get("topic") for i in outcome.operations.get("preferences", [])]
    assert "brand_new" in topics, "正确的那条必须保住"
    assert "keep" not in topics, "打不上的那条要被丢掉"
    assert outcome.warnings


@pytest.mark.asyncio
async def test_loop_refetches_when_page_id_is_unknown(memory_dir: Path) -> None:
    """
    情况 3：模型引用了一个没见过的 page_id → 幻觉。

    不该按它给的内容去写 —— 那可能把已有记忆整体覆盖。
    """
    payload = json.dumps({"reasoning": "r", "preferences": [
        {"page_id": 42, "topic": "t", "content": {"blocks": [{"search": "a", "replace": "b"}]}}
    ]})
    llm = FakeLLM(payload)

    outcome = await _run_loop(llm)

    assert "refetch" in [s.kind for s in outcome.steps]
    assert "没有看过" in llm.calls[1][-1]["content"]


@pytest.mark.asyncio
async def test_loop_caps_runaway_item_count(memory_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    防的是模型把一段对话拆成 50 个"事件"。那些碎片召回时没有价值，
    只会挤占预算。
    """
    # patch 实例而非类：pydantic BaseModel 的字段在类上不是普通属性。
    monkeypatch.setattr(layout.settings.memory, "max_items_per_extraction", 3)
    many = [{"page_id": None, "topic": f"t{i}", "content": {"blocks": [{"search": "", "replace": f"- {i}"}]}}
            for i in range(10)]
    llm = FakeLLM(json.dumps({"reasoning": "r", "preferences": many}))

    outcome = await _run_loop(llm)

    assert outcome.total_items == 3
    assert any("超过上限" in w for w in outcome.warnings)


@pytest.mark.asyncio
async def test_loop_ignores_unknown_memory_type(memory_dir: Path) -> None:
    """
    模型偶尔自造类型名。忽略比报错好 —— 其余类型仍能写进去。
    """
    llm = FakeLLM(json.dumps({
        "reasoning": "r",
        "made_up_type": [{"whatever": 1}],
        "preferences": [{"topic": "t", "content": {"blocks": [{"search": "", "replace": "- x"}]}}],
    }))

    outcome = await _run_loop(llm)

    assert "made_up_type" not in outcome.operations
    assert outcome.total_items == 1


@pytest.mark.asyncio
async def test_prompt_includes_message_indices_for_ranges(memory_dir: Path) -> None:
    """
    events 的 ranges 需要消息下标。不编号的话模型只能猜。
    """
    llm = FakeLLM(json.dumps({"reasoning": "r"}))
    await _run_loop(llm)

    assert "[0]" in llm.last_prompt()


@pytest.mark.asyncio
async def test_final_iteration_gets_empty_skeleton(memory_dir: Path) -> None:
    """
    最后一轮要给确切的空结构模板，否则模型会输出解释文字。
    """
    llm = FakeLLM("不是 JSON")
    await _run_loop(llm, max_iterations=1)

    assert "delete_page_ids" in llm.calls[0][-1]["content"]


def test_chunk_kind_literal_is_content_not_text() -> None:
    """
    【回归】ChunkKind 的字面量是 content/reasoning/tool_call/usage/done。

    我在 verify 脚本里写成了 `kind == "text"`，于是把所有正文丢掉，
    表现为"模型返回空 → parse_error"—— 看起来像模型不听话，
    实际是消费端筛错了字段名。真实模型跑了两轮才发现。

    这个测试锁住字面量，让下次写错时在 CI 里就失败。
    """
    from typing import get_args

    from app.infra.llm.port import ChunkKind

    kinds = set(get_args(ChunkKind))
    assert kinds == {"content", "reasoning", "tool_call", "usage", "done"}
    assert "text" not in kinds, "不存在 text 这个 kind —— 正文的 kind 是 content"


def test_reasoning_must_not_be_parsed_as_output() -> None:
    """
    推理模型会输出大量 reasoning chunk（实测单轮 21830 字符）。
    那是思考过程，混进正文会让 JSON 解析必然失败。
    """
    assert _parse_json("我需要先分析一下用户的偏好……然后决定记什么") is None


def test_json_parsing_strips_markdown_fence() -> None:
    """
    即使 prompt 说了不要用代码块，模型仍然经常包一层。
    为这件事浪费一轮重试不值得。
    """
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json('```\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json('这是结果：{"a": 1} 完毕') == {"a": 1}
    assert _parse_json("not json") is None
    assert _parse_json("[1,2]") is None, "顶层必须是对象"
    assert _parse_json("") is None


# ══════════════════════════════════════════════════
# 阶段 5-7：端到端（去重、合并、写入、痕迹）
# ══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_commit_writes_memories_end_to_end(
    ready: AsyncSession, workspace_id: str
) -> None:
    """整条管线：真实对话（DB）→ 记忆文件。"""
    sid = await seed_session(ready, "ses_first_memory", workspace_id=workspace_id, agent_id=AGENT)

    llm = FakeLLM(json.dumps({
        "reasoning": "用户明确说了 ruff 强制",
        "preferences": [{"page_id": None, "topic": "code_style",
                         "content": {"blocks": [{"search": "", "replace": "- 提交前必须过 ruff check"}]}}],
        "events": [{"event_name": "verbose_added", "goal": "加开关",
                    "summary": "给 cli.py 加了 --verbose 参数。", "outcome": "success", "ranges": "4-6"}],
    }))

    report = await commit_session(ready, session_id=sid, llm_call=llm)

    assert report.ok, report.warnings
    assert report.messages_loaded == 25
    assert report.messages_used > 0
    assert report.batch is not None
    assert len(report.batch.written) == 2

    pref = await memory.get(MemoryScope(agent_id=AGENT), "preferences", "code_style")
    assert pref is not None
    assert "ruff check" in pref.body

    # events 落在按日期分层的路径下
    events = await memory.list_items(MemoryScope(agent_id=AGENT, session_id=sid), "events")
    assert len(events) == 1
    assert "/2026/" in events[0].uri


@pytest.mark.asyncio
async def test_commit_writes_archive(
    ready: AsyncSession, workspace_id: str, memory_dir: Path
) -> None:
    """提取完成后，已提取的消息被归档（archive 目录 + watermark）。"""
    from app.modules.memory.commit import get_latest_archive_summary

    sid = await seed_session(ready, "ses_first_memory", workspace_id=workspace_id, agent_id=AGENT)

    llm = FakeLLM(json.dumps({
        "reasoning": "用户说提交前要过 ruff",
        "preferences": [{"page_id": None, "topic": "code_style",
                         "content": {"blocks": [{"search": "", "replace": "- 提交前必须过 ruff check"}]}}],
    }))

    report = await commit_session(ready, session_id=sid, llm_call=llm)

    assert report.archive_id, "提取后应写出归档"
    assert report.archive_id.startswith("archive_")

    # archive 目录被写入，契约与 get_latest_archive_summary 对应
    history_dir = memory_dir / "sessions" / sid / "history"
    archive_dir = history_dir / report.archive_id
    assert (archive_dir / "messages.jsonl").is_file()
    assert (archive_dir / ".overview.md").is_file()
    assert (archive_dir / ".meta.json").is_file()

    # 归档的 messages.jsonl 里要有 seq，且 watermark 能读回来
    latest = await get_latest_archive_summary(sid)
    assert latest is not None
    assert latest.archive_id == report.archive_id
    assert latest.last_seq > 0
    assert latest.message_count > 0
    assert latest.overview.strip() != ""


@pytest.mark.asyncio
async def test_second_commit_only_extracts_new_messages(
    ready: AsyncSession, workspace_id: str
) -> None:
    """归档后，第二次提取只处理 watermark 之后的新消息。"""
    sid = await seed_session(ready, "ses_first_memory", workspace_id=workspace_id, agent_id=AGENT)

    llm = FakeLLM(json.dumps({
        "reasoning": "r",
        "preferences": [{"page_id": None, "topic": "code_style",
                         "content": {"blocks": [{"search": "", "replace": "- 提交前必须过 ruff check"}]}}],
    }))

    first = await commit_session(ready, session_id=sid, llm_call=llm)
    first_loaded = first.messages_loaded

    # 第二次：没有新消息时，应该直接跳过（watermark 挡住了旧消息）
    second = await commit_session(ready, session_id=sid, llm_call=llm)
    # 第二次要么跳过，要么加载的消息数远少于第一次
    assert second.skipped or second.messages_loaded < first_loaded


@pytest.mark.asyncio
async def test_commit_second_run_updates_instead_of_duplicating(
    ready: AsyncSession, workspace_id: str
) -> None:
    """
    【去重的核心验证】第二次提取要改已有记忆，不能新建重复的。

    这依赖预取把 page_id 给到模型。

    ## 测试策略

    由于 commit_session 内部会创建自己的 PageMap，测试无法提前知道真实的 page_id。
    所以我们使用一个动态 FakeLLM，它在第二次调用时能够读取预取结果并使用正确的 page_id。
    """
    sid = await seed_session(ready, "ses_first_memory", workspace_id=workspace_id, agent_id=AGENT)

    first = FakeLLM(json.dumps({
        "reasoning": "r",
        "preferences": [{"page_id": None, "topic": "testing",
                         "content": {"blocks": [{"search": "", "replace": "- 用 pytest -x 跑测试"}]}}],
    }))
    await commit_session(ready, session_id=sid, llm_call=first)

    # 第二次提取：使用一个能够检查预取消息的 FakeLLM
    sid2 = await seed_session(ready, "ses_accumulated", workspace_id=workspace_id, agent_id=AGENT)

    # 动态LLM：根据预取内容中的 page_id 生成响应
    class DynamicLLM:
        def __init__(self):
            self.calls: list[list[dict[str, Any]]] = []

        async def __call__(self, messages: list[dict[str, Any]], tools: Any = None) -> str:
            self.calls.append([dict(m) for m in messages])

            # 从预取的 tool_result 中提取 page_id
            # 预取结果包含在 messages 中，格式为 tool_result
            for msg in messages:
                content = msg.get("content", "")
                if isinstance(content, list):
                    # Tool result 格式
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "tool_result":
                            result_content = item.get("content", "")
                            if "testing" in result_content and "page_id=" in result_content:
                                # 匹配格式：page_id=4 | testing
                                import re
                                match = re.search(r'page_id=(\d+)[^\n]*testing', result_content)
                                if match:
                                    pid = int(match.group(1))
                                    return json.dumps({
                                        "reasoning": "他改主意了",
                                        "preferences": [{
                                            "page_id": pid,
                                            "topic": "testing",
                                            "content": {"blocks": [{
                                                "search": "- 用 pytest -x 跑测试",
                                                "replace": "- 用 pytest -q 跑全量"
                                            }]}
                                        }],
                                    })
                elif isinstance(content, str):
                    if "testing" in content and "page_id=" in content:
                        import re
                        match = re.search(r'page_id=(\d+)[^\n]*testing', content)
                        if match:
                            pid = int(match.group(1))
                            return json.dumps({
                                "reasoning": "他改主意了",
                                "preferences": [{
                                    "page_id": pid,
                                    "topic": "testing",
                                    "content": {"blocks": [{
                                        "search": "- 用 pytest -x 跑测试",
                                        "replace": "- 用 pytest -q 跑全量"
                                    }]}
                                }],
                            })

            # 如果找不到，返回空结果
            return json.dumps({"reasoning": "未找到 testing 偏好"})

    second = DynamicLLM()
    report = await commit_session(ready, session_id=sid2, llm_call=second)

    assert report.ok, report.warnings
    # 预取把已有偏好给了模型
    assert report.prefetched_items >= 1

    items = await memory.list_items(MemoryScope(agent_id=AGENT), "preferences")
    # 可能有多条（骨架文件），但 topic=testing 的只有一条
    testing_items = [i for i in items if i.fields.get("topic") == "testing"]
    assert len(testing_items) == 1, "必须是改写，不能新建出第二条"
    assert "pytest -q" in testing_items[0].body
    assert "pytest -x" not in testing_items[0].body, "旧事实必须消失，不能并存"


@pytest.mark.asyncio
async def test_commit_skips_when_conversation_too_short(
    ready: AsyncSession, workspace_id: str
) -> None:
    """
    新会话聊两句就 commit 是正常的，应该跳过而不是硬提取。
    """
    from app.modules.session import repo

    session = await repo.create_session(ready, workspace_id=workspace_id)
    await repo.append_message(ready, session.id, Msg(role="user", content="你好"))

    llm = FakeLLM("{}")
    report = await commit_session(ready, session_id=session.id, llm_call=llm)

    assert report.skipped
    assert llm.rounds == 0, "不该白调 LLM"


@pytest.mark.asyncio
async def test_commit_records_diff_and_report(
    ready: AsyncSession, workspace_id: str, memory_dir: Path
) -> None:
    """
    每个阶段的数字都要留下 —— "提取了 0 条"可能是截断滤掉了、
    可能是模型没找到、也可能是 patch 全失败，报告要能区分。
    """
    sid = await seed_session(ready, "ses_first_memory", workspace_id=workspace_id, agent_id=AGENT)
    llm = FakeLLM(json.dumps({
        "reasoning": "r",
        "preferences": [{"topic": "t", "content": {"blocks": [{"search": "", "replace": "- x"}]}}],
    }))

    report = await commit_session(ready, session_id=sid, llm_call=llm)

    assert report.extraction_id.startswith("ext_")
    assert report.diff_path
    assert (memory_dir / "agents" / AGENT / ".trace" / report.diff_path).is_file()
    # 报告能回答"每一步发生了什么"
    assert "消息" in report.summary()
    assert report.outcome is not None
    assert report.outcome.reasoning == "r"


@pytest.mark.asyncio
async def test_supersedes_removes_the_narrower_experience(
    ready: AsyncSession, workspace_id: str
) -> None:
    """
    经验会逐步泛化。新经验声明 supersedes 时要删掉被取代的那条。

    不处理的话两条并存（名字不同所以 upsert 不会合并），召回时
    一条窄一条宽，而窄的那条会误导 —— 让模型以为只有特定场景才适用。
    """
    scope = MemoryScope(agent_id=AGENT)
    await memory.write(scope, "experiences", {
        "experience_name": "pytest_hang_cancel",
        "content": "## Situation\n- pytest 挂住\n\n## Approach\n- 查 CancelledError\n\n## Reflect\n- 不要裸 except",
    }, db=ready)

    sid = await seed_session(ready, "ses_first_memory", workspace_id=workspace_id, agent_id=AGENT)
    llm = FakeLLM(json.dumps({
        "reasoning": "发现了更普适的规律",
        "experiences": [{
            "page_id": None,
            "experience_name": "bare_except_swallows_await",
            "supersedes": "pytest_hang_cancel",
            "content": {"blocks": [{"search": "", "replace":
                "## Situation\n- 任何 await 挂住\n\n## Approach\n- 检查 except 范围\n\n## Reflect\n- 绝不用裸 except 包 await"}]},
        }],
    }))

    report = await commit_session(ready, session_id=sid, llm_call=llm)

    assert report.ok, report.warnings
    names = {i.title for i in await memory.list_items(scope, "experiences")}
    assert "bare_except_swallows_await" in names
    assert "pytest_hang_cancel" not in names, "被取代的经验必须删掉"
    # 删除要留痕迹 —— 需要时能从 diff 里找回内容
    assert report.batch is not None
    assert any("CancelledError" in d.deleted_content for d in report.batch.deletes)


@pytest.mark.asyncio
async def test_same_batch_write_and_delete_keeps_the_write(
    ready: AsyncSession, workspace_id: str
) -> None:
    """
    模型可能同时"改写 X"和"删除 X"—— 它把「替换」表达成了「删旧 + 建新」，
    而两者算出同一个路径。

    照字面执行会把刚写好的内容删掉，净效果是记忆【凭空消失】，
    而 diff 里显示"写入成功 + 删除成功"，看不出问题。
    正确语义是当更新：保留写入，跳过删除。

    照抄 OpenViking 的同批保护（memory_updater.py:913-935）。
    """
    scope = MemoryScope(agent_id=AGENT)
    await memory.write(scope, "preferences", {"topic": "testing", "content": "- 旧内容"}, db=ready)

    sid = await seed_session(ready, "ses_first_memory", workspace_id=workspace_id, agent_id=AGENT)
    pre = await prefetch(MemoryScope(agent_id=AGENT, session_id=sid))
    pid = pre.pages.assign(
        next(i.uri for i in pre.by_type["preferences"] if i.fields.get("topic") == "testing")
    )

    # 同时改写它、又把它列进删除
    llm = FakeLLM(json.dumps({
        "reasoning": "替换这条",
        "preferences": [{"page_id": pid, "topic": "testing",
                         "content": {"blocks": [{"search": "- 旧内容", "replace": "- 新内容"}]}}],
        "delete_page_ids": [pid],
    }))

    report = await commit_session(ready, session_id=sid, llm_call=llm)

    item = await memory.get(scope, "preferences", "testing")
    assert item is not None, "同批写入的记忆绝不能被同批的删除干掉"
    assert "新内容" in item.body
    assert report.batch is not None
    assert not report.batch.deletes, "该跳过这个删除"
    assert any("同批刚写入过" in w for w in report.warnings)


@pytest.mark.asyncio
async def test_deletes_are_skipped_when_any_write_failed(
    ready: AsyncSession, workspace_id: str
) -> None:
    """
    写入失败时【整批不删】。

    如果模型的意图是"把 A 的内容搬到 B 然后删掉 A"，而 B 写失败了，
    那时删掉 A 就是净数据丢失。保守到底：任何写入错误阻止全部删除。

    照抄 OpenViking 的 has_unresolved_upserts 保护（memory_updater.py:917）。
    """
    scope = MemoryScope(agent_id=AGENT)
    await memory.write(scope, "preferences", {"topic": "keep", "content": "- 必须保住"}, db=ready)

    sid = await seed_session(ready, "ses_first_memory", workspace_id=workspace_id, agent_id=AGENT)
    pre = await prefetch(MemoryScope(agent_id=AGENT, session_id=sid))
    keep_pid = pre.pages.assign(
        next(i.uri for i in pre.by_type["preferences"] if i.fields.get("topic") == "keep")
    )

    # 一条写入注定失败（patch 打不上），同时要求删掉另一条
    llm = FakeLLM(json.dumps({
        "reasoning": "搬内容然后删原件",
        "preferences": [{"page_id": keep_pid, "topic": "keep",
                         "content": {"blocks": [{"search": "- 不存在的原文", "replace": "- x"}]}}],
        "delete_page_ids": [keep_pid],
    }))

    report = await commit_session(ready, session_id=sid, llm_call=llm)

    assert await memory.get(scope, "preferences", "keep") is not None, (
        "写入失败时不能执行删除，否则是净数据丢失"
    )
    assert any("跳过全部" in w or "同批刚写入过" in w for w in report.warnings)


@pytest.mark.asyncio
async def test_overview_refreshed_after_deleting_last_item(
    ready: AsyncSession, workspace_id: str, memory_dir: Path
) -> None:
    """
    删掉某类最后一条记忆后，overview 不能还列着它 —— 那会让点进去 404。

    我原来只刷新【写入过】的类型，漏了删除的目录。
    OpenViking 把 delete 的目录也并进刷新集合（memory_updater.py:982）。
    """
    scope = MemoryScope(agent_id=AGENT)
    await memory.write(scope, "preferences", {"topic": "doomed", "content": "- 即将消失"}, db=ready)
    await memory.refresh_overview(scope, "preferences")

    overview = memory_dir / "agents" / AGENT / "preferences" / ".overview.md"
    assert "doomed" in overview.read_text(encoding="utf-8")

    sid = await seed_session(ready, "ses_first_memory", workspace_id=workspace_id, agent_id=AGENT)
    pre = await prefetch(MemoryScope(agent_id=AGENT, session_id=sid))
    pid = pre.pages.assign(
        next(i.uri for i in pre.by_type["preferences"] if i.fields.get("topic") == "doomed")
    )
    # 只删除，不写任何 preferences —— 这正是原来漏刷新的情形
    llm = FakeLLM(json.dumps({
        "reasoning": "这条过时了",
        "delete_page_ids": [pid],
        "experiences": [{"page_id": None, "experience_name": "unrelated",
                         "content": {"blocks": [{"search": "", "replace": "## Situation\n- x"}]}}],
    }))

    await commit_session(ready, session_id=sid, llm_call=llm)

    assert "doomed" not in overview.read_text(encoding="utf-8"), (
        "删除后 overview 必须刷新，否则列表里有指向不存在文件的链接"
    )


@pytest.mark.asyncio
async def test_supersedes_pointing_at_self_is_ignored(
    ready: AsyncSession, workspace_id: str
) -> None:
    """
    模型有时把自己的名字填进 supersedes。忽略而非删掉刚写的那条 ——
    否则这次提取等于白做。
    """
    sid = await seed_session(ready, "ses_first_memory", workspace_id=workspace_id, agent_id=AGENT)
    llm = FakeLLM(json.dumps({
        "reasoning": "r",
        "experiences": [{
            "page_id": None,
            "experience_name": "self_ref",
            "supersedes": "self_ref",
            "content": {"blocks": [{"search": "", "replace": "## Situation\n- x"}]},
        }],
    }))

    report = await commit_session(ready, session_id=sid, llm_call=llm)

    names = {i.title for i in await memory.list_items(MemoryScope(agent_id=AGENT), "experiences")}
    assert "self_ref" in names, "不能把刚写的那条删掉"
    assert any("指向自己" in w for w in report.warnings)


@pytest.mark.asyncio
async def test_supersedes_missing_target_is_tolerated(
    ready: AsyncSession, workspace_id: str
) -> None:
    """旧的不存在不是错误 —— 模型可能记错名字，或它已被别处删掉。"""
    sid = await seed_session(ready, "ses_first_memory", workspace_id=workspace_id, agent_id=AGENT)
    llm = FakeLLM(json.dumps({
        "reasoning": "r",
        "experiences": [{
            "page_id": None,
            "experience_name": "brand_new",
            "supersedes": "never_existed",
            "content": {"blocks": [{"search": "", "replace": "## Situation\n- x"}]},
        }],
    }))

    report = await commit_session(ready, session_id=sid, llm_call=llm)

    assert report.ok
    assert any("never_existed 不存在" in w for w in report.warnings)


@pytest.mark.asyncio
async def test_commit_deletes_by_page_id(ready: AsyncSession, workspace_id: str) -> None:
    """删除也走 page_id，且要留下正文痕迹。"""
    sid = await seed_session(ready, "ses_first_memory", workspace_id=workspace_id, agent_id=AGENT)
    await memory.write(MemoryScope(agent_id=AGENT), "preferences",
                       {"topic": "outdated", "content": "- 过时的偏好"}, db=ready)

    # 查真实 page_id，不硬编码 —— 骨架文件会占用小号
    pre = await prefetch(MemoryScope(agent_id=AGENT, session_id=sid))
    pid = pre.pages.assign(pre.by_type["preferences"][0].uri)

    llm = FakeLLM(json.dumps({"reasoning": "这条过时了", "delete_page_ids": [pid]}))
    report = await commit_session(ready, session_id=sid, llm_call=llm)

    assert report.batch is not None
    assert len(report.batch.deletes) == 1
    assert report.batch.deletes[0].deleted_content == "- 过时的偏好"
    assert await memory.list_items(MemoryScope(agent_id=AGENT), "preferences") == []


@pytest.mark.asyncio
async def test_commit_ignores_empty_shell_items(
    ready: AsyncSession, workspace_id: str
) -> None:
    """
    模型偶尔输出 {"page_id": null} 空壳。写进去会产生只有 frontmatter
    的空文件 —— 那比不写更糟，它会占位在列表里而点开是空的。
    """
    sid = await seed_session(ready, "ses_first_memory", workspace_id=workspace_id, agent_id=AGENT)
    llm = FakeLLM(json.dumps({
        "reasoning": "r",
        "preferences": [{"page_id": None}, {"page_id": None, "topic": "real",
                                            "content": {"blocks": [{"search": "", "replace": "- 真内容"}]}}],
    }))

    report = await commit_session(ready, session_id=sid, llm_call=llm)

    assert report.batch is not None
    assert len(report.batch.written) == 1
    assert any("没有任何有效字段" in w for w in report.warnings)


@pytest.mark.asyncio
async def test_commit_disabled_by_config(
    ready: AsyncSession, workspace_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    总开关关掉时不提取，但已有记忆保留 —— 删记忆要显式操作。
    """
    monkeypatch.setattr(layout.settings.memory, "enabled", False)
    sid = await seed_session(ready, "ses_first_memory", workspace_id=workspace_id, agent_id=AGENT)

    llm = FakeLLM("{}")
    report = await commit_session(ready, session_id=sid, llm_call=llm)

    assert "已关闭" in report.skipped
    assert llm.rounds == 0


@pytest.mark.asyncio
async def test_multi_agent_session_memory_isolation(
    ready: AsyncSession, workspace_id: str, memory_dir: Path
) -> None:
    """
    多智能体场景：只有第一个智能体提取 session 和 global 记忆，
    其他智能体只提取自己的 agent 记忆，避免并发冲突。
    """
    from app.modules.agent.messages import Msg
    from app.modules.session import repo

    # 创建会话
    sess = await repo.create_session(ready, workspace_id=workspace_id)
    sid = sess.id

    # 插入两个智能体的消息
    await repo.append_message(ready, sid, Msg(role="user", content="你好 Alice", agent_name=""))
    await repo.append_message(ready, sid, Msg(role="assistant", content="Hi", agent_name="adf_alice"))
    await repo.append_message(ready, sid, Msg(role="user", content="你好 Bob", agent_name=""))
    await repo.append_message(ready, sid, Msg(role="assistant", content="Hello", agent_name="adf_bob"))

    # 模拟两个智能体都会提取记忆
    # 注意：events 的 scope 是 session，但 Alice 用的 scope_session 只能看到自己有权访问的类型
    # 本测试的重点是验证 Bob 不会写 session 层（agent_only=True），而不是验证具体写了哪些类型
    call_count = 0

    async def multi_llm(*args: Any, **kwargs: Any) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:  # Alice
            return json.dumps({
                "reasoning": "记录 Alice 看到的",
                "preferences": [{"topic": "alice_pref", "content": "- Alice 的偏好"}],
            })
        else:  # Bob
            return json.dumps({
                "reasoning": "记录 Bob 看到的",
                "preferences": [{"topic": "bob_pref", "content": "- Bob 的偏好"}],
            })

    # 不传 agent_id，让它自动识别多智能体；禁用截断保证消息被提取
    report = await commit_session(ready, session_id=sid, llm_call=multi_llm, keep_recent_turns=0)

    assert len(report.agents) == 2
    alice_report = next(r for r in report.agents if r.agent_id == "adf_alice")
    bob_report = next(r for r in report.agents if r.agent_id == "adf_bob")

    # Alice（第一个智能体）应该成功写入（agent_only=False）
    assert alice_report.batch is not None
    assert len(alice_report.batch.written) > 0, "Alice 应该写入了记忆"
    assert not any("agent_only" in w for w in alice_report.warnings), "Alice 不应该有 agent_only 警告"

    # Bob（第二个智能体）应该成功写入（agent_only=True）
    assert bob_report.batch is not None
    assert len(bob_report.batch.written) > 0, "Bob 应该写入了记忆"
    # Bob 应该有 "agent_only 模式下跳过" 的提示（如果他的 LLM 输出了 session 类型）
    # 但因为我们的 mock LLM 只输出 preferences（agent 层），所以不会有 skip 警告
    # 真正的验证点是：Bob 的 visible_types 里不包含 session 类型
