"""
端到端测试：真实 HTTP 栈 + SSE 流 + 数据库。

用 ASGITransport 直接打 app，不起真实端口。FakeLLM 通过替换
app.state.chat_service 注入。

这是 M0 的验收测试：走通 建会话 → 对话 → SSE 事件 → 落库 全链路。
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from app.core.ids import binding_id, endpoint_id, model_id
from app.infra.db.base import Base
from app.modules.agent.chat_service import ChatService
from app.modules.agent.tools.base import ToolRegistry
from app.modules.endpoint.models import Endpoint, Model, ModelBinding
from app.modules.session import repo
from app.modules.todo.models import Todo  # noqa: F401
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.fakes import EchoTool, FakeLLM, text_chunks, tool_call_chunks


def parse_sse(raw: str) -> list[tuple[str, dict[str, Any]]]:
    """
    解析 SSE 文本。同时校验协议格式 ——
    data 必须是单行 JSON，裸换行会让前端解析到半个 JSON。
    """
    out: list[tuple[str, dict[str, Any]]] = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        event = ""
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        assert len(data_lines) <= 1, f"data 必须单行，实际 {len(data_lines)} 行"
        if event and data_lines:
            out.append((event, json.loads(data_lines[0])))
    return out


@pytest_asyncio.fixture
async def app_and_maker(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """
    构造一个用临时库的 app，跳过 lifespan 的真实初始化。

    ## 为什么用临时文件库而不是 :memory:

    sqlite+aiosqlite 的 :memory: 走 StaticPool —— 所有 session 共享【同一个
    连接】。E2E 里有多个并存的 session（依赖注入的 get_db、ChatService 自己的
    sessionmaker、生成端 task 里的 session），它们共享一个连接会导致事务互相
    穿插，表现为莫名的 "FOREIGN KEY constraint failed"（另一个 session 的
    未提交事务里看不到刚插入的父行）。

    文件库每个 session 拿到独立连接，与生产一致。
    """
    from app.core.config import settings
    from cryptography.fernet import Fernet

    # 用一个合法的 Fernet key，让加解密能跑
    monkeypatch.setattr(
        settings.security, "encryption_key", Fernet.generate_key().decode()
    )

    db_file = tmp_path / "e2e.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")

    def _pragmas(conn, _rec):  # type: ignore[no-untyped-def]
        cur = conn.cursor()
        # 与生产一致：WAL 让读写不互斥（多 session 并存时必需），
        # foreign_keys 必须显式开（SQLite 默认关闭，不开则级联删除静默失效）
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    event.listen(engine.sync_engine, "connect", _pragmas)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    from app.api import routes_chat, routes_config
    from app.core.exceptions import AppError
    from fastapi import FastAPI
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

    app = FastAPI()

    @app.exception_handler(AppError)
    async def _h(_r, exc: AppError):  # type: ignore[no-untyped-def]
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.to_detail()})

    @app.exception_handler(RequestValidationError)
    async def _v(_r, exc: RequestValidationError):  # type: ignore[no-untyped-def]
        first = exc.errors()[0] if exc.errors() else {}
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "validation_error",
                    "message": "请求参数不合法",
                    "hint": str(first.get("msg", "")),
                }
            },
        )

    app.include_router(routes_chat.router, prefix="/api")
    app.include_router(routes_config.router, prefix="/api")

    # 覆盖 get_db 依赖，指向临时库
    from app.infra.db.session import get_db

    async def _get_db() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_db] = _get_db

    registry = ToolRegistry()
    registry.register(EchoTool())
    app.state.registry = registry
    app.state.chat_service = ChatService(sessionmaker=maker, base_registry=registry)

    yield app, maker
    await engine.dispose()


async def _seed_model(maker: async_sessionmaker[AsyncSession]) -> None:
    """建一个端点 + 模型 + chat 绑定。"""
    from app.core.crypto import encrypt

    async with maker() as db:
        await repo.ensure_default_workspace(db, "/tmp/e2e-ws")
        p = Endpoint(
            id=endpoint_id(),
            name="fake",
            base_url="http://fake/v1",
            api_key_cipher=encrypt("sk-fake"),
            key_hint="fake",
        )
        db.add(p)
        # 每层之间都要 flush。没有 relationship() 时 SQLAlchemy 不保证
        # 父行先插，foreign_keys=ON 下会 IntegrityError。
        await db.flush()

        m = Model(
            id=model_id(),
            endpoint_id=p.id,
            model_id="fake-model",
            context_window=32768,
            window_source="matched",
        )
        db.add(m)
        await db.flush()

        db.add(ModelBinding(id=binding_id(), agent_name="", purpose="chat", model_pk=m.id))
        await db.commit()


def _inject_llm(app: Any, llm: FakeLLM, monkeypatch: pytest.MonkeyPatch) -> None:
    """替换 get_llm，让 chat_service 与 loop 都拿到 FakeLLM。"""
    import app.modules.agent.chat_service as cs
    import app.modules.agent.loop as lp

    monkeypatch.setattr(cs, "get_llm", lambda: llm)
    monkeypatch.setattr(lp, "get_llm", lambda: llm, raising=False)


class TestChatE2E:
    async def test_full_round_trip(
        self, app_and_maker: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, maker = app_and_maker
        await _seed_model(maker)
        _inject_llm(app, FakeLLM([text_chunks("你好，我是助手")]), monkeypatch)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            s = (await c.post("/api/sessions", json={})).json()
            sid = s["id"]

            r = await c.post("/api/chat", json={"session_id": sid, "content": "你好"})
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")

            events = parse_sse(r.text)
            names = [e for e, _ in events]

            # meta 必须第一个 —— 前端拿到 run_id 才能启用取消按钮
            assert names[0] == "meta"
            # done 必须最后一个，且无论成败都要有
            assert names[-1] == "done"
            assert "agent_start" in names
            assert "message" in names
            assert "agent_end" in names

            meta = events[0][1]
            assert meta["run_id"].startswith("run_")
            assert meta["session_id"] == sid
            assert meta["user_message_id"].startswith("msg_")

            done = events[-1][1]
            assert done["status"] == "done"

            # 流式增量拼起来等于完整回复
            text = "".join(d["delta"] for e, d in events if e == "message")
            assert text == "你好，我是助手"

            # 落库校验
            msgs = (await c.get(f"/api/sessions/{sid}/messages")).json()["items"]
            assert [m["role"] for m in msgs] == ["user", "assistant"]
            assert msgs[1]["content"] == "你好，我是助手"
            assert msgs[0]["seq"] < msgs[1]["seq"]

    async def test_tool_call_round_trip(
        self, app_and_maker: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, maker = app_and_maker
        await _seed_model(maker)
        _inject_llm(
            app,
            FakeLLM([tool_call_chunks("echo", '{"text":"hi"}'), text_chunks("好了")]),
            monkeypatch,
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            sid = (await c.post("/api/sessions", json={})).json()["id"]
            r = await c.post("/api/chat", json={"session_id": sid, "content": "echo hi"})
            events = parse_sse(r.text)
            names = [e for e, _ in events]

            assert "tool_start" in names
            assert "tool_end" in names

            ts = next(d for e, d in events if e == "tool_start")
            te = next(d for e, d in events if e == "tool_end")
            # start/end 靠 call_id 关联到同一个前端卡片
            assert ts["call_id"] == te["call_id"]
            assert ts["tool_name"] == "echo"
            assert te["is_error"] is False
            assert te["display"] == {"ok": True}

            msgs = (await c.get(f"/api/sessions/{sid}/messages")).json()["items"]
            assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant"]
            # tool_calls 已解析成对象数组返回，不是 JSON 字符串
            assert isinstance(msgs[1]["tool_calls"], list)
            assert msgs[1]["tool_calls"][0]["name"] == "echo"

    async def test_all_events_carry_span_fields(
        self, app_and_maker: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        每个事件都要带 span 三件套 —— 前端靠它们把扁平事件流还原成气泡树。
        """
        app, maker = app_and_maker
        await _seed_model(maker)
        _inject_llm(
            app,
            FakeLLM([tool_call_chunks("echo", '{"text":"x"}'), text_chunks("ok")]),
            monkeypatch,
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            sid = (await c.post("/api/sessions", json={})).json()["id"]
            r = await c.post("/api/chat", json={"session_id": sid, "content": "x"})
            for name, data in parse_sse(r.text):
                assert "ts" in data, f"{name} 缺 ts"
                assert "span_id" in data, f"{name} 缺 span_id"
                assert "depth" in data, f"{name} 缺 depth"

    async def test_concurrent_run_rejected(
        self, app_and_maker: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        一个会话同时只允许一个 run。前端会禁用发送按钮，
        但后端必须也拦 —— 用户可能开两个标签页。
        """
        import asyncio

        app, maker = app_and_maker
        await _seed_model(maker)

        # 让 LLM 卡住，使第一个 run 保持进行中
        class SlowLLM(FakeLLM):
            async def stream_chat(self, *a: Any, **kw: Any):  # type: ignore[no-untyped-def]
                await asyncio.sleep(5)
                for ch in text_chunks("late"):
                    yield ch

        _inject_llm(app, SlowLLM([]), monkeypatch)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            sid = (await c.post("/api/sessions", json={})).json()["id"]

            async def first() -> None:
                await c.post("/api/chat", json={"session_id": sid, "content": "a"})

            t = asyncio.create_task(first())
            await asyncio.sleep(0.5)
            r2 = await c.post("/api/chat", json={"session_id": sid, "content": "b"})
            assert r2.status_code == 409
            assert r2.json()["detail"]["code"] == "run_in_progress"

            t.cancel()
            with pytest.raises((asyncio.CancelledError, Exception)):
                await t


class TestTodoE2E:
    async def test_todo_written_by_tool_visible_via_api(
        self, app_and_maker: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.modules.agent.tools.todo import TodoWriteTool

        app, maker = app_and_maker
        await _seed_model(maker)
        app.state.registry.register(TodoWriteTool())
        app.state.chat_service = ChatService(
            sessionmaker=maker, base_registry=app.state.registry
        )

        args = json.dumps(
            {
                "todos": [
                    {"content": "第一步", "status": "completed"},
                    {"content": "第二步", "status": "in_progress"},
                    {"content": "第三步"},
                ]
            },
            ensure_ascii=False,
        )
        _inject_llm(
            app, FakeLLM([tool_call_chunks("todo_write", args), text_chunks("已列出")]), monkeypatch
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            sid = (await c.post("/api/sessions", json={})).json()["id"]
            r = await c.post("/api/chat", json={"session_id": sid, "content": "列计划"})
            events = parse_sse(r.text)

            assert "todo_updated" in [e for e, _ in events]
            upd = next(d for e, d in events if e == "todo_updated")
            assert upd["stats"]["total"] == 3
            assert upd["stats"]["completed"] == 1

            todos = (await c.get(f"/api/sessions/{sid}/todos")).json()
            assert len(todos["items"]) == 3
            assert todos["stats"]["in_progress"] == 1

            # 验收关闭
            arch = (await c.post(f"/api/sessions/{sid}/todos/archive")).json()
            assert arch["archived_count"] == 3
            after = (await c.get(f"/api/sessions/{sid}/todos")).json()
            assert after["items"] == []

    async def test_only_one_in_progress_enforced(
        self, app_and_maker: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """两个 in_progress 时第二个降为 pending，并告知模型。"""
        from app.modules.agent.tools.todo import TodoWriteTool

        app, maker = app_and_maker
        await _seed_model(maker)
        app.state.registry.register(TodoWriteTool())
        app.state.chat_service = ChatService(
            sessionmaker=maker, base_registry=app.state.registry
        )

        args = json.dumps(
            {
                "todos": [
                    {"content": "A", "status": "in_progress"},
                    {"content": "B", "status": "in_progress"},
                ]
            },
            ensure_ascii=False,
        )
        _inject_llm(app, FakeLLM([tool_call_chunks("todo_write", args), text_chunks("ok")]), monkeypatch)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            sid = (await c.post("/api/sessions", json={})).json()["id"]
            await c.post("/api/chat", json={"session_id": sid, "content": "x"})
            todos = (await c.get(f"/api/sessions/{sid}/todos")).json()
            assert todos["stats"]["in_progress"] == 1
            assert todos["stats"]["pending"] == 1
