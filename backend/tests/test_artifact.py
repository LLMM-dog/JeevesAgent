"""
artifact（工作成果）的测试。

artifact 有三条特殊待遇，每条对应一个失败模式：
  1. 不参与压缩  —— 否则用户说"把刚才那份代码改一下"时它已经没了
  2. 只留最新一版 —— 否则改 5 次会在上下文里累积 5 份，互相矛盾
  3. 钉在末尾    —— 按时序插入会埋在中间，模型注意不到
"""

from typing import Any

from app.core.config import settings
from app.modules.agent.loop import AgentLoop
from app.modules.agent.messages import Msg
from app.modules.agent.pathguard import AllowedPath, set_allowed
from app.modules.agent.tools.base import ArtifactPayload, ToolRegistry
from app.modules.agent.tools.file import WriteFileTool
from app.modules.session import repo
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes import FakeLLM, fake_model, text_chunks, tool_call_chunks


class TestArtifactUpsert:
    async def test_only_latest_version_kept(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        改 5 次只留最新一版。累积的话上下文里会有 5 份互相矛盾的代码。
        """
        for i in range(5):
            await repo.append_message(
                db,
                session_id,
                Msg(role="artifact", content=f"第 {i} 版代码"),
                artifact_kind="file",
                artifact_path="a.py",
            )

        rows = await repo.load_messages(db, session_id)
        arts = [r for r in rows if r.role == "artifact"]
        assert len(arts) == 1
        assert arts[0].content == "第 4 版代码"

    async def test_different_agents_keep_separate_artifacts(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        upsert 的粒度是 (session, agent_name)。
        主 agent 和子 agent 的产出不能互相覆盖。
        """
        await repo.append_message(
            db, session_id, Msg(role="artifact", content="主的", agent_name="")
        )
        await repo.append_message(
            db, session_id, Msg(role="artifact", content="子的", agent_name="researcher")
        )

        # load_messages 默认只拉主记忆线（agent_name=""），
        # 要拿全部记忆线必须显式传 None
        rows = await repo.load_messages(db, session_id, agent_name=None)
        arts = [r for r in rows if r.role == "artifact"]
        assert len(arts) == 2
        by_agent = {r.agent_name: r.content for r in arts}
        assert by_agent[""] == "主的"
        assert by_agent["researcher"] == "子的"

    async def test_upsert_does_not_touch_other_roles(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """替换 artifact 不能顺手删掉普通消息。"""
        await repo.append_message(db, session_id, Msg(role="user", content="问"))
        await repo.append_message(db, session_id, Msg(role="assistant", content="答"))
        await repo.append_message(db, session_id, Msg(role="artifact", content="v1"))
        await repo.append_message(db, session_id, Msg(role="artifact", content="v2"))

        rows = await repo.load_messages(db, session_id)
        assert [r.role for r in rows] == ["user", "assistant", "artifact"]
        assert rows[0].content == "问"
        assert rows[-1].content == "v2"


class TestWriteFileProducesArtifact:
    async def test_write_file_declares_artifact(self, tmp_path: Any) -> None:
        from app.modules.agent.tools.base import ToolContext

        ws = tmp_path.resolve()
        set_allowed([AllowedPath(path=ws, can_write=True)])
        tool = WriteFileTool()
        ctx = ToolContext(
            session_id="s",
            run_id="r",
            workspace=ws,
            db=None,  # type: ignore[arg-type]
            llm=None,  # type: ignore[arg-type]
        )

        result = await tool.run(ctx, path="hello.py", content="print(1)")

        assert result.is_error is False
        assert result.artifact is not None
        assert result.artifact.kind == "file"
        assert result.artifact.content == "print(1)"
        assert result.artifact.path == "hello.py"

    async def test_huge_file_not_an_artifact(self, tmp_path: Any) -> None:
        """
        artifact 常驻上下文。几万行的文件会把窗口占满，
        反而挤掉真正需要的历史 —— 那就本末倒置了。
        """
        from app.modules.agent.tools.base import ToolContext

        ws = tmp_path.resolve()
        set_allowed([AllowedPath(path=ws, can_write=True)])
        tool = WriteFileTool()
        ctx = ToolContext(
            session_id="s",
            run_id="r",
            workspace=ws,
            db=None,  # type: ignore[arg-type]
            llm=None,  # type: ignore[arg-type]
        )

        big = "x" * (settings.agent.artifact_max_chars + 1)
        result = await tool.run(ctx, path="big.txt", content=big)

        assert result.is_error is False
        assert result.artifact is None, "超大文件不该当 artifact"
        # 但文件本身要写成功
        assert (ws / "big.txt").read_text(encoding="utf-8") == big

    async def test_edit_file_is_not_an_artifact(self, tmp_path: Any) -> None:
        """零散编辑不算产物 —— 否则每次改一行都覆盖掉完整版本。"""
        from app.modules.agent.tools.base import ToolContext
        from app.modules.agent.tools.file import EditFileTool

        ws = tmp_path.resolve()
        set_allowed([AllowedPath(path=ws, can_write=True)])
        (ws / "a.py").write_text("old line\nkeep\n", encoding="utf-8")

        ctx = ToolContext(
            session_id="s",
            run_id="r",
            workspace=ws,
            db=None,  # type: ignore[arg-type]
            llm=None,  # type: ignore[arg-type]
        )
        result = await EditFileTool().run(
            ctx, path="a.py", old_string="old line", new_string="new line"
        )
        assert result.is_error is False
        assert result.artifact is None


class TestArtifactInLoop:
    async def test_loop_persists_artifact_from_tool(
        self, db: AsyncSession, session_id: str, tmp_path: Any
    ) -> None:
        """工具返回 artifact 时 loop 要落库并放进工作副本。"""
        ws = tmp_path.resolve()
        set_allowed([AllowedPath(path=ws, can_write=True)])
        await repo.append_message(db, session_id, Msg(role="user", content="建文件"))

        reg = ToolRegistry()
        reg.register(WriteFileTool())
        llm = FakeLLM(
            [
                tool_call_chunks(
                    "write_file", '{"path":"m.py","content":"print(2)"}', call_id="c1"
                ),
                text_chunks("建好了"),
            ]
        )
        loop = AgentLoop(
            db=db,
            llm=llm,
            model=fake_model(),
            registry=reg,
            session_id=session_id,
            run_id="run_art",
            workspace=ws,
            system_prompt="sys",
        )
        await loop.load_context()
        await loop.run()

        rows = await repo.load_messages(db, session_id)
        arts = [r for r in rows if r.role == "artifact"]
        assert len(arts) == 1
        assert arts[0].content == "print(2)"
        assert arts[0].artifact_path == "m.py"

        # 工作副本里要有
        assert any(m.role == "artifact" for m in loop.messages)

        # "钉在末尾"是在【组装请求时】保证的，不是在 self.messages 里。
        # 保存后 assistant 回复会追加在后面，所以 messages[-1] 不是 artifact ——
        # 但发给上游的列表里 artifact 必须是最后一条。
        api = loop.build_api_messages()
        assert api[-1]["content"] == "print(2)", "artifact 未钉在请求末尾"

    async def test_artifact_replaced_not_accumulated_in_working_copy(
        self, db: AsyncSession, session_id: str, tmp_path: Any
    ) -> None:
        """
        同一轮里写两次文件，工作副本里只能有一份 artifact。
        累积会让上下文里出现两份矛盾的代码。
        """
        ws = tmp_path.resolve()
        set_allowed([AllowedPath(path=ws, can_write=True)])

        loop = AgentLoop(
            db=db,
            llm=FakeLLM([]),
            model=fake_model(),
            registry=ToolRegistry(),
            session_id=session_id,
            run_id="run_art2",
            workspace=ws,
        )
        await loop.load_context()

        await loop._save_artifact(ArtifactPayload(kind="file", content="v1", path="a.py"))
        await loop._save_artifact(ArtifactPayload(kind="file", content="v2", path="a.py"))

        arts = [m for m in loop.messages if m.role == "artifact"]
        assert len(arts) == 1
        assert arts[0].content == "v2"
        # 钉末尾在组装请求时保证
        api = loop.build_api_messages()
        assert api[-1]["content"] == "v2"


class TestArtifactSurvivesCompaction:
    async def test_artifact_still_present_after_compaction(
        self, db: AsyncSession, session_id: str
    ) -> None:
        """
        压缩后 artifact 必须还在 —— 这是它存在的全部理由。
        """
        from app.modules.agent.compaction import plan_compaction

        msgs = [Msg(role="system", content="sys")]
        for i in range(10):
            msgs.append(Msg(role="user", content=f"问{i}"))
            msgs.append(Msg(role="assistant", content=f"答{i}"))
        msgs.append(Msg(role="artifact", content="重要代码"))

        plan = plan_compaction(msgs, keep_tail_turns=2)
        assert plan is not None
        assert len(plan.pinned) == 1
        assert plan.pinned[0].content == "重要代码"
        # 不在被压缩的候选集里
        assert all(m.role != "artifact" for m in plan.victims)
