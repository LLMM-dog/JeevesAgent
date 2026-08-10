"""
系统提醒测试 — 验证 _system_reminder 在正确时机注入且不持久化。
"""


from app.modules.agent.loop import AgentLoop
from app.modules.agent.messages import Msg
from app.modules.agent.tools.base import ToolRegistry
from app.modules.session import repo
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes import FakeLLM, fake_model, text_chunks


def _registry() -> ToolRegistry:
    return ToolRegistry()


async def _loop(
    db: AsyncSession, session_id: str, llm: FakeLLM, reg: ToolRegistry
) -> AgentLoop:
    loop = AgentLoop(
        db=db,
        llm=llm,  # type: ignore[arg-type]
        model=fake_model(),
        registry=reg,
        session_id=session_id,
        run_id="run_test",
        system_prompt="你是测试助手",
    )
    await loop.load_context()
    return loop


class TestSystemReminder:
    async def test_not_injected_when_short(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """短会话（≤3 条消息）不注入提醒。"""
        # 只有 1 条消息：user。len=1 ≤ 3，不注入
        await repo.append_message(db, session_id, Msg(role="user", content="你好"))

        llm = FakeLLM([text_chunks("你好！有什么可以帮你？")])
        loop = await _loop(db, session_id, llm, _registry())
        await loop.run()

        assert llm.received, "LLM 应该被调用过"
        sent = llm.received[-1]
        # 最后一条不应该是提醒
        assert "执行规则" not in sent[-1]["content"], f"短会话不应该注入提醒，实际最后一条: {sent[-1]['content'][:200]}"

    async def test_injected_when_long(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """长会话（>3 条消息）注入提醒。"""
        # 4 条消息，触发注入
        await repo.append_message(db, session_id, Msg(role="user", content="问题1"))
        await repo.append_message(db, session_id, Msg(role="assistant", content="答案1"))
        await repo.append_message(db, session_id, Msg(role="user", content="问题2"))
        await repo.append_message(db, session_id, Msg(role="assistant", content="答案2"))

        llm = FakeLLM([text_chunks("好的")])
        loop = await _loop(db, session_id, llm, _registry())
        await loop.run()

        sent = llm.received[-1]
        reminder = sent[-1]
        assert reminder["role"] == "user", f"提醒应该以 user 角色注入，实际: {reminder['role']}"
        assert "执行规则" in reminder["content"], f"提醒应该包含'执行规则'，实际: {reminder['content'][:200]}"
        assert "一次只做一个步骤" in reminder["content"]
        assert "立即验证" in reminder["content"]

    async def test_not_persisted_in_messages(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """提醒不进入 self.messages，不会污染持久化状态。"""
        await repo.append_message(db, session_id, Msg(role="user", content="Q1"))
        await repo.append_message(db, session_id, Msg(role="assistant", content="A1"))
        await repo.append_message(db, session_id, Msg(role="user", content="Q2"))
        await repo.append_message(db, session_id, Msg(role="assistant", content="A2"))

        llm = FakeLLM([text_chunks("好的")])
        loop = await _loop(db, session_id, llm, _registry())
        await loop.run()

        # self.messages 里不应该有提醒
        for msg in loop.messages:
            if msg.role == "user":
                assert "执行规则" not in (msg.content or ""), (
                    f"提醒不应该出现在 self.messages 里: {msg.content[:200]}"
                )

    async def test_reminder_not_duplicated(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """多轮调用后提醒只出现一次，不会累积。"""
        await repo.append_message(db, session_id, Msg(role="user", content="Q1"))
        await repo.append_message(db, session_id, Msg(role="assistant", content="A1"))
        await repo.append_message(db, session_id, Msg(role="user", content="Q2"))
        await repo.append_message(db, session_id, Msg(role="assistant", content="A2"))

        # 第一轮：text 回答
        llm = FakeLLM([text_chunks("回答1")])
        loop = await _loop(db, session_id, llm, _registry())
        await loop.run()

        # 第二轮：再发一条 user 消息
        await repo.append_message(db, session_id, Msg(role="user", content="继续"))
        llm2 = FakeLLM([text_chunks("回答2")])
        loop2 = await _loop(db, session_id, llm2, _registry())
        await loop2.run()

        # 第二轮发送的消息中，提醒只出现在最后一条
        sent = llm2.received[-1]
        reminder_count = sum(
            1 for m in sent if "执行规则" in (m.get("content") or "")
        )
        assert reminder_count == 1, f"提醒只应该出现一次，实际: {reminder_count} 次"
