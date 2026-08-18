"""
Agent loop 测试。

重点测三件"不会报错但功能静默失效"的事：
1. 取消后落库完整（journal sink 机制）
2. 取消后不留孤立的 tool_calls（否则该会话永久 400）
3. 工具异常转成错误文本而非崩掉整轮
"""

import asyncio

import pytest
from app.core.events import Ev, EventBus, reset_bus, set_bus
from app.infra.llm.port import TokenUsage
from app.modules.agent.loop import AgentLoop
from app.modules.agent.messages import Msg, find_missing_tool_calls, repair_tool_pairing
from app.modules.agent.tools.base import ToolRegistry
from app.modules.session import repo
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes import (
    BoomTool,
    EchoTool,
    FakeLLM,
    HangTool,
    fake_model,
    text_chunks,
    tool_call_chunks,
)


def _registry(*tools: object) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)  # type: ignore[arg-type]
    return reg


async def _loop(
    db: AsyncSession, session_id: str, llm: FakeLLM, reg: ToolRegistry, journal: list[Msg]
) -> AgentLoop:
    loop = AgentLoop(
        db=db,
        llm=llm,  # type: ignore[arg-type]
        model=fake_model(),
        registry=reg,
        session_id=session_id,
        run_id="run_test",
        system_prompt="你是测试助手",
        journal_sink=journal,
    )
    await loop.load_context()
    return loop


class TestBasicFlow:
    async def test_plain_answer(self, db: AsyncSession, session_id: str) -> None:
        await repo.append_message(db, session_id, Msg(role="user", content="你好"))
        llm = FakeLLM([text_chunks("你好呀", usage=TokenUsage(100, 5))])
        journal: list[Msg] = []
        loop = await _loop(db, session_id, llm, _registry(), journal)

        result = await loop.run()

        assert result.stop_reason == "final"
        assert result.turns == 1
        assert result.final_text == "你好呀"
        assert result.prompt_tokens == 100

        rows = await repo.load_messages(db, session_id)
        assert [r.role for r in rows] == ["user", "assistant"]
        assert rows[1].content == "你好呀"

    async def test_tool_then_answer(self, db: AsyncSession, session_id: str) -> None:
        await repo.append_message(db, session_id, Msg(role="user", content="echo hi"))
        llm = FakeLLM(
            [
                tool_call_chunks("echo", '{"text":"hi"}'),
                text_chunks("完成了"),
            ]
        )
        journal: list[Msg] = []
        loop = await _loop(db, session_id, llm, _registry(EchoTool()), journal)

        result = await loop.run()

        assert result.stop_reason == "final"
        assert result.turns == 2
        rows = await repo.load_messages(db, session_id)
        assert [r.role for r in rows] == ["user", "assistant", "tool", "assistant"]
        assert rows[2].content == "echo: hi"
        assert rows[2].tool_call_id == "call_1"

        # 第二轮发给 LLM 的消息里必须包含完整的 tool 配对
        second = llm.received[1]
        assert second[0]["role"] == "system"
        roles = [m["role"] for m in second]
        assert roles == ["system", "user", "assistant", "tool"]

    async def test_tool_exception_becomes_error_text(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """工具抛异常不能让整轮崩掉 —— 转成错误文本让模型自我纠正。"""
        await repo.append_message(db, session_id, Msg(role="user", content="boom"))
        llm = FakeLLM([tool_call_chunks("boom", "{}"), text_chunks("我换个方式")])
        journal: list[Msg] = []
        loop = await _loop(db, session_id, llm, _registry(BoomTool()), journal)

        result = await loop.run()

        assert result.stop_reason == "final"
        rows = await repo.load_messages(db, session_id)
        tool_row = next(r for r in rows if r.role == "tool")
        assert tool_row.is_error == 1
        assert "炸了" in tool_row.content

    async def test_unknown_tool_lists_available(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """模型幻觉出不存在的工具时，返回可用列表让它纠正。"""
        await repo.append_message(db, session_id, Msg(role="user", content="x"))
        llm = FakeLLM([tool_call_chunks("nonexistent", "{}"), text_chunks("ok")])
        journal: list[Msg] = []
        loop = await _loop(db, session_id, llm, _registry(EchoTool()), journal)

        await loop.run()

        rows = await repo.load_messages(db, session_id)
        tool_row = next(r for r in rows if r.role == "tool")
        assert tool_row.is_error == 1
        assert "echo" in tool_row.content


class TestCancel:
    async def test_journal_complete_after_cancel(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        取消后已生成的内容必须已落库。

        失败模式：只剩 user 消息 + 空的 assistant 占位，模型说的话全丢，
        且全程零报错。
        """
        await repo.append_message(db, session_id, Msg(role="user", content="hang"))
        hang = HangTool()
        llm = FakeLLM([tool_call_chunks("hang", "{}")])
        journal: list[Msg] = []
        loop = await _loop(db, session_id, llm, _registry(hang), journal)

        task = asyncio.create_task(loop.run())
        await asyncio.wait_for(hang.entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        rows = await repo.load_messages(db, session_id)
        roles = [r.role for r in rows]
        # assistant（含 tool_calls）必须已落库
        assert "assistant" in roles
        ai_row = next(r for r in rows if r.role == "assistant")
        assert ai_row.tool_calls is not None and "hang" in ai_row.tool_calls

    async def test_no_orphan_tool_calls_after_cancel(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        这是最关键的一条：取消后不能留下孤立的 tool_calls，
        否则下一轮把这段历史发给 LLM 会直接 400，该会话永久坏掉。
        """
        await repo.append_message(db, session_id, Msg(role="user", content="hang"))
        hang = HangTool()
        llm = FakeLLM([tool_call_chunks("hang", "{}")])
        journal: list[Msg] = []
        loop = await _loop(db, session_id, llm, _registry(hang), journal)

        task = asyncio.create_task(loop.run())
        await asyncio.wait_for(hang.entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # 重新从 DB 组装：不应有任何缺失的 tool 结果
        rows = await repo.load_messages(db, session_id)
        msgs = [repo.row_to_msg(r) for r in rows]
        assert find_missing_tool_calls(msgs) == []
        _, fixes = repair_tool_pairing(msgs)
        assert fixes == 0, "取消后仍需修复，说明补齐逻辑没生效"

        # 且那条占位消息标了错误态，前端能把卡片从"执行中"改成错误
        tool_rows = [r for r in rows if r.role == "tool"]
        assert len(tool_rows) == 1
        assert tool_rows[0].is_error == 1
        assert "取消" in tool_rows[0].content

    async def test_next_turn_after_cancel_is_valid(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """取消后紧接着再发一轮，发给 LLM 的消息序列必须合法。"""
        await repo.append_message(db, session_id, Msg(role="user", content="hang"))
        hang = HangTool()
        llm1 = FakeLLM([tool_call_chunks("hang", "{}")])
        loop1 = await _loop(db, session_id, llm1, _registry(hang), [])
        task = asyncio.create_task(loop1.run())
        await asyncio.wait_for(hang.entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # 第二轮
        await repo.append_message(db, session_id, Msg(role="user", content="继续"))
        llm2 = FakeLLM([text_chunks("好")])
        loop2 = await _loop(db, session_id, llm2, _registry(EchoTool()), [])
        result = await loop2.run()

        assert result.stop_reason == "final"
        sent = llm2.received[0]
        # 每个 tool 消息前面都能找到声明它的 assistant
        for i, m in enumerate(sent):
            if m["role"] == "tool":
                assert sent[i - 1]["role"] in ("assistant", "tool")


class TestMaxTurns:
    async def test_stops_at_max_turns(
        self, db: AsyncSession, session_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import settings

        monkeypatch.setattr(settings.agent, "max_turns", 3)
        await repo.append_message(db, session_id, Msg(role="user", content="loop"))
        # 永远调工具，逼它撞上限
        llm = FakeLLM([tool_call_chunks("echo", '{"text":"x"}')])
        journal: list[Msg] = []
        loop = await _loop(db, session_id, llm, _registry(EchoTool()), journal)

        result = await loop.run()

        assert result.stop_reason == "max_turns"
        assert result.turns == 3


class TestEvents:
    async def test_emits_expected_events(self, db: AsyncSession, session_id: str) -> None:
        await repo.append_message(db, session_id, Msg(role="user", content="echo"))
        llm = FakeLLM([tool_call_chunks("echo", '{"text":"hi"}'), text_chunks("好了")])
        bus = EventBus()
        token = set_bus(bus)
        try:
            loop = await _loop(db, session_id, llm, _registry(EchoTool()), [])
            await loop.run()
        finally:
            reset_bus(token)
            await bus.close()

        events: list[dict] = []
        while True:
            item = await bus.get()
            if item is None:
                break
            events.append(item)

        seen = [e["event"] for e in events]
        assert Ev.TOOL_START in seen
        assert Ev.TOOL_END in seen
        assert Ev.MESSAGE in seen

        # 每个事件都必须带公共字段（span 三件套 + ts），
        # 前端靠它们把扁平事件流还原成气泡树
        for e in events:
            assert set(e["data"]) >= {"ts", "span_id", "parent_span_id", "depth"}

        # tool 事件的 span 应该嵌在 llm/agent 之下（depth 递增）
        tool_start = next(e for e in events if e["event"] == Ev.TOOL_START)
        assert tool_start["data"]["span_id"] is not None

    async def test_emit_without_bus_is_noop(self, db: AsyncSession, session_id: str) -> None:
        """
        无订阅者时 emit 静默 no-op —— 这让流式与非流式走完全相同的代码路径。
        """
        await repo.append_message(db, session_id, Msg(role="user", content="x"))
        llm = FakeLLM([text_chunks("y")])
        loop = await _loop(db, session_id, llm, _registry(), [])
        result = await loop.run()  # 没有 set_bus，不应抛异常
        assert result.stop_reason == "final"
