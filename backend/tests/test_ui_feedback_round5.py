"""
第五轮反馈：标题不刷新、追踪默认进了详情、百分比分母错、固定开销不常驻。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest_asyncio
from app.core.crypto import encrypt
from app.core.ids import binding_id, endpoint_id, model_id
from app.modules.endpoint.models import Endpoint, Model, ModelBinding
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
    from app.modules.agent.tools.file import ListDirTool, ReadFileTool

    reg = ToolRegistry()
    reg.register(ReadFileTool())
    reg.register(ListDirTool())

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[deps.get_registry] = lambda: reg
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def bound(db: AsyncSession) -> Model:
    p = Endpoint(
        id=endpoint_id(),
        name="r5",
        base_url="https://example.com/v1",
        api_key_cipher=encrypt("sk-test-r5-000000000000"),
    )
    db.add(p)
    await db.flush()
    m = Model(
        id=model_id(),
        endpoint_id=p.id,
        model_id="r5-model",
        context_window=131072,
        enabled=1,
    )
    db.add(m)
    await db.flush()
    db.add(
        ModelBinding(id=binding_id(), agent_name="", purpose="chat", model_pk=m.id)
    )
    await db.flush()
    return m


class TestSidebarRefreshesTitle:
    """
    第 1 条：对话后侧栏仍显示"未命名会话"。

    ## 根因

    首轮结束时后端发 title 事件，store 里的 title 更新了 —— 但侧栏读的是
    ["sessions"] 这个 query 的缓存，它不会因为 store 变化而重新拉。
    于是列表里一直是"未命名会话"，要刷新页面或切一次会话才变。

    标题生成本身是好的（上一轮验过兜底），坏的是列表不刷新。
    """

    def test_sidebar_watches_store_title(self) -> None:
        src = (FRONT / "components" / "Sidebar.tsx").read_text(encoding="utf-8")
        assert "useChatStore" in src, "侧栏没订阅 store"
        assert "s.title" in src, "没监听标题"
        # 必须 invalidate 会话列表
        i = src.index("liveTitle")
        body = src[i : i + 600]
        assert 'invalidateQueries({ queryKey: ["sessions"] })' in body

    def test_store_still_sets_title(self) -> None:
        """store 自己也要更新 —— 顶栏用的是它。"""
        src = (FRONT / "store" / "chat.ts").read_text(encoding="utf-8")
        assert "set({ title: d.title })" in src


class TestTraceStartsAtList:
    """
    第 2 条：点追踪直接进了当前会话，没看到会话列表。

    上一版默认选中当前会话想的是"少一次点击"。但那样分组就白做了 ——
    用户看到的还是一个会话的 run 列表，和改之前没区别，
    他甚至不知道上面还有一层。
    """

    def test_no_auto_select(self) -> None:
        src = (FRONT / "components" / "TracePanel.tsx").read_text(encoding="utf-8")
        assert "const effective = pickedSession;" in src, (
            "还在自动选中会话 —— 用户看不到列表层"
        )

    def test_current_session_marked(self) -> None:
        """
        不自动进去，但要标出来 —— 列表按时间排，
        用户未必认得出哪条是自己正在聊的。
        """
        src = (FRONT / "components" / "TracePanel.tsx").read_text(encoding="utf-8")
        assert "s.session_id === sessionId" in src
        assert "当前" in src


class TestPercentAgainstWindow:
    """
    第 3 条：百分比该按窗口算，不是分项互比。

    之前显示"工具定义 70%"——那是它在【已用部分】里的占比。用户看到
    70% 的第一反应是"窗口快被工具吃满了"，而 3188 / 131072 只有 2.4%，
    空间非常充裕。
    """

    def test_denominator_is_window(self) -> None:
        src = (FRONT / "components" / "ContextBar.tsx").read_text(encoding="utf-8")
        assert "const pct = (n: number) => (n / win) * 100" in src
        # 不该再有以 used 为分母的写法
        assert "/ usage.used_tokens) * 100" not in src

    def test_small_values_not_shown_as_zero(self) -> None:
        """
        小于 0.1% 时显示 "<0.1%"。显示 "0.0%" 看起来像"没有"，
        而用户正在找这个数字。
        """
        src = (FRONT / "components" / "ContextBar.tsx").read_text(encoding="utf-8")
        assert "<0.1%" in src

    def test_hint_triggers_on_window_share(self) -> None:
        """
        提示的判据要用"占窗口比例"。

        用"占已用部分比例"会在每个新会话都触发 —— 那时对话内容是 0，
        固定开销必然占 100%。
        """
        src = (FRONT / "components" / "ContextBar.tsx").read_text(encoding="utf-8")
        assert "(tools + system) / win > 0.15" in src


class TestFixedOverheadAlwaysShown:
    """
    第 4 条：固定开销应该常驻。

    工具定义和系统提示词在发消息之前就确定了 —— 工具集和人格文件都是
    配置，不随对话变。之前只有 run 期间的事件带这两项，
    切一次页面就只剩对话内容，看起来像固定开销凭空消失。
    """

    async def test_endpoint_returns_overhead(
        self, client: AsyncClient, bound: Model
    ) -> None:
        r = await client.get("/api/context-overhead")
        assert r.status_code == 200
        d = r.json()
        assert d["tools_tokens"] > 0, "工具定义算出 0"
        assert d["system_tokens"] > 0, "系统提示词算出 0"
        assert d["tool_count"] == 2, f"工具数不对：{d['tool_count']}"
        assert d["window_tokens"] == 131072
        # 本地分词器的数，必须标明
        assert d["is_estimate"] is True

    async def test_window_follows_session_model(
        self, client: AsyncClient, db: AsyncSession, bound: Model
    ) -> None:
        """会话选了别的模型时，窗口要跟着那个模型。"""
        small = Model(
            id=model_id(),
            endpoint_id=bound.endpoint_id,
            model_id="small-window",
            context_window=8192,
            enabled=1,
        )
        db.add(small)
        await db.flush()
        sid = (await client.post("/api/sessions", json={"title": "w"})).json()["id"]
        await client.patch(f"/api/sessions/{sid}", json={"model_pk": small.id})

        d = (await client.get(f"/api/context-overhead?session_id={sid}")).json()
        assert d["window_tokens"] == 8192

    async def test_no_model_returns_zero_window(self, client: AsyncClient) -> None:
        """
        没配模型时窗口返回 0，前端回落到默认值。

        抛错的话上下文条整块消失 —— 而那时用户最需要看到的是
        "你还没配模型"，不是一个 500。
        """
        r = await client.get("/api/context-overhead")
        assert r.status_code == 200
        assert r.json()["window_tokens"] == 0

    def test_bar_uses_endpoint(self) -> None:
        src = (FRONT / "components" / "ContextBar.tsx").read_text(encoding="utf-8")
        assert "api.contextOverhead" in src
        # 没有实测数据时用估算值填分项
        assert "overhead?.tools_tokens" in src

    def test_live_data_wins(self) -> None:
        """
        有实测就用实测 —— 本地估算比模型的分词器偏高 30%，
        两者并存时该信模型的。
        """
        src = (FRONT / "components" / "ContextBar.tsx").read_text(encoding="utf-8")
        assert "hasLive" in src

    def test_overhead_invalidated_on_config_change(self) -> None:
        """
        改了 MCP 或技能后固定开销会变，缓存要失效 ——
        不失效的话条上显示的还是旧值，而用户刚刚才改完。
        """
        for name in ("McpPanel.tsx", "SkillsPanel.tsx"):
            src = (FRONT / "components" / name).read_text(encoding="utf-8")
            assert "contextOverhead" in src, f"{name} 改完没让固定开销失效"

    def test_label_says_fixed_when_no_live_data(self) -> None:
        """
        没有实测时要说清这是固定部分，否则用户会以为
        "才用了 3%"已经包括了他的对话。
        """
        src = (FRONT / "components" / "ContextBar.tsx").read_text(encoding="utf-8")
        assert "固定开销" in src
