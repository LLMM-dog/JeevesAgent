"""
第四轮反馈：上下文条分段、span 时间轴、多条同时展开、追踪按会话分层、
标题兜底。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest_asyncio
from app.core.crypto import encrypt
from app.core.ids import binding_id, endpoint_id, model_id
from app.modules.endpoint.models import Endpoint, Model, ModelBinding
from app.modules.session import repo
from app.modules.trace.models import Run
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / "frontend" / "src"


@pytest_asyncio.fixture
async def client(db: AsyncSession, workspace_id: str) -> Any:
    from app.api import deps
    from app.infra.db.session import get_db
    from app.main import create_app
    from app.modules.agent.tools.base import ToolRegistry

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[deps.get_registry] = lambda: ToolRegistry()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    app.dependency_overrides.clear()


class TestTitleFallback:
    """
    第 5 条：标题没生成时用回复的前 20 字。

    ## 为什么需要

    标题位没配模型、或上游报错时，会话永远停在"未命名会话"。侧栏里
    几十条都叫这个名字，等于没有标题功能 —— 而用户不知道这是
    "标题模型没配"导致的。
    """

    def _svc(self) -> Any:
        """
        只用来调 _fallback_title / _generate_title，
        所以 sessionmaker 传 None 就够 —— 那两个方法只用传入的 db。
        """
        from app.infra.db.session import get_sessionmaker
        from app.modules.agent.chat_service import ChatService
        from app.modules.agent.tools.base import ToolRegistry

        return ChatService(
            sessionmaker=get_sessionmaker(), base_registry=ToolRegistry()
        )

    async def test_fallback_used_when_generation_fails(
        self, db: AsyncSession, workspace_id: str
    ) -> None:
        """
        没配标题模型时也要有标题。

        _generate_title 里 resolve 会抛 NoModelBoundError，
        然后应该走兜底。
        """
        s = await repo.create_session(db, workspace_id=workspace_id, title="")
        svc = self._svc()
        await svc._generate_title(
            db, s.id, fallback_text="已经修好 calc.py 的第 5 行，输出 OK 7。"
        )
        again = await repo.get_session(db, s.id)
        assert again.title, "标题还是空的 —— 兜底没生效"
        assert "calc.py" in again.title

    async def test_takes_about_20_chars(
        self, db: AsyncSession, workspace_id: str
    ) -> None:
        """
        20 字。侧栏大约能显示这么多，再长会被截成省略号 ——
        多存的部分只是白占地方。
        """
        s = await repo.create_session(db, workspace_id=workspace_id, title="")
        long = "这是一段很长的回复" * 10
        await self._svc()._fallback_title(db, s.id, long)
        again = await repo.get_session(db, s.id)
        assert 0 < len(again.title) <= 20, f"长度不对：{len(again.title)}"

    async def test_cuts_at_punctuation(
        self, db: AsyncSession, workspace_id: str
    ) -> None:
        """
        在标点处收尾。

        直接切 20 字会切在词中间，读起来是残句。
        """
        s = await repo.create_session(db, workspace_id=workspace_id, title="")
        await self._svc()._fallback_title(
            db, s.id, "已经修好了。接下来我会继续检查其它文件的问题。"
        )
        again = await repo.get_session(db, s.id)
        assert again.title.endswith(("。", "！", "？", "；")) or len(
            again.title
        ) == 20, f"没在标点处收尾：{again.title!r}"

    async def test_strips_code_fence(
        self, db: AsyncSession, workspace_id: str
    ) -> None:
        """
        回复常以 ```python 开头。那些符号当标题毫无信息，
        而它们会把真正有用的文字挤到 20 字之外。
        """
        s = await repo.create_session(db, workspace_id=workspace_id, title="")
        await self._svc()._fallback_title(
            db, s.id, "```python\nprint('hi')\n```\n这段代码打印 hi"
        )
        again = await repo.get_session(db, s.id)
        assert "```" not in again.title
        assert again.title.strip(), "只剩空的了"

    async def test_strips_markdown_marks(
        self, db: AsyncSession, workspace_id: str
    ) -> None:
        s = await repo.create_session(db, workspace_id=workspace_id, title="")
        await self._svc()._fallback_title(db, s.id, "## 检查结果\n\n- 第一项没问题")
        again = await repo.get_session(db, s.id)
        assert not again.title.startswith(("#", "-", "*"))

    async def test_empty_text_leaves_title_alone(
        self, db: AsyncSession, workspace_id: str
    ) -> None:
        """
        空回复不该写一个空标题 —— 那样侧栏显示的是空白，
        比"未命名会话"更糟。
        """
        s = await repo.create_session(db, workspace_id=workspace_id, title="")
        await self._svc()._fallback_title(db, s.id, "   \n\n```\n```\n")
        again = await repo.get_session(db, s.id)
        assert again.title == ""


class TestTraceSessionSummaries:
    """第 4 条：追踪先按会话分组。"""

    async def test_endpoint_returns_grouped(
        self, client: AsyncClient, db: AsyncSession, workspace_id: str
    ) -> None:
        s = await repo.create_session(db, workspace_id=workspace_id, title="测试对话")
        db.add(
            Run(
                id="run_grouptest00000000000",
                session_id=s.id,
                agent_name="",
                status="ok",
                started_at=1000,
                turns=2,
                total_tokens=500,
                cost_usd=0.001,
            )
        )
        await db.flush()

        r = await client.get("/api/traces-sessions")
        assert r.status_code == 200
        items = r.json()["items"]
        mine = [x for x in items if x["session_id"] == s.id]
        assert mine, "汇总里没有这个会话"
        assert mine[0]["title"] == "测试对话", "没带会话标题"
        assert mine[0]["runs"] == 1
        assert mine[0]["total_tokens"] == 500

    async def test_excludes_subagent_runs(
        self, client: AsyncClient, db: AsyncSession, workspace_id: str
    ) -> None:
        """
        只统计顶层 run。

        子 agent 的 run 有 parent_run_id，计进去会让"执行次数"翻倍 ——
        而用户理解的一次执行是"我发了一条消息"。
        """
        s = await repo.create_session(db, workspace_id=workspace_id, title="有委派")
        db.add(
            Run(
                id="run_parent0000000000000",
                session_id=s.id,
                agent_name="",
                status="ok",
                started_at=2000,
                turns=1,
            )
        )
        await db.flush()
        db.add(
            Run(
                id="run_child00000000000000",
                session_id=s.id,
                parent_run_id="run_parent0000000000000",
                agent_name="researcher",
                status="ok",
                started_at=2100,
                turns=1,
            )
        )
        await db.flush()

        items = (await client.get("/api/traces-sessions")).json()["items"]
        mine = [x for x in items if x["session_id"] == s.id]
        assert mine[0]["runs"] == 1, "子 agent 的 run 被算进去了"

    async def test_counts_errors(
        self, client: AsyncClient, db: AsyncSession, workspace_id: str
    ) -> None:
        """失败次数要单独报 —— 用户找的往往就是出错那次。"""
        s = await repo.create_session(db, workspace_id=workspace_id, title="有失败")
        for i, status in enumerate(("ok", "error")):
            db.add(
                Run(
                    id=f"run_errtest{i}0000000000000"[:24],
                    session_id=s.id,
                    agent_name="",
                    status=status,
                    started_at=3000 + i,
                    turns=1,
                )
            )
        await db.flush()
        items = (await client.get("/api/traces-sessions")).json()["items"]
        mine = [x for x in items if x["session_id"] == s.id]
        assert mine[0]["errors"] == 1


class TestContextBarSegments:
    """第 1 条：上下文条分段着色 + 一直显示。"""

    def test_component_exists(self) -> None:
        assert (FRONT / "components" / "ContextBar.tsx").is_file()

    def test_three_segments_with_percent(self) -> None:
        src = (FRONT / "components" / "ContextBar.tsx").read_text(encoding="utf-8")
        for label in ("工具定义", "系统提示词", "对话内容"):
            assert label in src, f"缺 {label} 分段"
        # 每段要有百分比，且【分母是窗口】
        assert "const pct = (n: number) => (n / win) * 100" in src, (
            "百分比不是按窗口算的 —— 分项之间互比会让用户以为窗口快满了"
        )

    def test_has_legend(self) -> None:
        """
        图例要一直显示，不能只靠 hover —— 用户不会去 hover
        一个他认为是坏的数字。
        """
        src = (FRONT / "components" / "ContextBar.tsx").read_text(encoding="utf-8")
        assert "inline-block h-2 w-2" in src, "没有颜色图例块"

    def test_composer_uses_it(self) -> None:
        src = (FRONT / "components" / "Composer.tsx").read_text(encoding="utf-8")
        assert "<ContextBar" in src
        # 旧的单色条不该还在
        assert "usage.ratio > 0.75" not in src, "旧的单色条逻辑还在"

    def test_window_from_store(self) -> None:
        """
        窗口大小要单独存 —— 没有 usage 时也得显示它。
        """
        src = (FRONT / "store" / "chat.ts").read_text(encoding="utf-8")
        assert "contextWindow" in src
        assert "contextWindow: session.context_window" in src

    def test_window_updates_on_model_switch(self) -> None:
        """
        换成 65K 的模型后条还按 131K 画的话，占用比例显示成一半，
        用户以为还很空。
        """
        src = (FRONT / "store" / "chat.ts").read_text(encoding="utf-8")
        assert "contextWindow: s.context_window" in src


@pytest_asyncio.fixture
async def bound(db: AsyncSession) -> Model:
    p = Endpoint(
        id=endpoint_id(),
        name="r4",
        base_url="https://example.com/v1",
        api_key_cipher=encrypt("sk-test-r4-000000000000"),
    )
    db.add(p)
    await db.flush()
    m = Model(
        id=model_id(),
        endpoint_id=p.id,
        model_id="r4-model",
        context_window=65536,
        enabled=1,
    )
    db.add(m)
    await db.flush()
    db.add(
        ModelBinding(id=binding_id(), agent_name="", purpose="chat", model_pk=m.id)
    )
    await db.flush()
    return m


async def test_session_detail_has_window(client: AsyncClient, bound: Model) -> None:
    """上下文条要靠它在发消息之前显示窗口大小。"""
    sid = (await client.post("/api/sessions", json={"title": "w"})).json()["id"]
    d = (await client.get(f"/api/sessions/{sid}")).json()
    assert d["context_window"] == 65536
