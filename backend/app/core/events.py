"""
事件总线。

## 为什么用 ContextVar 而不是回调

调用链有四层：router → service → graph → node → tool。工具内部还可能再调 LLM。
回调方案要求 StreamCallback 一路当参数往下传 —— 传递链上任何一层忘了传，
该层以下的所有事件【静默消失】：没有报错，只是前端少显示了东西。
这类 bug 极难发现，因为它只在特定路径上出现。

而且回调通常是同步方法，同步函数里没法 await，事件发送就不能做异步 I/O。

## emit() 无订阅者时 no-op 是关键的架构简化

因为 emit() 在没人订阅时什么都不做，所以 agent loop 里不需要 if streaming 判断,
不需要 stream: bool 参数穿透四层，流式和非流式【走完全相同的代码路径】。
两套代码路径一定会不同步，这个设计从根上消除了分叉。

loop.py 判断"要不要流式调 LLM"也用这个：current_bus() is None 即非流式场景。

## 事件名必须用 Ev 枚举，不接受裸字符串

事件名散落成各处的字符串字面量时，很容易出现 tool_error
的两个不同 schema（一处带 call_id 一处不带），前端只能容忍差异。
更严重的情况：后端发 14 种事件前端只处理 6 种，功能等于不存在且零报错。
"""

import asyncio
import contextvars
from enum import StrEnum
from typing import Any

import structlog

from app.core.config import settings
from app.core.time import now_ms
from app.core.trace_context import current_span

log = structlog.get_logger(__name__)


class Ev(StrEnum):
    """
    SSE 事件名。唯一真源是 docs/03-api/sse-events.md，新增必须先改那份表。

    backend/tests/test_events_contract.py 会扫描本枚举与文档表比对，
    多了或少了都会测试失败。
    """

    META = "meta"
    AGENT_START = "agent_start"
    THINKING = "thinking"
    MESSAGE = "message"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    APPROVAL_REQUIRED = "approval_required"
    # 审批有结果时也要发事件：前端要收掉那个弹窗。
    # 只发 required 不发 resolved 的话，超时/取消/在别处批准这几种情况下
    # 弹窗会永远留在界面上。
    APPROVAL_RESOLVED = "approval_resolved"
    INTERACT_REQUIRED = "interact_required"
    TODO_UPDATED = "todo_updated"
    ARTIFACT_UPDATED = "artifact_updated"
    MEMORY_RECALLED = "memory_recalled"
    REFS_EXPANDED = "refs_expanded"
    # 压缩开始/结束分成两个事件：压缩要花一次 LLM 调用（几秒），
    # 只发 compacted 的话用户会看到界面卡住而没有任何解释。
    COMPACTING = "compacting"
    COMPACTED = "compacted"
    CONTEXT_USAGE = "context_usage"
    MODEL_FALLBACK = "model_fallback"
    SANDBOX_FALLBACK = "sandbox_fallback"
    MCP_UNAVAILABLE = "mcp_unavailable"
    AGENT_END = "agent_end"
    TITLE = "title"
    ERROR = "error"
    CANCELLED = "cancelled"
    DONE = "done"
    PING = "ping"


# 增量类事件：队列满时直接丢弃，不阻塞生成。丢几个字符用户几乎无感。
# 其余（结构类）必须保序入队哪怕阻塞 —— 丢了 agent_end 前端气泡树永远转圈，
# 丢了 tool_end 那个工具卡片永远停在"执行中"。
_DELTA_EVENTS = frozenset({Ev.THINKING, Ev.MESSAGE})


class EventBus:
    def __init__(self, maxsize: int | None = None) -> None:
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=maxsize or settings.agent.event_queue_size
        )
        self.dropped = 0
        self._closed = False

    async def push(self, event: Ev, data: dict[str, Any]) -> None:
        if self._closed:
            return

        span = current_span()
        payload = {
            "event": str(event),
            "data": {
                **data,
                "ts": now_ms(),
                "span_id": span.span_id if span else None,
                "parent_span_id": span.parent_span_id if span else None,
                "depth": span.depth if span else 0,
            },
        }

        if event in _DELTA_EVENTS:
            try:
                self._queue.put_nowait(payload)
            except asyncio.QueueFull:
                self.dropped += 1
        else:
            await self._queue.put(payload)

    async def get(self) -> dict[str, Any] | None:
        """返回 None 表示流结束。"""
        return await self._queue.get()

    async def close(self) -> None:
        """放一个哨兵让消费端退出循环。"""
        self._closed = True
        await self._queue.put(None)


_current_bus: contextvars.ContextVar[EventBus | None] = contextvars.ContextVar(
    "current_bus", default=None
)


def current_bus() -> EventBus | None:
    return _current_bus.get()


def set_bus(bus: EventBus | None) -> contextvars.Token[EventBus | None]:
    return _current_bus.set(bus)


def reset_bus(token: contextvars.Token[EventBus | None]) -> None:
    _current_bus.reset(token)


async def emit(event: Ev, **data: Any) -> None:
    """
    发一个事件。无订阅者时静默 no-op —— 这不是防御性编程，是架构简化。
    见模块 docstring。
    """
    bus = _current_bus.get()
    if bus is None:
        return
    await bus.push(event, data)
