# 事件系统

事件名与字段的**唯一真源**是 [../03-api/sse-events.md](../03-api/sse-events.md)。本文只讲机制。

## 为什么用 ContextVar 事件总线，不用回调

调用链有四层：`router → service → graph → node → tool`。工具内部还可能再调 LLM（如 subagent）。

**回调方案的问题**：`StreamCallback` 要一路当参数往下传。传递链上任何一层忘了传，该层以下的所有事件**静默消失**——没有报错，只是前端少显示了东西。这类 bug 极难发现，因为它只在特定路径上出现。

而且回调通常是同步方法，同步函数里没法 `await`，事件发送就不能做异步 I/O。

**ContextVar 方案**：

```python
_current_bus: ContextVar[EventBus | None] = ContextVar("current_bus", default=None)

def current_bus() -> EventBus | None:
    return _current_bus.get()

async def emit(event: str, **data) -> None:
    bus = _current_bus.get()
    if bus is None:
        return          # 无订阅者时静默 no-op
    await bus.push(event, data)
```

任意深度的任意函数直接 `await emit("tool_start", ...)`，不需要任何参数传递。

## emit() 无订阅者时 no-op

这不是防御性编程，是一个**关键的架构简化**。

因为 `emit()` 在没人订阅时什么都不做，所以：

- agent loop 里不需要 `if streaming: emit(...)` 判断
- 不需要 `stream: bool` 参数穿透四层
- 流式和非流式**走完全相同的代码路径**

`agent/loop.py` 判断"要不要流式调 LLM"时也用这个：`current_bus() is None` 即非流式场景。

两套代码路径一定会不同步。这个设计从根上消除了分叉。

## EventBus

```python
class EventBus:
    def __init__(self, maxsize: int = 512):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)

    async def push(self, event: str, data: dict) -> None:
        payload = {"event": event, "data": data, "ts": now_ms()}
        if event in _DELTA_EVENTS:
            # 增量类事件（thinking / message）队列满时直接丢弃，不阻塞生成。
            # 丢几个字符用户几乎无感，而阻塞会让整个生成卡住。
            try:
                self._queue.put_nowait(payload)
            except asyncio.QueueFull:
                _dropped.inc()
        else:
            # 结构类事件（agent_start/agent_end/tool_start/tool_end）必须保序入队，
            # 哪怕阻塞。丢了 agent_end 前端的气泡树就永远转圈，
            # 丢了 tool_end 那个工具卡片永远停在"执行中"。
            await self._queue.put(payload)
```

`_DELTA_EVENTS = {"thinking", "message"}`。只有这两个允许丢。

## span 三件套

每个事件自动带 `span_id` / `parent_span_id` / `depth`，从 `core/trace_context.py` 的 ContextVar 里取，不需要调用方传。

```python
# 进入一个新的执行单元时
with new_span(kind="tool", name="read_file"):
    ...   # 这里面 emit 的所有事件自动带上新 span_id 和正确的 parent
```

前端据此把扁平的事件流还原成树：

```
run
├─ span(agent:main)
│  ├─ span(llm)          → thinking / message 事件挂这里
│  ├─ span(tool:grep)    → tool_start / tool_end
│  └─ span(tool:subagent)
│     └─ span(agent:sub)
│        ├─ span(llm)
│        └─ span(tool:read_file)
```

**落库的 trace 树与推给前端的气泡树结构同源**——同一套 span 数据，一份写 `span` 表，一份走 SSE。不维护两套。

## 为什么用 SSE 而不是 WebSocket

 用 WebSocket，这里改用 SSE。理由：

| 维度 | SSE | WebSocket |
| --- | --- | --- |
| 方向 | 单向（服务端→客户端） | 双向 |
| 协议 | 普通 HTTP，走现有中间件/CORS | 需单独升级握手 |
| 断线重连 | 浏览器原生支持（用 `EventSource` 时） | 要自己实现 |
| 与 FastAPI 集成 | `StreamingResponse` 即可 | 需 `WebSocket` 端点 + 连接管理 |
| 调试 | `curl` 直接看 | 需专门工具 |

本项目的数据流**本质上是单向的**：agent 往外推事件。少量的反向通信（审批、取消、交互回答）用独立的 POST 端点更清晰——它们是有明确请求/响应语义的操作，塞进 WebSocket 消息里反而要自己发明一套 request-id 匹配机制。

 用 WebSocket 的代价可以从它的 `dev_docs/notes/fix-staticfiles-websocket.md` 看出来——WebSocket 与静态文件服务的冲突需要专门修。

## POST + SSE，不能用 EventSource

这是本项目最重要的一条前后端契约。

`POST /api/chat` 的请求体里有消息内容、引用列表、模式开关，**必须是 POST**。而浏览器的 `EventSource` API 只能发 GET 请求。

所以前端必须手写解析：

```typescript
const resp = await fetch("/api/chat", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(payload),
  signal: abortController.signal,
});
const reader = resp.body!.getReader();
const decoder = new TextDecoder();
// 按 event: / data: 逐行解析，空行触发 dispatch
```

**这里容易踩坑**：后端是 POST，前端 `useStreamResponse.ts` 写的是 `method: "GET"`，整条链路根本跑不通。

代价是失去 `EventSource` 的自动重连。手动实现指数退避重试，见 [../04-frontend/sse.md](../04-frontend/sse.md)。

## 响应头

```python
headers = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # 反代（nginx）默认会缓冲响应，导致流式变成"等全部结束一次性给出"。
    # 本项目虽然通常直连，但加上这个头没有代价。
    "X-Accel-Buffering": "no",
}
```

## JSON 编码必须 ensure_ascii=False

```python
def sse_encode(event: str, data: dict) -> str:
    # ensure_ascii=False：默认的 True 会把中文转成 \uXXXX 转义序列，
    # 一个汉字从 3 字节变成 6 字节，中文对话的流量直接翻倍。
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {body}\n\n"
```

数据里不能有裸换行——`data:` 字段遇到换行即结束。`json.dumps` 会把换行转义成 `\n` 字面量，所以安全。**但不要手拼 SSE 数据体。**

## 心跳

每 15 秒发一个 `ping` 事件。没有心跳时，长推理阶段（可能 200s+ 不吐字节）会被中间层或浏览器判为连接超时。

`ping` 事件前端直接忽略，不需要处理。

## 新增事件的流程

违反这个流程就会出现"后端发 14 种事件，前端只处理 6 种"的脱节：

1. 先改 [../03-api/sse-events.md](../03-api/sse-events.md) 的表，定义事件名与字段
2. 后端 `emit()`
3. 前端 `switch` 加分支
4. 前端的 `default` 分支必须 `console.warn` 未知事件名，不能静默 `break`

第 4 条是兜底：即使有人忘了改前端，开发时也能在控制台立即看到。
