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
    """
    一个 run 一个总线，生产端 push、SSE 生成器 get。

    ## detach：没人听的时候不能阻塞

    【这是一个真实死锁的修复】。用户在 auto 模式下切走会话时：

      1. 前端 abort fetch（只停本地读取，服务端 run 继续 —— 这是有意的，
         用户要的就是"让它自己跑"）
      2. Starlette 取消 SSE 响应任务，消费端不再调 get()
      3. 队列很快填满（512 槽位，一条长回复的 delta 就够）
      4. 下一个非增量事件（tool_start / tool_end / approval_required）
         执行 `await queue.put()` —— **永久阻塞**
      5. produce() 的 finally 永不执行 → run_registry.unregister 不执行
         → task 永远不 done → active_run_of() 永远返回它

    结果是那个会话被永久锁死：切回去看不到新输出（agent 卡在第 4 步，
    不再写库），发消息永远 409，而错误信息说的是"连接中断"。
    只有重启进程才能恢复。

    detach() 之后 push 变成 no-op：run 继续跑完、继续写库、
    finally 正常执行、注册表正常释放。用户切回来时从库里读到完整结果。

    ## 为什么不是"取消 run"

    用户切走会话的意图是"让它在后台跑"，不是"停掉它"。取消的话
    auto 模式下跑了一半的任务就废了 —— 而那正是用户切走去干别的事
    的原因。
    """

    def __init__(self, maxsize: int | None = None) -> None:
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=maxsize or settings.agent.event_queue_size
        )
        self.dropped = 0
        self._closed = False
        self._detached = False

    def detach(self) -> None:
        """
        消费端走了。此后 push 全部丢弃，不阻塞生成。

        幂等 —— SSE 生成器的 finally 和异常路径都可能调它。
        """
        self._detached = True
        # 清空队列，让已经阻塞在 put 上的协程立刻得到槽位。
        #
        # 【只 detach 不清空是不够的】：已经卡在 await put() 上的那个
        # 协程不会因为标志位变化而醒来，它在等一个永远不会腾出的槽位。
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    @property
    def detached(self) -> bool:
        return self._detached

    async def push(self, event: Ev, data: dict[str, Any]) -> None:
        if self._closed or self._detached:
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
            # 结构类事件要保序入队，可以等 —— 但【不能无限等】。
            #
            # detach() 覆盖了"消费端正常退出"这条路径。这个超时兜的是
            # 其余情况：消费端还在但卡住了（网络极慢的客户端、
            # 或者 detach 因为某个异常路径没被调到）。
            #
            # 无限等的代价是整个 run 永久死锁 + 会话被锁死 + DB 连接泄漏，
            # 而丢一个事件的代价是前端某个工具卡片停在"执行中"。
            # 后者用户刷新一下就好，前者要重启进程。
            try:
                await asyncio.wait_for(
                    self._queue.put(payload),
                    timeout=settings.agent.event_put_timeout,
                )
            except TimeoutError:
                self.dropped += 1
                # 【字段名不能叫 event】。structlog 的第一个位置参数就叫
                # event，传 event=... 会撞成 "got multiple values for
                # argument 'event'" —— 而这个 TypeError 发生在异常处理
                # 路径里，正常跑的时候永远不会暴露。
                log.warning(
                    "event_dropped_queue_full",
                    event_name=str(event),
                    dropped=self.dropped,
                )

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
