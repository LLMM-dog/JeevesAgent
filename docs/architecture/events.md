# 事件系统

事件名与字段的**唯一真源**是 [../api/sse-events.md](../api/sse-events.md)。本文只讲机制。

## 为什么用 ContextVar 事件总线，不用回调

调用链有四层：`router → service → graph → node → tool`。工具内部还可能再调 LLM（如 subagent）。

**回调方案的问题**：`StreamCallback` 要一路当参数往下传。传递链上任何一层忘了传，该层以下的所有事件**静默消失**——没有报错，只是前端少显示了东西。这类 bug 极难发现，因为它只在特定路径上出现。

而且回调通常是同步方法，同步函数里没法 `await`，事件发送就不能做异步 I/O。

**ContextVar 方案**：

```python
_current_bus: ContextVar[EventBus | None] = ContextVar("current_bus", default=None)

def current_bus() -> EventBus | None:
    return _current_bus.get()

async def emit(event: Ev, **data) -> None:
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
    def __init__(self, maxsize: int | None = None):
        self._queue = asyncio.Queue(maxsize=maxsize or settings.agent.event_queue_size)
        self.dropped = 0
        self._closed = False
        self._detached = False

    async def push(self, event: Ev, data: dict) -> None:
        if self._closed or self._detached:
            return
        payload = {"event": str(event), "data": {**data, "ts": ..., "span_id": ...}}
        if event in _DELTA_EVENTS:
            # 增量类（thinking / message）队列满时直接丢弃，不阻塞生成。
            # 丢几个字符用户几乎无感，而阻塞会让整个生成卡住。
            try:
                self._queue.put_nowait(payload)
            except asyncio.QueueFull:
                self.dropped += 1
        else:
            # 结构类要保序入队，可以等 —— 但【不能无限等】，见下。
            try:
                await asyncio.wait_for(
                    self._queue.put(payload),
                    timeout=settings.agent.event_put_timeout,
                )
            except TimeoutError:
                self.dropped += 1
```

注意 `ts` / `span_id` / `parent_span_id` / `depth` 都在 **`data` 内部**，不是 payload 顶层。

`_DELTA_EVENTS = {Ev.THINKING, Ev.MESSAGE}`。只有这两个允许无条件丢 —— 丢了 `agent_end` 前端的气泡树永远转圈，丢了 `tool_end` 那个工具卡片永远停在"执行中"。

`push` 只接 `Ev` 枚举，不接裸字符串。事件名散落成各处的字符串字面量时，很容易出现同名事件两个不同 schema（一处带 `call_id` 一处不带），而前端只能容忍差异。

### detach：没人听的时候不能阻塞

**这是一个真实死锁的修复。** 用户在 auto 模式下切走会话时：

```
1. 前端 abort fetch（只停本地读取，服务端 run 继续 —— 这是有意的，
   用户切走的意图是"让它在后台跑完"）
2. Starlette 取消 SSE 响应任务，消费端不再调 bus.get()
3. 队列很快填满（512 槽位，auto 模式一条长回复的 delta 就够）
4. 下一个结构类事件（tool_start / tool_end / approval_required）
   执行 await queue.put() —— 永久阻塞
5. produce() 的 finally 永不执行
   → run_registry.unregister 不执行
   → task 永远不 done
   → active_run_of() 永远返回它
```

结果是那个会话被**永久锁死**：切回去看不到新输出（agent 卡在第 4 步，不再写库），发消息永远 409（而错误信息说的是"连接中断"），只有重启进程才能恢复。

SSE 生成器退出时调 `bus.detach()`，此后 push 变 no-op，run 继续跑完、继续写库、finally 正常执行。

**detach 必须同时清空队列。** 只置标志位不够 —— 已经卡在 `await put()` 上的协程不会因为标志位变化而醒来，它在等一个永远不会腾出的槽位。

**为什么不是取消 run**：用户切走会话的意图是"让它在后台跑"，不是"停掉它"。取消的话 auto 模式下跑了一半的任务就废了 —— 而那正是他切走去干别的事的原因。

切回来时前端调 `GET /api/sessions/{id}/active-run` 判断后台是否还在跑，然后锁输入框 + 轮询拉增量。见 [../api/endpoints-chat.md](../api/endpoints-chat.md)。

### 入队超时是兜底

`event_put_timeout` 默认 30 秒。detach 覆盖了"消费端正常退出"这条路径，这个超时兜的是其余情况：消费端还在但卡住了（网络极慢的客户端），或者 detach 因为某个异常路径没被调到。

权衡很清楚：无限等的代价是整个 run 永久死锁 + 会话被锁死 + DB 连接泄漏（要重启进程）；丢一个事件的代价是前端某个工具卡片停在"执行中"（刷新一下就好）。

> 记一个实现细节：丢弃时的日志字段**不能叫 `event`** —— structlog 的第一个位置参数就叫 event，传 `event=...` 会撞成 `got multiple values for argument 'event'`。而这个 TypeError 只在异常处理路径里发生，正常跑的时候永远不会暴露。

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

代价是失去 `EventSource` 的自动重连。手动实现指数退避重试，见 [../architecture/frontend-sse.md](../architecture/frontend-sse.md)。

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

1. 先改 [../api/sse-events.md](../api/sse-events.md) 的表，定义事件名与字段
2. 后端 `emit()`
3. 前端 `switch` 加分支
4. 前端的 `default` 分支必须 `console.warn` 未知事件名，不能静默 `break`

第 4 条是兜底：即使有人忘了改前端，开发时也能在控制台立即看到。

还有一层真正的守卫：`backend/tests/test_events_contract.py` 扫描 `Ev` 枚举、
`sse-events.md` 的事件总表、前端 `SseEventMap` 与 store 的 switch，
四者不一致就测试失败。

这个测试之前**不存在**（虽然本文件和 `events.py` 的 docstring 都声称它存在），
于是四个事件悄悄漏出了文档表：`approval_resolved` / `compacting` /
`memory_recalled` / `refs_expanded`。前端处理了、后端在发，只有那份
"唯一真源"不知道。

漏文档的后果不是文档难看 —— 是下一个改这块的人会照着过时的表去改前端 switch，
然后某个事件静默丢失。
