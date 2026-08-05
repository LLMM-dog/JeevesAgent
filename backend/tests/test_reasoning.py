"""
推理模型（带思维链）的专项测试。

真实模型验证时发现的形状：一次 tool_call 轮次是
  content=0 字符、reasoning=306 字符、tool_calls=2 个
说明 content 为空对推理模型是【常态】，不能当异常。

由此暴露的缺陷：`reasoning 有内容但 content 和 tool_calls 都空` 时
loop 会当成"模型说完了"，返回上一轮的旧答案。
推理模型在 max_tokens 用尽于思考阶段时正是这个形状。
"""

from typing import Any

import pytest
from app.infra.llm.port import LLMChunk, TokenUsage, ToolCallDelta
from app.modules.agent.loop import AgentLoop
from app.modules.agent.messages import Msg
from app.modules.agent.tools.base import ToolRegistry
from app.modules.session import repo
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes import EchoTool, FakeLLM, fake_model, text_chunks


def reasoning_only(reasoning: str, finish: str = "length") -> list[LLMChunk]:
    """只有思维链、没有正文和工具调用的响应。"""
    out: list[LLMChunk] = []
    for ch in reasoning:
        out.append(LLMChunk(kind="reasoning", text=ch))
    out.append(LLMChunk(kind="done", finish_reason=finish))
    return out


def reasoning_then_text(reasoning: str, text: str) -> list[LLMChunk]:
    out: list[LLMChunk] = [LLMChunk(kind="reasoning", text=reasoning)]
    for ch in text:
        out.append(LLMChunk(kind="content", text=ch))
    out.append(
        LLMChunk(
            kind="done",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=100, completion_tokens=20),
        )
    )
    return out


def reasoning_then_tool(reasoning: str, name: str, args: str) -> list[LLMChunk]:
    """推理模型的典型形状：思维链 + 工具调用，正文为空。"""
    return [
        LLMChunk(kind="reasoning", text=reasoning),
        LLMChunk(kind="tool_call", tool_call=ToolCallDelta(index=0, call_id="rc1", name=name)),
        LLMChunk(kind="tool_call", tool_call=ToolCallDelta(index=0, arguments_delta=args)),
        LLMChunk(kind="done", finish_reason="tool_calls"),
    ]


async def _mk(db: AsyncSession, sid: str, llm: Any) -> AgentLoop:
    reg = ToolRegistry()
    reg.register(EchoTool())
    loop = AgentLoop(
        db=db,
        llm=llm,
        model=fake_model(),
        registry=reg,
        session_id=sid,
        run_id="run_reason",
        system_prompt="sys",
    )
    await loop.load_context()
    return loop


class TestReasoningModel:
    async def test_reasoning_with_tool_call_and_empty_content_is_normal(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        content 空 + reasoning 有 + tool_calls 有 —— 这是推理模型的常态，
        不能当异常处理。真实的 deepseek 就是这个形状。
        """
        await repo.append_message(db, session_id, Msg(role="user", content="x"))
        llm = FakeLLM(
            [
                reasoning_then_tool("我需要先看看目录…", "echo", '{"text":"hi"}'),
                reasoning_then_text("看完了，汇报一下", "已完成"),
            ]
        )
        loop = await _mk(db, session_id, llm)

        result = await loop.run()

        assert result.stop_reason == "final"
        assert result.final_text == "已完成"
        # 两轮都调了，没有被误判成空响应而重试
        assert llm.calls == 2

        rows = await repo.load_messages(db, session_id)
        assert [r.role for r in rows] == ["user", "assistant", "tool", "assistant"]
        # 思维链逐轮独立保存
        assert rows[1].reasoning == "我需要先看看目录…"
        assert rows[1].content == ""
        assert rows[3].reasoning == "看完了，汇报一下"

    async def test_reasoning_only_truncated_is_not_a_valid_final(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        【核心缺陷】推理模型在思考阶段用尽 max_tokens：
        reasoning 很长、content 空、tool_calls 空、finish_reason=length。

        不能当成"模型说完了" —— 否则返回的是上一轮的旧答案，
        用户看到的回复和问题对不上，而且全程无报错。
        """
        await repo.append_message(db, session_id, Msg(role="user", content="难题"))
        llm = FakeLLM(
            [
                reasoning_only("嗯，这个问题很复杂，我需要仔细想想" * 3, finish="length"),
                reasoning_then_text("想清楚了", "答案是 42"),
            ]
        )
        loop = await _mk(db, session_id, llm)

        result = await loop.run()

        assert result.stop_reason == "final"
        # 关键：必须是重试后的真答案，不能是空串
        assert result.final_text == "答案是 42"
        assert llm.calls == 2, "只有思维链的响应应当被重试"

    async def test_reasoning_only_stop_also_retried(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """finish_reason=stop 但只有思维链，同样不是有效答复。"""
        await repo.append_message(db, session_id, Msg(role="user", content="x"))
        llm = FakeLLM(
            [
                reasoning_only("想了想但什么也没说", finish="stop"),
                reasoning_then_text("这次说了", "好的"),
            ]
        )
        loop = await _mk(db, session_id, llm)

        result = await loop.run()
        assert result.final_text == "好的"
        assert llm.calls == 2

    async def test_stale_answer_never_returned(
        self, db: AsyncSession, session_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        持续只返回思维链时，宁可报错也不能返回上一轮的旧答案。
        """
        from app.core.config import settings

        monkeypatch.setattr(settings.agent, "max_llm_retries", 2)
        await repo.append_message(db, session_id, Msg(role="user", content="x"))

        llm = FakeLLM(
            [
                reasoning_then_tool("先查一下", "echo", '{"text":"a"}'),
                reasoning_only("在想…", finish="length"),
            ]
        )
        loop = await _mk(db, session_id, llm)

        with pytest.raises(Exception):  # noqa: B017
            await loop.run()

    async def test_thinking_events_emitted_incrementally(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        思维链要逐字发 thinking 事件，前端才能显示"正在思考"。
        真实模型验证时是 144 个 thinking 事件 / 338 字符。
        """
        from app.core.events import Ev, EventBus, reset_bus, set_bus

        await repo.append_message(db, session_id, Msg(role="user", content="x"))
        llm = FakeLLM([reasoning_only("abcde", finish="stop"), text_chunks("done")])
        loop = await _mk(db, session_id, llm)

        # EventBus 内部是队列，事件会缓冲住 —— 跑完再取即可，
        # 不需要并发订阅者
        bus = EventBus()
        token = set_bus(bus)
        try:
            await loop.run()
        finally:
            await bus.close()
            reset_bus(token)

        collected: list[dict[str, Any]] = []
        while True:
            item = await bus.get()
            if item is None:
                break
            collected.append(item)

        thinking = [p for p in collected if p.get("event") == str(Ev.THINKING)]
        assert len(thinking) == 5, (
            f"应有 5 个 thinking 事件，实际 {len(thinking)}；"
            f"收到的事件名={sorted({str(p.get('event')) for p in collected})}"
        )
        # 载荷结构是 {"event": 名字, "data": {...}}
        assert "".join(p["data"]["delta"] for p in thinking) == "abcde"
        # 每个事件都要带 span 三件套，前端靠它们还原气泡树
        for p in thinking:
            assert "span_id" in p["data"]
            assert p["data"]["ts"] > 0

    async def test_reasoning_sent_back_only_on_tool_call_turns(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        思维链【只在带 tool_calls 的轮次】回传给上游。

        DeepSeek 文档的规则：两个 user 消息之间有工具调用时，中间 assistant
        的 reasoning_content 必须传回（让模型延续推理）；没有工具调用时
        不必传，传了也会被忽略。

        对 agent 是实质收益：tool_call 轮次的 content 常常是空的
        （真实观测 content=0 字符 / reasoning=306 字符），思维链就是它
        全部的思考。丢掉等于让模型每步都从头想。
        """
        await repo.append_message(db, session_id, Msg(role="user", content="x"))
        llm = FakeLLM(
            [
                reasoning_then_tool("第一轮的思考", "echo", '{"text":"a"}'),
                reasoning_then_text("第二轮的思考", "好"),
            ]
        )
        loop = await _mk(db, session_id, llm)
        await loop.run()

        assert len(llm.received) == 2
        second = llm.received[1]

        # 带 tool_calls 的那条 assistant 必须带上 reasoning_content
        with_calls = [m for m in second if m.get("tool_calls")]
        assert len(with_calls) == 1
        assert with_calls[0]["reasoning_content"] == "第一轮的思考"

        # 工具结果也必须在
        assert any(m.get("role") == "tool" and "echo: a" in m["content"] for m in second)

    async def test_reasoning_dropped_on_plain_answer_turns(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        不带 tool_calls 的 assistant 轮次不回传思维链 —— 上游会忽略，纯浪费 token。
        """
        await repo.append_message(db, session_id, Msg(role="user", content="第一问"))
        llm = FakeLLM([reasoning_then_text("对第一问的思考", "答一")])
        loop = await _mk(db, session_id, llm)
        await loop.run()

        # 第二轮提问，重新加载上下文
        await repo.append_message(db, session_id, Msg(role="user", content="第二问"))
        llm2 = FakeLLM([reasoning_then_text("对第二问的思考", "答二")])
        loop2 = await _mk(db, session_id, llm2)
        await loop2.run()

        sent = llm2.received[0]
        plain = [
            m for m in sent if m.get("role") == "assistant" and not m.get("tool_calls")
        ]
        assert plain, "应该有一条不带 tool_calls 的历史 assistant 消息"
        for m in plain:
            assert "reasoning_content" not in m, "普通回答轮次不应回传思维链"

    async def test_send_reasoning_back_can_be_disabled(
        self, db: AsyncSession, session_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        某些端点对未知字段严格会因 reasoning_content 报 400，
        要能一键关掉。
        """
        from app.core.config import settings

        monkeypatch.setattr(settings.llm, "send_reasoning_back", False)
        await repo.append_message(db, session_id, Msg(role="user", content="x"))
        llm = FakeLLM(
            [
                reasoning_then_tool("思考内容", "echo", '{"text":"a"}'),
                reasoning_then_text("再思考", "好"),
            ]
        )
        loop = await _mk(db, session_id, llm)
        await loop.run()

        blob = str(llm.received[1])
        assert "思考内容" not in blob
        assert "reasoning_content" not in blob
