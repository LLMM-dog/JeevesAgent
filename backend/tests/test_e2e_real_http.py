"""
用真实 TCP socket 验证整条链路。

## 与 test_e2e_chat.py 的分工

那个用 ASGITransport 直连 app + FakeLLM，测的是 loop 与落库逻辑。
这个起一个【真实 TCP 服务器】当上游，测 ASGITransport 测不到的东西：

  - httpx 客户端的真实行为（连接池、trust_env、超时）
  - SSE 在真实网络分片下的表现（事件跨包、UTF-8 跨包）
  - base_url 规范化后能真的连上
  - 前端 sse.ts 的解析假设是否成立

## 为什么用裸 asyncio.start_server 而不是 uvicorn

试过 uvicorn，两种方式都失败：

1. 放在 pytest 的 loop 里跑 —— `server.started` 永远不变 True，
   测试静默挂死在 fixture setup（栈顶是 GetQueuedCompletionStatus，
   完全看不出原因）。同样代码在独立脚本里 asyncio.run() 正常。
2. 放独立线程 + 独立 loop 里跑 —— 独立脚本能起来，pytest 里仍然超时。

pytest-asyncio 与 uvicorn 的启动流程有冲突，具体原因没继续追。

裸 `asyncio.start_server` 只有 40 行，不涉及信号处理、不接管 loop，
在 pytest 里完全正常。测试目的（真实 socket）一样达到，
而且少一层不受控的依赖。
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from app.core.ids import binding_id, endpoint_id, model_id, path_id
from app.infra.db.base import Base
from app.modules.endpoint.models import Endpoint, Model, ModelBinding, PathWhitelist
from app.modules.session import repo
from app.modules.todo.models import Todo  # noqa: F401
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# ─────────────────────────── 假上游（裸 TCP） ───────────────────────────

MODELS_PAYLOAD = {
    "object": "list",
    "data": [
        {"id": "openai/gpt-4o-2024-11-20"},
        {"id": "Pro/deepseek-ai/DeepSeek-V3"},
        {"id": "text-embedding-3-large"},
        {"id": "bge-large-zh"},
        {"id": "unknown-custom-model"},
    ],
}

REPLY_TEXT = "好的，我看过了。目录里有源码和文档，中文测试没问题 ✓"


def _sse_data(obj: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


def _tool_call_chunks() -> list[bytes]:
    return [
        _sse_data(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_real_1",
                                    "type": "function",
                                    "function": {"name": "list_dir", "arguments": ""},
                                }
                            ]
                        },
                    }
                ]
            }
        ),
        _sse_data(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '{"path":"."}'}}
                            ]
                        },
                    }
                ]
            }
        ),
        _sse_data({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}),
    ]


def _text_chunks() -> list[bytes]:
    out = [_sse_data({"choices": [{"index": 0, "delta": {"reasoning_content": "先想一下…"}}]})]
    # 逐字发送中文，制造 UTF-8 跨包
    for ch in REPLY_TEXT:
        out.append(_sse_data({"choices": [{"index": 0, "delta": {"content": ch}}]}))
    out.append(_sse_data({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}))
    return out


async def _read_http_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, bytes]:
    """读一个完整的 HTTP 请求，返回 (method, path, body)。"""
    header_blob = await reader.readuntil(b"\r\n\r\n")
    head = header_blob.decode("latin-1")
    request_line = head.split("\r\n", 1)[0]
    parts = request_line.split(" ")
    method, path = parts[0], parts[1]

    length = 0
    for line in head.split("\r\n")[1:]:
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip())
            break
    body = await reader.readexactly(length) if length else b""
    return method, path, body


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        _method, path, body = await _read_http_request(reader)

        if path.endswith("/models"):
            payload = json.dumps(MODELS_PAYLOAD, ensure_ascii=False).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(payload)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + payload
            )
            await writer.drain()
            return

        # chat/completions：流式响应。
        # 不用 Content-Length，靠 Connection: close 标记结束 ——
        # 省掉 chunked 编码的复杂度，httpx 能正确处理。
        req = json.loads(body or b"{}")
        msgs = req.get("messages", [])
        has_tool_result = any(m.get("role") == "tool" for m in msgs)
        last_user = ""
        for m in reversed(msgs):
            if m.get("role") == "user":
                last_user = str(m.get("content", ""))
                break

        chunks = (
            _tool_call_chunks()
            if ("列目录" in last_user and not has_tool_result)
            else _text_chunks()
        )

        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\n"
            b"Connection: close\r\n\r\n"
        )
        await writer.drain()

        for payload in chunks:
            # 【逐字节写】：制造最恶劣的分片，事件边界和 UTF-8 都会被切开。
            # 这是本测试的核心 —— 验证解码器和缓冲逻辑真的正确。
            for i in range(len(payload)):
                writer.write(payload[i : i + 1])
            await writer.drain()

        writer.write(b"data: [DONE]\n\n")
        await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError):
            pass


@pytest_asyncio.fixture
async def upstream() -> AsyncIterator[str]:
    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


# ─────────────────────────── 被测 app ───────────────────────────


@pytest_asyncio.fixture
async def app_client(tmp_path, monkeypatch, upstream):  # type: ignore[no-untyped-def]
    from app.api import routes_chat, routes_config
    from app.core.config import settings
    from app.core.crypto import encrypt
    from app.core.exceptions import AppError
    from app.infra.db.session import get_db
    from app.modules.agent.chat_service import ChatService
    from app.modules.agent.pathguard import AllowedPath, set_allowed
    from app.modules.agent.tools.base import ToolRegistry
    from app.modules.agent.tools.file import ListDirTool, ReadFileTool
    from cryptography.fernet import Fernet
    from fastapi import FastAPI
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

    monkeypatch.setattr(
        settings.security, "encryption_key", Fernet.generate_key().decode()
    )

    ws = (tmp_path / "ws").resolve()
    (ws / "src").mkdir(parents=True)
    (ws / "README.md").write_text("# 测试", encoding="utf-8")

    # 白名单必须与【session 的 workspace 行】指向同一目录。
    #
    # 工具解析相对路径用的是 ctx.workspace，它来自 workspace 表的
    # root_path（不是 settings.workspace_dir —— 那个只用于首次初始化）。
    # 两者不一致时报错是"路径不在白名单内"，看起来像白名单配错了。
    set_allowed([AllowedPath(path=ws, can_write=True)])

    db_file = tmp_path / "e2e_real.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")

    def _pragmas(conn, _rec):  # type: ignore[no-untyped-def]
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    event.listen(engine.sync_engine, "connect", _pragmas)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker() as db:
        await repo.ensure_default_workspace(db, str(ws))

        # 会话级白名单的库记录。
        #
        # session_id=None 表示全局条目 —— 对所有会话生效，
        # 这里用它是因为测试的会话 id 要等下面才生成。
        db.add(
            PathWhitelist(
                id=path_id(),
                session_id=None,
                path=str(ws),
                can_write=1,
                note="e2e 测试工作区",
                builtin=0,
            )
        )
        await db.flush()
        p = Endpoint(
            id=endpoint_id(),
            name="fake-upstream",
            base_url=f"{upstream}/v1",
            api_key_cipher=encrypt("sk-test"),
            key_hint="test",
        )
        db.add(p)
        await db.flush()
        m = Model(
            id=model_id(),
            endpoint_id=p.id,
            model_id="gpt-4o",
            context_window=128_000,
            window_source="matched",
        )
        db.add(m)
        await db.flush()
        db.add(ModelBinding(id=binding_id(), agent_name="", purpose="chat", model_pk=m.id))
        await db.commit()

    app = FastAPI()

    @app.exception_handler(AppError)
    async def _h(_r, exc: AppError):  # type: ignore[no-untyped-def]
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.to_detail()})

    @app.exception_handler(RequestValidationError)
    async def _v(_r, _exc: RequestValidationError):  # type: ignore[no-untyped-def]
        return JSONResponse(
            status_code=422,
            content={
                "detail": {"code": "validation_error", "message": "参数不合法", "hint": None}
            },
        )

    app.include_router(routes_chat.router, prefix="/api")
    app.include_router(routes_config.router, prefix="/api")

    async def _get_db() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_db] = _get_db

    reg = ToolRegistry()
    reg.register(ListDirTool())
    reg.register(ReadFileTool())
    app.state.registry = reg
    app.state.chat_service = ChatService(sessionmaker=maker, base_registry=reg)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", timeout=60.0
    ) as c:
        yield c

    await engine.dispose()


def parse_sse_strict(raw: str) -> list[tuple[str, dict[str, Any]]]:
    """按 frontend/src/lib/sse.ts 的同一套逻辑解析，验证前端假设成立。"""
    out: list[tuple[str, dict[str, Any]]] = []
    for block in raw.split("\n\n"):
        t = block.strip()
        if not t:
            continue
        name = ""
        data_lines: list[str] = []
        for line in t.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        assert len(data_lines) <= 1, f"事件 {name} 的 data 跨了 {len(data_lines)} 行"
        if name and data_lines:
            out.append((name, json.loads(data_lines[0])))
    return out


# ─────────────────────────── 测试 ───────────────────────────


class TestRealNetwork:
    async def test_probe_against_real_socket(
        self, app_client: AsyncClient, upstream: str
    ) -> None:
        r = await app_client.post(
            "/api/endpoints/probe", json={"base_url": upstream, "api_key": "sk-x"}
        )
        assert r.status_code == 200, r.text
        body = r.json()

        # base_url 自动补了 /v1
        assert body["normalized_base_url"] == f"{upstream}/v1"

        by_id = {m["model_id"]: m for m in body["models"]}
        # 带前缀的名字要能匹配到正确窗口
        assert by_id["openai/gpt-4o-2024-11-20"]["context_window"] == 128_000
        assert by_id["openai/gpt-4o-2024-11-20"]["window_source"] == "matched"
        assert by_id["Pro/deepseek-ai/DeepSeek-V3"]["context_window"] == 65_536
        # 嵌入模型要被识别为非对话（bge 名字里没有 embed）
        assert by_id["text-embedding-3-large"]["looks_non_chat"] is True
        assert by_id["bge-large-zh"]["looks_non_chat"] is True
        # 认不出的要标 default，让 UI 能提示用户手动设置
        assert by_id["unknown-custom-model"]["window_source"] == "default"
        # 对话模型排前面
        assert body["models"][0]["looks_non_chat"] is False

    async def test_full_chat_over_real_socket(self, app_client: AsyncClient) -> None:
        sid = (await app_client.post("/api/sessions", json={})).json()["id"]
        r = await app_client.post(
            "/api/chat", json={"session_id": sid, "content": "帮我列目录"}
        )
        assert r.status_code == 200, r.text

        events = parse_sse_strict(r.text)
        names = [n for n, _ in events]

        assert names[0] == "meta"
        assert names[-1] == "done"
        assert "tool_start" in names
        assert "tool_end" in names
        assert "thinking" in names

        # 中文在逐字节分片下必须完整还原 —— 这是本测试最核心的断言
        text = "".join(d["delta"] for n, d in events if n == "message")
        assert text == REPLY_TEXT

        # 工具真的执行了，看到了真实文件
        te = next(d for n, d in events if n == "tool_end")
        assert te["is_error"] is False, f"工具失败: {te['content_preview'][:400]}"
        assert "README.md" in te["content_preview"]

        # 上游没返回 usage，所以必须发估算值并标记出来 ——
        # 不能因为缺 usage 就不发事件（前端进度条会卡住不动）
        cu = [d for n, d in events if n == "context_usage"]
        assert cu, "缺 context_usage 事件"
        assert cu[-1]["is_estimate"] is True
        assert cu[-1]["used_tokens"] > 0

        msgs = (await app_client.get(f"/api/sessions/{sid}/messages")).json()["items"]
        assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant"]
        assert msgs[-1]["content"] == REPLY_TEXT
        # 思维链在【第二轮】（给结论那次），首轮只有 tool_call。
        # 假上游就是这么发的，与真实推理模型的行为一致。
        assert msgs[3]["reasoning"] == "先想一下…"
        assert msgs[1]["reasoning"] is None

    async def test_every_event_has_common_fields(self, app_client: AsyncClient) -> None:
        sid = (await app_client.post("/api/sessions", json={})).json()["id"]
        r = await app_client.post(
            "/api/chat", json={"session_id": sid, "content": "帮我列目录"}
        )
        events = parse_sse_strict(r.text)
        assert events, "没有解析到任何事件"
        for name, data in events:
            for field in ("ts", "span_id", "parent_span_id", "depth"):
                assert field in data, f"{name} 缺 {field}"
            assert isinstance(data["ts"], int) and data["ts"] > 0, f"{name} 的 ts 无效"
