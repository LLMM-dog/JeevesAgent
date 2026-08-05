"""
压缩在 loop 里的集成测试。

test_compaction.py 测的是纯函数（切点算得对不对）。
这里测的是"接进 loop 之后行为对不对"：阈值触发、summary 落库、
压缩后不 400、超限后强制压缩。
"""

from typing import Any

import pytest
from app.core.config import settings
from app.core.exceptions import ProviderError
from app.infra.llm.port import LLMChunk, ResolvedModel, TokenUsage
from app.modules.agent.loop import AgentLoop
from app.modules.agent.messages import Msg, find_missing_tool_calls
from app.modules.agent.tools.base import ToolRegistry
from app.modules.session import repo
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes import EchoTool, fake_model


class ScriptedLLM:
    """
    可编排 usage 的 FakeLLM。

    压缩测试必须能精确控制 prompt_tokens —— 阈值触发完全由它决定。
    """

    # 默认摘要要足够长。compaction 会拒绝短得不合理的摘要
    # （输入长度的 0.5%，且至少 80 字符）—— 那是为了兜住"模型收到
    # 未替换的占位符后回复'无对话历史内容'"这类垃圾摘要。
    DEFAULT_SUMMARY = (
        "## 用户要求\n"
        "之前约定只改 a.py 这一个文件，不引入新依赖，注释统一用中文。\n\n"
        "## 已完成的改动\n"
        "创建了 a.py，实现了基础功能并补了中文注释。\n\n"
        "## 失败过的尝试\n"
        "最初想用第三方库实现，但与「不引入新依赖」冲突，改为标准库方案。\n\n"
        "## 未完成事项\n"
        "还需要补充单元测试和错误处理分支。"
    )

    def __init__(
        self,
        scripts: list[list[LLMChunk]],
        *,
        compact_reply: str | None = None,
    ) -> None:
        self.scripts = scripts
        self.compact_reply = (
            compact_reply if compact_reply is not None else self.DEFAULT_SUMMARY
        )
        self.calls = 0
        self.received: list[list[dict[str, Any]]] = []
        self.compact_calls = 0

    async def stream_chat(  # type: ignore[override]
        self,
        model: ResolvedModel,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ):
        self.received.append(messages)
        # 压缩请求的特征：单条 user 消息且不带 tools
        is_compact = len(messages) == 1 and not tools
        if is_compact:
            self.compact_calls += 1
            for ch in self.compact_reply:
                yield LLMChunk(kind="content", text=ch)
            yield LLMChunk(kind="done", finish_reason="stop")
            return

        idx = min(self.calls, len(self.scripts) - 1)
        self.calls += 1
        for chunk in self.scripts[idx]:
            yield chunk

    async def list_models(self, base_url: str, api_key: str) -> list[str]:
        return ["fake-model"]


def reply_with_usage(text: str, prompt_tokens: int) -> list[LLMChunk]:
    out: list[LLMChunk] = [LLMChunk(kind="content", text=text)]
    out.append(
        LLMChunk(
            kind="done",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=prompt_tokens, completion_tokens=10),
        )
    )
    return out


async def seed_history(db: AsyncSession, sid: str, turns: int) -> None:
    """铺一段够长的历史，让压缩有东西可压。"""
    for i in range(turns):
        await repo.append_message(db, sid, Msg(role="user", content=f"问题{i}"))
        await repo.append_message(
            db, sid, Msg(role="assistant", content=f"回答{i}" * 20)
        )


async def mk_loop(
    db: AsyncSession, sid: str, llm: Any, *, window: int = 32768
) -> AgentLoop:
    reg = ToolRegistry()
    reg.register(EchoTool())
    loop = AgentLoop(
        db=db,
        llm=llm,
        model=fake_model(context_window=window),
        registry=reg,
        session_id=sid,
        run_id="run_compact",
        system_prompt="系统指令",
    )
    await loop.load_context()
    return loop


class TestThresholdTrigger:
    async def test_compacts_when_real_usage_exceeds_threshold(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        上一轮真实 usage 超过阈值时，下一轮调用前压缩。
        """
        await seed_history(db, session_id, 10)
        await repo.append_message(db, session_id, Msg(role="user", content="继续"))

        # 窗口 1000，阈值 0.75 → 750。第一轮报 800，第二轮应触发压缩
        llm = ScriptedLLM(
            [
                reply_with_usage("第一轮", prompt_tokens=800),
                reply_with_usage("第二轮", prompt_tokens=100),
            ]
        )
        loop = await mk_loop(db, session_id, llm, window=1000)
        loop._last_prompt_tokens = 800  # 模拟上一轮的真实 usage

        await loop.run()

        assert llm.compact_calls == 1, "应该压缩了一次"
        assert any(m.role == "summary" for m in loop.messages)

    async def test_no_compact_below_threshold(
        self, db: AsyncSession, session_id: str
    ) -> None:
        await seed_history(db, session_id, 10)
        await repo.append_message(db, session_id, Msg(role="user", content="继续"))

        llm = ScriptedLLM([reply_with_usage("答", prompt_tokens=100)])
        loop = await mk_loop(db, session_id, llm, window=10000)
        loop._last_prompt_tokens = 100

        await loop.run()
        assert llm.compact_calls == 0

    async def test_estimate_threshold_is_stricter(
        self, db: AsyncSession, session_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        没有真实 usage 时用估算，阈值要打折 —— 估算偏差可达 20%，
        不打折会估低后直接 400。
        """
        monkeypatch.setattr(settings.agent, "estimate_safety_ratio", 0.5)
        await seed_history(db, session_id, 20)
        await repo.append_message(db, session_id, Msg(role="user", content="继续"))

        llm = ScriptedLLM([reply_with_usage("答", prompt_tokens=50)])
        # 窗口设得让估算值落在打折阈值之上：2000*0.75*0.5=750
        loop = await mk_loop(db, session_id, llm, window=2000)
        loop._last_prompt_tokens = 0  # 强制走估算分支

        await loop.run()
        # 估算值应该超过 2000*0.75*0.5=750
        assert llm.compact_calls >= 1


class TestRealUsageRestoredAcrossRequests:
    async def test_last_prompt_tokens_loaded_from_db(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        每个 HTTP 请求都新建 loop，_last_prompt_tokens 从 0 开始。
        不从库里恢复的话【每次请求的第一轮都走估算分支】——
        而单轮对话恰好只有第一轮，于是"用真实 usage 触发压缩"
        这条铁律在最常见的场景里根本没生效。
        """
        await repo.append_message(db, session_id, Msg(role="user", content="问"))
        await repo.append_message(
            db,
            session_id,
            Msg(role="assistant", content="答"),
            prompt_tokens=4321,
            completion_tokens=10,
        )

        llm = ScriptedLLM([reply_with_usage("x", prompt_tokens=100)])
        loop = await mk_loop(db, session_id, llm, window=100000)

        assert loop._last_prompt_tokens == 4321, "没有从库里恢复真实 usage"

    async def test_restored_usage_triggers_compaction_on_first_turn(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        恢复的真实 usage 要能在【第一轮】就触发压缩 ——
        这正是单轮对话的场景。
        """
        await seed_history(db, session_id, 10)
        # 最后一条 assistant 带一个已经超阈值的 prompt_tokens
        await repo.append_message(
            db,
            session_id,
            Msg(role="assistant", content="上一轮的答"),
            prompt_tokens=900,
            completion_tokens=10,
        )
        await repo.append_message(db, session_id, Msg(role="user", content="新问题"))

        llm = ScriptedLLM([reply_with_usage("新答", prompt_tokens=200)])
        # 窗口 1000 → 阈值 750，恢复的 900 已越过
        loop = await mk_loop(db, session_id, llm, window=1000)
        await loop.run()

        assert llm.compact_calls == 1, "恢复的真实 usage 没能触发压缩"


class TestSummaryPersisted:
    async def test_summary_written_to_db(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        summary 必须落库。否则下次打开会话重新组装上下文时压缩成果丢失，
        又会立刻撞一次超限。
        """
        await seed_history(db, session_id, 10)
        await repo.append_message(db, session_id, Msg(role="user", content="继续"))

        llm = ScriptedLLM([reply_with_usage("答", prompt_tokens=100)])
        loop = await mk_loop(db, session_id, llm, window=1000)
        loop._last_prompt_tokens = 900

        await loop.run()

        rows = await repo.load_messages(db, session_id)
        summaries = [r for r in rows if r.role == "summary"]
        assert len(summaries) == 1
        assert "a.py" in summaries[0].content

    async def test_original_messages_not_deleted(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        压缩只改工作副本，不删库里的原始消息 ——
        用户在前端仍要能看到全部历史。
        """
        await seed_history(db, session_id, 10)
        await repo.append_message(db, session_id, Msg(role="user", content="继续"))
        before = len(await repo.load_messages(db, session_id))

        llm = ScriptedLLM([reply_with_usage("答", prompt_tokens=100)])
        loop = await mk_loop(db, session_id, llm, window=1000)
        loop._last_prompt_tokens = 900
        await loop.run()

        rows = await repo.load_messages(db, session_id)
        # 只增不减：原有的 + summary + 本轮的 assistant
        assert len(rows) > before
        assert any(r.content == "问题0" for r in rows), "原始消息被删了"


class TestNoOrphanAfterCompact:
    async def test_compacted_context_has_no_orphan_tool_calls(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        压缩后的上下文不能有孤立 tool_call —— 那会让下一轮请求 400。
        """
        # 铺一段带工具调用的历史
        from app.modules.agent.messages import ToolCall

        for i in range(8):
            await repo.append_message(db, session_id, Msg(role="user", content=f"任务{i}"))
            await repo.append_message(
                db,
                session_id,
                Msg(
                    role="assistant",
                    content="",
                    tool_calls=[ToolCall(id=f"c{i}", name="echo", arguments="{}")],
                ),
            )
            await repo.append_message(
                db,
                session_id,
                Msg(
                    role="tool",
                    content="ok" * 50,
                    tool_call_id=f"c{i}",
                    tool_name="echo",
                ),
            )
            await repo.append_message(
                db, session_id, Msg(role="assistant", content=f"完成{i}")
            )
        await repo.append_message(db, session_id, Msg(role="user", content="继续"))

        llm = ScriptedLLM([reply_with_usage("答", prompt_tokens=100)])
        loop = await mk_loop(db, session_id, llm, window=1000)
        loop._last_prompt_tokens = 900

        await loop.run()

        assert llm.compact_calls == 1
        assert find_missing_tool_calls(loop.messages) == []
        # 发给上游的第二次请求（压缩后）也不能有孤立 tool
        chat_requests = [r for r in llm.received if len(r) > 1]
        assert chat_requests
        for req in chat_requests:
            tool_ids = {m.get("tool_call_id") for m in req if m.get("role") == "tool"}
            declared = {
                tc["id"]
                for m in req
                for tc in (m.get("tool_calls") or [])
            }
            assert tool_ids <= declared, "请求里有孤立的 tool 消息"


class TestOverflowForcesCompaction:
    async def test_overflow_triggers_compact_then_retries(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        上游报上下文超限时，先压缩再重试 —— 直接重试会再超一次。
        """
        await seed_history(db, session_id, 10)
        await repo.append_message(db, session_id, Msg(role="user", content="继续"))

        class OverflowThenOK(ScriptedLLM):
            def __init__(self) -> None:
                super().__init__([])
                self.chat_calls = 0

            async def stream_chat(  # type: ignore[override]
                self, model, messages, tools=None, **kw
            ):
                self.received.append(messages)
                if len(messages) == 1 and not tools:
                    self.compact_calls += 1
                    for ch in self.compact_reply:
                        yield LLMChunk(kind="content", text=ch)
                    yield LLMChunk(kind="done", finish_reason="stop")
                    return
                self.chat_calls += 1
                if self.chat_calls == 1:
                    raise ProviderError(
                        "maximum context length is 8192 tokens",
                        code="context_overflow",
                    )
                yield LLMChunk(kind="content", text="压缩后成功")
                yield LLMChunk(kind="done", finish_reason="stop")

        llm = OverflowThenOK()
        loop = await mk_loop(db, session_id, llm, window=100000)
        # 阈值远未到，所以不是主动压缩触发的
        loop._last_prompt_tokens = 10

        result = await loop.run()

        assert llm.compact_calls == 1, "超限后应该强制压缩"
        assert result.final_text == "压缩后成功"

    async def test_compact_failure_raises_original_error(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        压缩失败时要抛【原始的 overflow 错误】，不是"压缩失败"——
        原始错误对用户更有信息量。
        """
        await repo.append_message(db, session_id, Msg(role="user", content="就一句话"))

        class AlwaysOverflow(ScriptedLLM):
            def __init__(self) -> None:
                super().__init__([])

            async def stream_chat(  # type: ignore[override]
                self, model, messages, tools=None, **kw
            ):
                if len(messages) == 1 and not tools:
                    self.compact_calls += 1
                    yield LLMChunk(kind="done", finish_reason="stop")
                    return
                raise ProviderError(
                    "maximum context length exceeded", code="context_overflow"
                )

        llm = AlwaysOverflow()
        loop = await mk_loop(db, session_id, llm, window=100000)

        # loop 把 ProviderError 转成 stop_reason="error" 而不是上抛
        # （chat_service 据此发 error 事件）。关键是 error 里保留的是
        # 原始的 overflow 信息，不是"压缩失败"。
        result = await loop.run()
        assert result.stop_reason == "error"
        assert result.error is not None
        assert "context length" in result.error, (
            f"应保留原始 overflow 错误，实际是：{result.error}"
        )


class TestGarbageSummaryRejected:
    async def test_too_short_summary_rejected(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        摘要短得不合理时拒绝使用。

        实测遇到过 9662 token 的历史被"压缩"成 17 个字符
        （"无对话历史内容。请提供需要压缩的对话历史。"）。这种摘要
        通过了所有其它检查：压缩事件正常、token 数正常下降、日志无异常 ——
        只有内容是垃圾，而会话会从此越来越糊涂。
        """
        await seed_history(db, session_id, 12)
        await repo.append_message(db, session_id, Msg(role="user", content="继续"))

        llm = ScriptedLLM(
            [reply_with_usage("答", prompt_tokens=100)],
            compact_reply="无对话历史内容。请提供需要压缩的对话历史。",
        )
        loop = await mk_loop(db, session_id, llm, window=1000)
        loop._last_prompt_tokens = 900

        await loop.run()

        # 压缩模型被调用了，但结果被拒绝
        assert llm.compact_calls == 1
        rows = await repo.load_messages(db, session_id)
        assert not any(r.role == "summary" for r in rows), "垃圾摘要被存下来了"

    async def test_reasonable_summary_accepted(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """正常长度的摘要要能通过。"""
        await seed_history(db, session_id, 12)
        await repo.append_message(db, session_id, Msg(role="user", content="继续"))

        good = (
            "## 用户要求\n"
            "只改一个文件，不引入新依赖，注释用中文。\n\n"
            "## 已完成\n"
            "创建了 src/a.py，实现了 add(a, b) 函数。\n\n"
            "## 失败过的尝试\n"
            "先试过用 requests，但用户明确禁止，改用 httpx。\n\n"
            "## 未完成\n"
            "还需要补单元测试。"
        )
        llm = ScriptedLLM(
            [reply_with_usage("答", prompt_tokens=100)], compact_reply=good
        )
        loop = await mk_loop(db, session_id, llm, window=1000)
        loop._last_prompt_tokens = 900

        await loop.run()

        rows = await repo.load_messages(db, session_id)
        summaries = [r for r in rows if r.role == "summary"]
        assert len(summaries) == 1
        assert "不引入新依赖" in summaries[0].content

    async def test_empty_summary_rejected(
        self, db: AsyncSession, session_id: str
    ) -> None:
        await seed_history(db, session_id, 12)
        await repo.append_message(db, session_id, Msg(role="user", content="继续"))

        llm = ScriptedLLM(
            [reply_with_usage("答", prompt_tokens=100)], compact_reply=""
        )
        loop = await mk_loop(db, session_id, llm, window=1000)
        loop._last_prompt_tokens = 900
        await loop.run()

        rows = await repo.load_messages(db, session_id)
        assert not any(r.role == "summary" for r in rows)


class TestCompactModelFallback:
    async def test_falls_back_to_chat_model_when_unbound(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        compact 位没绑定时回落到 chat 模型，而不是找个最便宜的 ——
        compact 错一次影响整个会话往后的全部推理。
        """
        await seed_history(db, session_id, 10)
        await repo.append_message(db, session_id, Msg(role="user", content="继续"))

        llm = ScriptedLLM([reply_with_usage("答", prompt_tokens=100)])
        loop = await mk_loop(db, session_id, llm, window=1000)
        loop._last_prompt_tokens = 900

        # 库里没有任何 binding，resolve 会抛 AppError → 回落
        await loop.run()
        assert llm.compact_calls == 1


class TestCompactEvents:
    async def test_emits_compacting_and_compacted(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        压缩要花一次 LLM 调用（几秒）。只发 compacted 的话
        用户会看到界面卡住而没有任何解释。
        """
        from app.core.events import Ev, EventBus, reset_bus, set_bus

        await seed_history(db, session_id, 10)
        await repo.append_message(db, session_id, Msg(role="user", content="继续"))

        llm = ScriptedLLM([reply_with_usage("答", prompt_tokens=100)])
        loop = await mk_loop(db, session_id, llm, window=1000)
        loop._last_prompt_tokens = 900

        bus = EventBus()
        token = set_bus(bus)
        try:
            await loop.run()
        finally:
            await bus.close()
            reset_bus(token)

        events: list[dict[str, Any]] = []
        while True:
            item = await bus.get()
            if item is None:
                break
            events.append(item)

        names = [e["event"] for e in events]
        assert str(Ev.COMPACTING) in names
        assert str(Ev.COMPACTED) in names

        done = next(e for e in events if e["event"] == str(Ev.COMPACTED))
        # 压缩后必须更小，否则压缩没意义
        assert done["data"]["after_tokens"] < done["data"]["before_tokens"]
        assert done["data"]["victim_count"] > 0
