"""
loop 的防护机制测试。

这四项都是"没有它也能跑，但生产里会咬人"的类型：
1. 截断保护 —— 半截参数被真的执行（空参数写文件，不可逆）
2. 空响应 —— 静默返回上一轮的旧答案，用户看到回复与问题对不上
3. 错误路径补齐 tool_calls —— 库里留孤立 tool_call，前端卡片永远转圈
4. 工具超时 —— 单个工具挂死整个 run，而心跳让前端看不出异常
"""

import asyncio
from typing import Any

import pytest
from app.core.exceptions import ProviderError
from app.infra.llm.openai_compat import classify_error
from app.infra.llm.port import LLMChunk, ToolCallDelta
from app.modules.agent.loop import AgentLoop
from app.modules.agent.messages import Msg, find_missing_tool_calls, repair_tool_pairing
from app.modules.agent.tools.base import ToolContext, ToolRegistry, ToolResult
from app.modules.session import repo
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes import EchoTool, FakeLLM, fake_model, text_chunks, tool_call_chunks


def _reg(*tools: object) -> ToolRegistry:
    r = ToolRegistry()
    for t in tools:
        r.register(t)  # type: ignore[arg-type]
    return r


async def _mk(
    db: AsyncSession, sid: str, llm: Any, reg: ToolRegistry
) -> AgentLoop:
    loop = AgentLoop(
        db=db,
        llm=llm,
        model=fake_model(),
        registry=reg,
        session_id=sid,
        run_id="run_guard",
        system_prompt="sys",
    )
    await loop.load_context()
    return loop


class TestTruncationGuard:
    async def test_truncated_tool_calls_not_executed(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        finish_reason=length 时 arguments 可能是半截 JSON。
        绝不能执行 —— parsed_args() 对坏 JSON 返回 {}，
        于是 write_file() 会以空参数被真的调用。
        """
        await repo.append_message(db, session_id, Msg(role="user", content="x"))

        executed: list[dict[str, Any]] = []

        class SpyTool:
            name = "spy"
            description = "记录被调用的参数"
            requires_approval = False

            def parameters(self) -> dict[str, Any]:
                return {"type": "object", "properties": {}}

            async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
                executed.append(kw)
                return ToolResult(content="ran")

        # 模拟被截断的流：tool_call 的 arguments 是半截 JSON，
        # finish_reason 是 length
        truncated_stream = [
            LLMChunk(
                kind="tool_call",
                tool_call=ToolCallDelta(index=0, call_id="c1", name="spy"),
            ),
            LLMChunk(
                kind="tool_call",
                tool_call=ToolCallDelta(index=0, arguments_delta='{"path": "/very/long/pa'),
            ),
            LLMChunk(kind="done", finish_reason="length"),
        ]
        llm = FakeLLM([truncated_stream, text_chunks("重发完成")])
        loop = await _mk(db, session_id, llm, _reg(SpyTool()))

        await loop.run()

        assert executed == [], "截断的 tool_call 竟然被执行了"

        rows = await repo.load_messages(db, session_id)
        tool_row = next(r for r in rows if r.role == "tool")
        assert tool_row.is_error == 1
        assert "截断" in tool_row.content
        # 配对仍然完整 —— 作废也要有 tool 消息，否则下一轮 400
        msgs = [repo.row_to_msg(r) for r in rows]
        assert find_missing_tool_calls(msgs) == []

    async def test_normal_finish_still_executes(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """finish_reason=tool_calls 时正常执行，不要误伤。"""
        await repo.append_message(db, session_id, Msg(role="user", content="x"))
        llm = FakeLLM([tool_call_chunks("echo", '{"text":"hi"}'), text_chunks("ok")])
        loop = await _mk(db, session_id, llm, _reg(EchoTool()))
        await loop.run()
        rows = await repo.load_messages(db, session_id)
        tool_row = next(r for r in rows if r.role == "tool")
        assert tool_row.is_error == 0
        assert tool_row.content == "echo: hi"


class TestEmptyResponse:
    async def test_empty_response_retried_not_returned_as_final(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        空响应必须重试，不能当成"模型说完了" ——
        否则会静默返回上一轮的旧答案。
        """
        await repo.append_message(db, session_id, Msg(role="user", content="x"))
        empty = [LLMChunk(kind="done", finish_reason="stop")]
        llm = FakeLLM([empty, text_chunks("这才是真答案")])
        loop = await _mk(db, session_id, llm, _reg())

        result = await loop.run()

        assert result.stop_reason == "final"
        assert result.final_text == "这才是真答案"
        # 调了两次：第一次空响应被重试
        assert llm.calls == 2

    async def test_persistent_empty_eventually_raises(
        self, db: AsyncSession, session_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """一直空响应时要有上限，不能无限重试。"""
        from app.core.config import settings

        monkeypatch.setattr(settings.agent, "max_llm_retries", 2)
        await repo.append_message(db, session_id, Msg(role="user", content="x"))
        empty = [LLMChunk(kind="done", finish_reason="stop")]
        llm = FakeLLM([empty])
        loop = await _mk(db, session_id, llm, _reg())

        with pytest.raises(Exception):  # noqa: B017
            await loop.run()
        assert llm.calls == 3  # 1 次初始 + 2 次重试


class TestErrorPathFillsToolResults:
    async def test_tool_provider_error_becomes_text_not_crash(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        先确认一件事：工具内部抛 ProviderError 不会让 run 崩掉。

        registry.execute 的铁律是"永不向上抛异常" —— 工具失败是 agent 的
        正常工作状态。所以三个工具全部会拿到结果（错误文本），配对天然完整。
        """
        await repo.append_message(db, session_id, Msg(role="user", content="x"))

        class BoomTool:
            name = "t"
            description = "总是抛 ProviderError"
            requires_approval = False

            def parameters(self) -> dict[str, Any]:
                return {"type": "object", "properties": {}}

            async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
                raise ProviderError("上游挂了")

        stream = [
            LLMChunk(kind="tool_call", tool_call=ToolCallDelta(index=0, call_id="c1", name="t")),
            LLMChunk(kind="tool_call", tool_call=ToolCallDelta(index=0, arguments_delta="{}")),
            LLMChunk(kind="tool_call", tool_call=ToolCallDelta(index=1, call_id="c2", name="t")),
            LLMChunk(kind="tool_call", tool_call=ToolCallDelta(index=1, arguments_delta="{}")),
            LLMChunk(kind="done", finish_reason="tool_calls"),
        ]
        llm = FakeLLM([stream, text_chunks("我换个方式")])
        loop = await _mk(db, session_id, llm, _reg(BoomTool()))

        result = await loop.run()

        assert result.stop_reason == "final"
        rows = await repo.load_messages(db, session_id)
        tool_rows = [r for r in rows if r.role == "tool"]
        assert len(tool_rows) == 2
        assert all(r.is_error == 1 for r in tool_rows)
        msgs = [repo.row_to_msg(r) for r in rows]
        assert find_missing_tool_calls(msgs) == []

    async def test_persist_failure_mid_batch_leaves_no_orphan(
        self, db: AsyncSession, session_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        真实的孤立场景：落库在工具批次中途失败。

        此时 assistant（带 3 个 tool_calls）和第 1 个 tool 结果已落库，
        第 2、3 个永远不会有。必须补齐，否则库里留下孤立 tool_call，
        前端那两个工具卡片会一直转圈。
        """
        await repo.append_message(db, session_id, Msg(role="user", content="x"))

        import app.modules.agent.loop as loop_mod

        real_append = loop_mod.repo.append_message
        calls = {"n": 0}

        async def flaky_append(*a: Any, **kw: Any) -> str:
            calls["n"] += 1
            # 第 1 次是 assistant，第 2 次是第一个 tool 结果，第 3 次炸
            if calls["n"] == 3:
                raise RuntimeError("模拟落库失败")
            return await real_append(*a, **kw)

        stream = [
            LLMChunk(kind="tool_call", tool_call=ToolCallDelta(index=0, call_id="c1", name="echo")),
            LLMChunk(
                kind="tool_call",
                tool_call=ToolCallDelta(index=0, arguments_delta='{"text":"a"}'),
            ),
            LLMChunk(kind="tool_call", tool_call=ToolCallDelta(index=1, call_id="c2", name="echo")),
            LLMChunk(
                kind="tool_call",
                tool_call=ToolCallDelta(index=1, arguments_delta='{"text":"b"}'),
            ),
            LLMChunk(kind="tool_call", tool_call=ToolCallDelta(index=2, call_id="c3", name="echo")),
            LLMChunk(
                kind="tool_call",
                tool_call=ToolCallDelta(index=2, arguments_delta='{"text":"c"}'),
            ),
            LLMChunk(kind="done", finish_reason="tool_calls"),
        ]
        llm = FakeLLM([stream])
        loop = await _mk(db, session_id, llm, _reg(EchoTool()))

        monkeypatch.setattr(loop_mod.repo, "append_message", flaky_append)

        # 落库失败会上抛（chat_service 会转成 error 事件）
        with pytest.raises(RuntimeError):
            await loop.run()

        # 恢复后检查库：补齐逻辑用的是真实 append
        monkeypatch.setattr(loop_mod.repo, "append_message", real_append)
        rows = await repo.load_messages(db, session_id)
        msgs = [repo.row_to_msg(r) for r in rows]
        assert find_missing_tool_calls(msgs) == [], "落库失败后留下了孤立 tool_call"
        _, fixes = repair_tool_pairing(msgs)
        assert fixes == 0


class TestToolTimeout:
    async def test_hanging_tool_times_out(
        self, db: AsyncSession, session_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        单个工具挂住不能拖死整个 run。
        """
        from app.core.config import settings

        monkeypatch.setattr(settings.agent, "tool_timeout", 1)
        await repo.append_message(db, session_id, Msg(role="user", content="x"))

        class SlowTool:
            name = "slow"
            description = "很慢"
            requires_approval = False

            def parameters(self) -> dict[str, Any]:
                return {"type": "object", "properties": {}}

            async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
                await asyncio.sleep(60)
                return ToolResult(content="never")

        llm = FakeLLM([tool_call_chunks("slow", "{}"), text_chunks("我换个方式")])
        loop = await _mk(db, session_id, llm, _reg(SlowTool()))

        result = await asyncio.wait_for(loop.run(), timeout=15)

        assert result.stop_reason == "final"
        rows = await repo.load_messages(db, session_id)
        tool_row = next(r for r in rows if r.role == "tool")
        assert tool_row.is_error == 1
        assert "超过" in tool_row.content

    async def test_timeout_does_not_break_cancel(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        wait_for 内部会产生 CancelledError，不能让它污染真正的取消路径。
        """
        await repo.append_message(db, session_id, Msg(role="user", content="x"))

        entered = asyncio.Event()

        class HangTool:
            name = "hang"
            description = "挂住"
            requires_approval = False

            def parameters(self) -> dict[str, Any]:
                return {"type": "object", "properties": {}}

            async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
                entered.set()
                await asyncio.sleep(3600)
                return ToolResult(content="never")

        llm = FakeLLM([tool_call_chunks("hang", "{}")])
        loop = await _mk(db, session_id, llm, _reg(HangTool()))

        task = asyncio.create_task(loop.run())
        await asyncio.wait_for(entered.wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # 取消路径仍然正常补齐
        rows = await repo.load_messages(db, session_id)
        msgs = [repo.row_to_msg(r) for r in rows]
        assert find_missing_tool_calls(msgs) == []
        tool_row = next(r for r in rows if r.role == "tool")
        assert "取消" in tool_row.content


class TestRepeatDetection:
    async def test_injects_hint_after_repeated_identical_calls(
        self, db: AsyncSession, session_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        连续多轮相同参数调同一工具 → 注入一次提示让模型换路。
        """
        from app.core.config import settings

        monkeypatch.setattr(settings.agent, "max_repeat_calls", 2)
        monkeypatch.setattr(settings.agent, "max_turns", 6)
        await repo.append_message(db, session_id, Msg(role="user", content="x"))

        # 每轮都返回完全相同的 tool_call
        llm = FakeLLM([tool_call_chunks("echo", '{"text":"same"}')])
        loop = await _mk(db, session_id, llm, _reg(EchoTool()))

        await loop.run()

        hints = [
            m
            for m in loop.messages
            if m.role == "user" and "完全相同的参数" in m.content
        ]
        assert len(hints) == 1, f"应注入恰好 1 次提示，实际 {len(hints)} 次"

    async def test_varying_args_no_hint(
        self, db: AsyncSession, session_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """参数不同就不算打转，不要误报。"""
        from app.core.config import settings

        monkeypatch.setattr(settings.agent, "max_repeat_calls", 2)
        await repo.append_message(db, session_id, Msg(role="user", content="x"))
        llm = FakeLLM(
            [
                tool_call_chunks("echo", '{"text":"a"}'),
                tool_call_chunks("echo", '{"text":"b"}'),
                tool_call_chunks("echo", '{"text":"c"}'),
                text_chunks("done"),
            ]
        )
        loop = await _mk(db, session_id, llm, _reg(EchoTool()))
        await loop.run()
        hints = [m for m in loop.messages if m.role == "user" and "完全相同" in m.content]
        assert hints == []


class TestErrorClassification:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("This model's maximum context length is 8192 tokens", "token_exceed"),
            ("context length exceeded", "token_exceed"),
            ("Please reduce the length of the messages", "token_exceed"),
            ("prompt too long", "token_exceed"),
            ("Rate limit reached for gpt-4", "rate_limit"),
            ("Too Many Requests", "rate_limit"),
            ("You exceeded your current quota", "rate_limit"),
            ("tokens per min (TPM): Limit 30000", "rate_limit"),
            ("Invalid API key provided", "others"),
            ("model not found", "others"),
        ],
    )
    def test_classify(self, text: str, expected: str) -> None:
        assert classify_error(text) == expected

    def test_rate_limit_checked_before_token(self) -> None:
        """
        "tokens per min" 同时含 token 和 limit 类词。
        必须判为 rate_limit（退避重试）而非 token_exceed（压缩）——
        判错会导致白压缩一次还是失败。
        """
        assert classify_error("Limit 30000 tokens per min") == "rate_limit"
