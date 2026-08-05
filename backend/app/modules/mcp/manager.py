"""
MCP 连接管理。每个服务器一个常驻 task。

## 为什么需要常驻 task

async context manager 必须在**同一个 task** 里 `__aenter__` 和 `__aexit__`。
跨 task 会触发 anyio 的 cancel scope 错误（`Attempted to exit cancel scope
in a different task than it was entered in`）。

而 MCP 连接天然跨请求：
  - 建立：应用启动时 / 用户在设置页点"连接"
  - 使用：模型调工具时（另一个请求）
  - 关闭：应用关停时 / 用户点"断开"（又一个请求）

三者不在同一个调用栈里。所以必须有个常驻 task 持有这个 context，
其他地方通过队列跟它通信。

`MCPContextHolder`就是这个方案，
它的 `start()` 用 future 等 `__aenter__` 完成、`stop()` 往队列塞
`close` 指令 —— 整个生命周期都在同一个 task 内。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.core.time import now_ms
from app.modules.mcp.config import (
    CALL_TIMEOUT,
    CONNECT_TIMEOUT,
    MAX_RESULT_CHARS,
    RemoteTool,
    ServerConfig,
    sanitize_description,
)

log = structlog.get_logger(__name__)


@dataclass
class ServerState:
    """一个服务器的运行时状态，回给前端展示。"""

    server_id: str
    transport: str
    status: str = "disconnected"  # disconnected | connecting | ready | error
    error: str = ""
    tools: list[RemoteTool] = field(default_factory=list)
    connected_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_id": self.server_id,
            "transport": self.transport,
            "status": self.status,
            "error": self.error,
            "tool_count": len(self.tools),
            "tools": [
                {"name": t.name, "raw_name": t.raw_name, "description": t.description}
                for t in self.tools
            ],
            "connected_at": self.connected_at,
        }


class _Connection:
    """
    单个服务器的连接。生命周期全部在 `_run` 那一个 task 里。

    ## 为什么不直接 await 而要用队列

    见模块 docstring：context manager 必须同 task 进出。
    """

    def __init__(self, cfg: ServerConfig) -> None:
        self.cfg = cfg
        self.state = ServerState(server_id=cfg.server_id, transport=cfg.transport)
        self._session: Any = None
        self._queue: asyncio.Queue[tuple[str, asyncio.Future]] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """
        建立连接。失败时把原因记进 state，不抛异常。

        ## 为什么失败不抛

        调用方是"连接所有服务器"的循环。抛异常的话第一个失败就中断了
        后面的服务器 —— 这正是 缺陷（相关实现
        一个失败全部消失）。
        """
        if self._task and not self._task.done():
            return
        self.state.status = "connecting"
        self.state.error = ""

        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()
        self._task = asyncio.create_task(
            self._run(ready), name=f"mcp-{self.cfg.server_id}"
        )
        try:
            await asyncio.wait_for(asyncio.shield(ready), timeout=CONNECT_TIMEOUT)
        except TimeoutError:
            self.state.status = "error"
            self.state.error = f"连接超时（{CONNECT_TIMEOUT:.0f}s）"
            await self._force_stop()
        except Exception as e:  # noqa: BLE001
            self.state.status = "error"
            self.state.error = str(e)[:400]
            await self._force_stop()

    async def _run(self, ready: asyncio.Future[None]) -> None:
        """常驻 task。进入 context → 拉工具列表 → 等关闭指令 → 退出 context。"""
        cm = None
        try:
            cm = self._make_client()
            streams = await cm.__aenter__()
            # 三种传输返回的元组长度不同：stdio/sse 是 (read, write)，
            # streamable_http 是 (read, write, get_session_id)
            read, write = streams[0], streams[1]

            from mcp import ClientSession

            session_cm = ClientSession(read, write)
            self._session = await session_cm.__aenter__()
            await self._session.initialize()
            await self._load_tools()

            self.state.status = "ready"
            self.state.connected_at = now_ms()
            if not ready.done():
                ready.set_result(None)

            log.info(
                "mcp_connected",
                server=self.cfg.server_id,
                transport=self.cfg.transport,
                tools=len(self.state.tools),
            )

            # 等关闭指令。整个 context 的生命周期都在这个 task 里。
            while True:
                action, fut = await self._queue.get()
                if action == "close":
                    try:
                        await session_cm.__aexit__(None, None, None)
                        await cm.__aexit__(None, None, None)
                        cm = None
                        if not fut.done():
                            fut.set_result(None)
                    except Exception as e:  # noqa: BLE001
                        if not fut.done():
                            fut.set_exception(e)
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("mcp_connect_failed", server=self.cfg.server_id, err=str(e)[:300])
            if not ready.done():
                ready.set_exception(e)
            else:
                self.state.status = "error"
                self.state.error = str(e)[:400]
        finally:
            self._session = None
            if cm is not None:
                # 异常路径上也要退出 context，否则子进程会残留
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001, S110
                    pass

    def _make_client(self) -> Any:
        """按传输类型构造客户端 context manager。"""
        c = self.cfg
        if c.transport == "stdio":
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            # env 传 None 而不是空 dict —— SDK 对 None 会继承当前环境，
            # 传空 dict 会让子进程完全没有 PATH，表现为"命令找不到"
            params = StdioServerParameters(
                command=c.command,
                args=list(c.args),
                env=dict(c.env) if c.env else None,
                cwd=c.cwd or None,
            )
            return stdio_client(params)

        if c.transport == "http":
            from mcp.client.streamable_http import streamablehttp_client

            return streamablehttp_client(c.url, headers=dict(c.headers) or None)

        from mcp.client.sse import sse_client

        return sse_client(c.url, headers=dict(c.headers) or None)

    async def _load_tools(self) -> None:
        """拉工具列表并包装描述。"""
        resp = await self._session.list_tools()
        tools: list[RemoteTool] = []
        for t in resp.tools:
            ann = {}
            raw_ann = getattr(t, "annotations", None)
            if raw_ann is not None:
                ann = (
                    raw_ann.model_dump()
                    if hasattr(raw_ann, "model_dump")
                    else dict(raw_ann)
                )
            tools.append(
                RemoteTool(
                    server_id=self.cfg.server_id,
                    raw_name=t.name,
                    # 描述必须包一层标注来源 + 声明不可信 ——
                    # 它由第三方提供且会进模型上下文
                    description=sanitize_description(t.description or "", self.cfg.server_id),
                    input_schema=t.inputSchema or {"type": "object", "properties": {}},
                    annotations=ann,
                )
            )
        self.state.tools = tools

    async def call(self, raw_name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """
        调用工具，返回 (文本内容, 是否错误)。

        ## 为什么要超时

        规范客户端义务：`Implement timeouts for tool calls`。
        MCP 服务器是外部进程/服务，卡住的话整个 agent 循环一起卡死。
        """
        if self._session is None or self.state.status != "ready":
            return f"MCP 服务器 {self.cfg.server_id} 未连接（{self.state.error or '未知'}）", True

        try:
            res = await asyncio.wait_for(
                self._session.call_tool(raw_name, args), timeout=CALL_TIMEOUT
            )
        except TimeoutError:
            return f"MCP 工具调用超时（{CALL_TIMEOUT:.0f}s）", True
        except Exception as e:  # noqa: BLE001
            return f"MCP 调用失败：{str(e)[:400]}", True

        parts: list[str] = []
        for item in res.content or []:
            kind = getattr(item, "type", "")
            if kind == "text":
                parts.append(getattr(item, "text", ""))
            elif kind == "image":
                # 图片不直接塞进文本 —— base64 会瞬间吃掉几万 token。
                # 只说明有图，让模型知道存在但不看内容。
                mime = getattr(item, "mimeType", "?")
                parts.append(f"[返回了一张图片 {mime}，未注入上下文以节省 token]")
            elif kind == "resource":
                r = getattr(item, "resource", None)
                parts.append(f"[资源 {getattr(r, 'uri', '?')}]\n{getattr(r, 'text', '')}")
            else:
                parts.append(f"[未识别的返回类型 {kind}]")

        text = "\n".join(p for p in parts if p)
        if len(text) > MAX_RESULT_CHARS:
            # 第三方返回多少不受我们控制，必须截断
            text = text[:MAX_RESULT_CHARS] + f"\n…（返回内容过长，已截断到 {MAX_RESULT_CHARS} 字符）"
        return text or "(无返回内容)", bool(getattr(res, "isError", False))

    async def stop(self) -> None:
        """关闭连接。通过队列让常驻 task 自己退出 context。"""
        if self._task is None or self._task.done():
            await self._force_stop()
            return
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        await self._queue.put(("close", fut))
        try:
            await asyncio.wait_for(fut, timeout=10.0)
            await asyncio.wait_for(self._task, timeout=5.0)
        except (TimeoutError, Exception):  # noqa: B014
            await self._force_stop()
        finally:
            self._task = None
            self._session = None
            self.state.status = "disconnected"
            self.state.tools = []

    async def _force_stop(self) -> None:
        """硬取消。正常关闭走不通时兜底，避免子进程残留。"""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass
        self._task = None
        self._session = None


class McpManager:
    """
    所有 MCP 服务器的管理器。

    ## 逐服务器隔离

    这是与 关键分歧。它用 `MultiServerMCPClient.get_tools()`
    一次拿所有服务器的工具，包在一个 try 里 —— 任一服务器
    连不上就 `_tools = []`，**另外几个正常服务器的工具也全消失**。

    这里每个服务器一个 `_Connection`，各自 try。3 个服务器坏 1 个，
    另外 2 个照常工作，坏的那个在设置页显示具体原因。
    """

    def __init__(self) -> None:
        self._conns: dict[str, _Connection] = {}

    async def connect_all(self, configs: list[ServerConfig]) -> None:
        """
        连接所有启用的服务器。

        并发连接 —— 串行的话 5 个服务器每个 3 秒就要等 15 秒。
        用 gather 且 return_exceptions=True，保证一个失败不影响其他。
        """
        tasks = []
        for cfg in configs:
            if not cfg.enabled:
                continue
            if cfg.transport == "stdio" and not cfg.command_approved:
                # 未确认过启动命令的 stdio 服务器【不连】。
                #
                # 规范要求一键配置时必须先让用户看到完整命令并确认 ——
                # 本地 MCP 服务器等于任意代码执行。
                st = ServerState(server_id=cfg.server_id, transport=cfg.transport)
                st.status = "error"
                st.error = "启动命令未经确认。请在设置页查看完整命令并确认后再连接"
                conn = _Connection(cfg)
                conn.state = st
                self._conns[cfg.server_id] = conn
                continue
            conn = _Connection(cfg)
            self._conns[cfg.server_id] = conn
            tasks.append(conn.start())

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        ready = sum(1 for c in self._conns.values() if c.state.status == "ready")
        log.info("mcp_connect_all_done", total=len(self._conns), ready=ready)

    async def disconnect_all(self) -> None:
        await asyncio.gather(
            *(c.stop() for c in self._conns.values()), return_exceptions=True
        )
        self._conns.clear()

    async def reconnect(self, cfg: ServerConfig) -> ServerState:
        """重连单个服务器。设置页改完配置后调。"""
        old = self._conns.pop(cfg.server_id, None)
        if old is not None:
            await old.stop()
        conn = _Connection(cfg)
        self._conns[cfg.server_id] = conn
        if cfg.enabled and not (cfg.transport == "stdio" and not cfg.command_approved):
            await conn.start()
        elif cfg.transport == "stdio" and not cfg.command_approved:
            conn.state.status = "error"
            conn.state.error = "启动命令未经确认"
        return conn.state

    def states(self) -> list[ServerState]:
        return [c.state for c in self._conns.values()]

    def all_tools(self) -> list[tuple[ServerConfig, RemoteTool]]:
        """所有已就绪服务器的工具。"""
        out: list[tuple[ServerConfig, RemoteTool]] = []
        for c in self._conns.values():
            if c.state.status != "ready":
                continue
            out.extend((c.cfg, t) for t in c.state.tools)
        return out

    async def call(self, server_id: str, raw_name: str, args: dict[str, Any]) -> tuple[str, bool]:
        conn = self._conns.get(server_id)
        if conn is None:
            return f"MCP 服务器 {server_id} 不存在", True
        return await conn.call(raw_name, args)


_manager: McpManager | None = None


def get_manager() -> McpManager:
    global _manager
    if _manager is None:
        _manager = McpManager()
    return _manager


async def close_manager() -> None:
    """应用关停时调。不关的话 stdio 子进程会残留。"""
    global _manager
    if _manager is not None:
        await _manager.disconnect_all()
        _manager = None
