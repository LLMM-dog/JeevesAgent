"""
第三轮反馈：禁用默认模型、追踪会话级、token 计数、favicon、单位文案。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest_asyncio
from app.core.crypto import encrypt
from app.core.ids import binding_id, model_id, provider_id
from app.modules.provider.models import Model, ModelBinding, Provider
from app.modules.trace.models import Run, Span
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
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


@pytest_asyncio.fixture
async def bound_model(db: AsyncSession) -> Model:
    """一个被 chat 位绑定的模型。"""
    p = Provider(
        id=provider_id(),
        name="绑定测试",
        base_url="https://example.com/v1",
        api_key_cipher=encrypt("sk-test-1234567890"),
    )
    db.add(p)
    await db.flush()
    m = Model(
        id=model_id(),
        provider_id=p.id,
        model_id="bound-chat",
        context_window=32768,
        enabled=1,
    )
    db.add(m)
    await db.flush()
    db.add(
        ModelBinding(id=binding_id(), agent_name="", purpose="chat", model_pk=m.id)
    )
    await db.flush()
    return m


class TestCannotDisableBoundModel:
    """
    第 1 条：能禁用默认模型是不对的。

    删除有这个检查，禁用却没有 —— 而后果一样：那个功能位指向一个禁用的
    模型，下次对话报错或静默降级，而用户只是"把一个看起来没在用的
    模型关掉了"，完全联系不起来。

    尤其是对话位：禁用它等于让整个应用不能对话。
    """

    async def test_disable_bound_rejected(
        self, client: AsyncClient, bound_model: Model
    ) -> None:
        r = await client.patch(
            f"/api/models/{bound_model.id}", json={"enabled": False}
        )
        assert r.status_code == 409, f"竟然允许禁用被绑定的模型：{r.status_code}"
        # 错误信息要说清是哪个功能位
        assert "对话" in r.text

    async def test_enable_always_allowed(
        self, client: AsyncClient, bound_model: Model
    ) -> None:
        """启用没有这个限制 —— 启用不会让任何功能不可用。"""
        r = await client.patch(f"/api/models/{bound_model.id}", json={"enabled": True})
        assert r.status_code == 200

    async def test_unbound_can_be_disabled(
        self, client: AsyncClient, db: AsyncSession, bound_model: Model
    ) -> None:
        """没被绑定的照常可以禁用。"""
        free = Model(
            id=model_id(),
            provider_id=bound_model.provider_id,
            model_id="free-one",
            context_window=8192,
            enabled=1,
        )
        db.add(free)
        await db.flush()
        r = await client.patch(f"/api/models/{free.id}", json={"enabled": False})
        assert r.status_code == 200
        assert r.json()["enabled"] is False

    async def test_other_fields_still_patchable(
        self, client: AsyncClient, bound_model: Model
    ) -> None:
        """
        改别名、改窗口不受影响 —— 只有"禁用"会让功能位失效。
        """
        r = await client.patch(
            f"/api/models/{bound_model.id}",
            json={"display_name": "新名字", "context_window": 65536},
        )
        assert r.status_code == 200
        assert r.json()["display_name"] == "新名字"


class TestTraceFollowsSession:
    """
    第 2 条：追踪要和会话绑定，存活与否、查看都是。

    ## 修复前的实测

    run 和 span 有 session_id 但【没有外键】—— 删会话时留在库里成孤儿。
    真实库里 99 条 run 有 29 条的会话已不存在，402 条 span 有 114 条。

    后果：磁盘只增不减（唯一清理手段是按 14 天清，删掉的会话它的 span
    还要占 14 天），而且累计花费统计把已删会话的也算进去。
    """

    async def test_run_has_cascade_fk(self, db: AsyncSession) -> None:
        fks = Run.__table__.foreign_keys
        target = {fk.column.table.name for fk in fks}
        assert "session" in target, "run 没有指向 session 的外键"
        assert any(fk.ondelete == "CASCADE" for fk in fks), "外键不是 CASCADE"

    async def test_span_has_cascade_fk(self, db: AsyncSession) -> None:
        fks = Span.__table__.foreign_keys
        target = {fk.column.table.name for fk in fks}
        assert "session" in target
        assert any(fk.ondelete == "CASCADE" for fk in fks)

    async def test_delete_session_drops_traces(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """删会话必须带走它的追踪记录。"""
        sid = (await client.post("/api/sessions", json={"title": "追踪测试"})).json()[
            "id"
        ]
        run = Run(
            id="run_tracetest000000000000",
            session_id=sid,
            agent_name="",
            status="ok",
            started_at=1,
            turns=1,
        )
        db.add(run)
        await db.flush()
        db.add(
            Span(
                id="spn_tracetest00000000000",
                run_id=run.id,
                session_id=sid,
                kind="llm",
                name="test",
                status="ok",
                started_at=1,
                depth=0,
            )
        )
        await db.flush()

        await client.delete(f"/api/sessions/{sid}")

        left_runs = list(
            (await db.execute(select(Run).where(Run.session_id == sid))).scalars()
        )
        left_spans = list(
            (await db.execute(select(Span).where(Span.session_id == sid))).scalars()
        )
        assert not left_runs, f"删会话后还剩 {len(left_runs)} 条 run"
        assert not left_spans, f"删会话后还剩 {len(left_spans)} 条 span"

    async def test_list_traces_filters_by_session(self, client: AsyncClient) -> None:
        """接口本来就支持按会话过滤 —— 锁住这个能力。"""
        r = await client.get("/api/traces?session_id=ses_nonexistent")
        assert r.status_code == 200
        assert r.json()["items"] == []

    def test_panel_defaults_to_current_session(self) -> None:
        """
        面板默认选中当前会话。

        用户打开追踪几乎总是因为当前这个对话有问题。

        ## 后来改成了两层

        原来是一个"只看当前对话"的开关。改成"先会话列表、点进去看细节"
        之后，默认选中当前会话仍然保留 —— 少一次点击。
        """
        src = (FRONT / "components" / "TracePanel.tsx").read_text(encoding="utf-8")
        assert "pickedSession" in src, "没有会话选择状态"
        assert "api.listTraces(effective" in src, "没有把会话 id 传下去"
        # effective 要进 queryKey，否则切会话不会重新拉
        assert 'queryKey: ["traces", effective' in src
        # 默认选当前会话
        assert "s.session_id === sessionId" in src

    def test_panel_keeps_all_sessions_option(self) -> None:
        """
        能回到全部会话。

        跨会话看花费和失败率是合理需求 —— 而两层结构里"返回"这一步
        没有的话用户就被锁在一个会话里了。
        """
        src = (FRONT / "components" / "TracePanel.tsx").read_text(encoding="utf-8")
        assert "所有对话" in src, "没有返回会话列表的入口"
        assert "setPickedSession(null)" in src

    def test_panel_groups_by_session(self) -> None:
        """
        第一层是会话列表，不是铺开的 run。

        run_id 的后 8 位混在一起没有任何线索说明哪条属于哪个对话 ——
        用户记得的是"那次让它改 calc.py 的对话"，不是 run_38a91c04。
        """
        src = (FRONT / "components" / "TracePanel.tsx").read_text(encoding="utf-8")
        assert "api.traceSessions" in src, "没有按会话汇总的查询"
        # 列表要显示标题和汇总
        assert "s.runs} 次执行" in src

    def test_multiple_runs_expandable(self) -> None:
        """
        能同时展开多条。

        单个 openRun 时展开第二条会自动收起第一条 —— 而对比两次执行
        （"上次成功这次失败，差在哪"）恰恰需要同时看。
        """
        src = (FRONT / "components" / "TracePanel.tsx").read_text(encoding="utf-8")
        assert "openRuns" in src
        assert "Set<string>" in src
        assert "openRuns.has(" in src

    def test_span_bar_shows_timeline(self) -> None:
        """
        span 的横条要有信息量。

        原来只按最慢的一步归一化宽度、所有条都从左边开始 —— 同一行里
        "耗时 200ms"这个数字已经说明了一切，条本身是根装饰线。

        改成甘特式（位置=何时开始，长度=持续多久）之后它能回答
        "哪些步骤串行、哪一步卡住了整个流程"。
        """
        src = (FRONT / "components" / "TracePanel.tsx").read_text(encoding="utf-8")
        assert "offsetPct" in src, "横条没有起始偏移，等于没有信息"
        assert "runStart" in src


class TestTokenCountFollowsSession:
    """第 3 条：切会话后 token 计数没变化。"""

    def test_open_session_resets_usage(self) -> None:
        """
        usage 是上一个会话的上下文占用。openSession 重置了 12 个字段
        却漏了它 —— 切会话后显示的是上一个会话的数字。
        """
        src = (FRONT / "store" / "chat.ts").read_text(encoding="utf-8")
        # 找 openSession 里那个清空的 set({...})
        i = src.index("async openSession(")
        head = src[i : i + 1400]
        assert "usage: null" in head, "openSession 没清 usage"

    def test_restores_usage_from_history(self) -> None:
        """
        context_usage 只在 run 期间发。不恢复的话切到有历史的会话时
        计数是空的 —— 而那个会话已经用掉几千 token，显示空是错的。
        """
        src = (FRONT / "store" / "chat.ts").read_text(encoding="utf-8")
        assert "function restoreUsage" in src
        assert "prompt_tokens" in src
        # 必须解释为什么用 prompt_tokens 而不是两者之和
        assert "completion_tokens 或两者之和都不对" in src

    async def test_session_detail_carries_window(
        self, client: AsyncClient, bound_model: Model
    ) -> None:
        """
        前端要用窗口大小算占用比例。不给的话它只能猜 32K，
        而实际可能是 128K —— 进度条虚高四倍，用户以为快满了。
        """
        sid = (await client.post("/api/sessions", json={"title": "w"})).json()["id"]
        d = (await client.get(f"/api/sessions/{sid}")).json()
        assert "context_window" in d
        assert d["context_window"] == 32768, "该取 chat 位绑定模型的窗口"

    async def test_window_follows_session_model(
        self, client: AsyncClient, db: AsyncSession, bound_model: Model
    ) -> None:
        """会话选了别的模型时，窗口要跟着那个模型。"""
        big = Model(
            id=model_id(),
            provider_id=bound_model.provider_id,
            model_id="big-window",
            context_window=131072,
            enabled=1,
        )
        db.add(big)
        await db.flush()
        sid = (await client.post("/api/sessions", json={"title": "w2"})).json()["id"]
        await client.patch(f"/api/sessions/{sid}", json={"model_pk": big.id})
        d = (await client.get(f"/api/sessions/{sid}")).json()
        assert d["context_window"] == 131072


class TestTokenUnitSpelledOut:
    """
    第 5 条：'5.4Ktok' 看不懂。

    挤在一起、缩写没人认得、还看不出是不是 5.4 千个 token。
    """

    def test_no_tok_abbreviation(self) -> None:
        bad: list[str] = []
        for p in list(FRONT.rglob("*.tsx")) + list(FRONT.rglob("*.ts")):
            for i, ln in enumerate(p.read_text(encoding="utf-8").split("\n"), 1):
                # 注释里提到 "tok" 是在解释这个问题，跳过
                if ln.strip().startswith(("*", "//", "{/*")):
                    continue
                if re.search(r"\btok\b", ln):
                    bad.append(f"{p.name}:{i}")
        assert not bad, f"还有缩写的 tok：{bad}"

    def test_unit_comes_from_formatter(self) -> None:
        """
        单位跟着值走，调用方不再自己拼 —— 那是 5.4Ktok 的来源。
        """
        src = (FRONT / "components" / "TracePanel.tsx").read_text(encoding="utf-8")
        i = src.index("function fmtTok")
        body = src[i : i + 400]
        assert "token" in body, "格式化函数没带单位"

    def test_small_numbers_not_abbreviated(self) -> None:
        """
        1.2K token 比 1200 token 难读。四位数没有阅读负担，
        到五位数才值得缩写。
        """
        src = (FRONT / "components" / "TracePanel.tsx").read_text(encoding="utf-8")
        i = src.index("function fmtTok")
        assert "10000" in src[i : i + 400]


class TestFavicon:
    """第 4 条：标签页图标。"""

    def test_icons_exist(self) -> None:
        pub = ROOT / "frontend" / "public"
        for name in ("favicon-32.png", "apple-touch-icon.png", "icon-512.png"):
            f = pub / name
            assert f.is_file(), f"缺 {name}"
            assert f.stat().st_size > 0

    def test_favicon_is_small(self) -> None:
        """
        源图 1024x1024 有 890KB。直接拿它当 favicon 的话每次开页面
        都要下 890KB 去渲染一个 16px 的图标。
        """
        f = ROOT / "frontend" / "public" / "favicon-32.png"
        assert f.stat().st_size < 20 * 1024, "favicon 太大，说明没缩过"

    def test_html_links_them(self) -> None:
        html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        assert 'href="/favicon-32.png"' in html
        assert 'rel="apple-touch-icon"' in html

    def test_source_moved_out_of_root(self) -> None:
        """1.png 不该留在仓库根上。"""
        assert not (ROOT / "1.png").exists(), "1.png 还在根目录"
        assert (ROOT / "docs" / "assets" / "logo-1024.png").is_file(), "源图没留档"
