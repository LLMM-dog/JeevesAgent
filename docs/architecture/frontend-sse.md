# SSE 消费

事件定义见 [../api/sse-events.md](../api/sse-events.md)。本文只讲前端怎么消费。

## 必须用 fetch，不能用 EventSource

**这是本项目最容易搞错的一点。**

`POST /api/chat` 的请求体里有消息内容、引用列表、附件 ID。浏览器的 `EventSource` API **只能发 GET 请求**，没有任何办法给它加 body。

所以必须手写：

```typescript
export async function streamChat(
  payload: ChatRequest,
  handlers: EventHandlers,
  signal: AbortSignal,
): Promise<void> {
  const resp = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(payload),
    signal,
  });

  // 流开始前的错误仍是普通 HTTP 错误（404 / 400 / 409）。
  // 一旦响应头发出，此后的错误只能走 error 事件。
  if (!resp.ok) {
    const body = await resp.json().catch(() => null);
    throw new ApiError(resp.status, body?.detail?.code, body?.detail?.message, body?.detail?.hint);
  }

  const reader = resp.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // 按空行切分完整事件
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";        // 最后一段可能不完整，留在 buffer
    for (const part of parts) {
      const parsed = parseEvent(part);
      if (parsed) dispatch(parsed, handlers);
    }
  }
}
```

### 三个必须注意的细节

**1. `decoder.decode(value, { stream: true })` 的 `stream: true` 不能省。**

UTF-8 的中文字符是 3 字节。一个 chunk 可能在字符中间被切断，此时前 1~2 字节单独解码会得到 `�`。`stream: true` 让 decoder 保留不完整的字节序列到下一次调用。

不加这个参数，中文流式输出会随机出现乱码字符——而且是间歇性的，很难复现。

**2. `buffer = parts.pop()` 保留不完整的尾部。**

一个 SSE 事件可能跨多个 chunk 到达。切分后最后一段如果不是以 `\n\n` 结尾就是不完整的，必须留到下次。

这一点值得注意。

**3. `parts.pop() ?? ""` 的空值处理。**

`split` 永不返回空数组，但 TS 不知道。

## 事件解析

```typescript
function parseEvent(raw: string): { event: string; data: any } | null {
  let event = "message";     // SSE 规范的默认事件名
  const dataLines: string = ;

  for (const line of raw.split("\n")) {
    if (line.startsWith(":")) continue;              // 注释行
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }

  if (dataLines.length === 0) return null;
  try {
    return { event, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    console.error("[sse] JSON 解析失败:", dataLines.join("\n"));
    return null;
  }
}
```

后端保证 `data` 是单行 JSON，但按规范支持多行拼接更稳妥。

**JSON 解析失败不能抛异常**——一个坏事件不该终止整个流。记录并跳过。

## 事件分发

实现在 `store/chat.ts` 的 `handleEvent()`，一个 switch on `SseEventName`。

**不是 handlers 对象。** 早期设计是"所有字段必需的 EventHandlers interface"，靠 TS 在 handler 定义处报错来强制处理新事件。实际走的是 store 里的 switch + `SseEventMap` 类型 —— 漏了 case 时 TS 不报错（switch 不要求穷尽），所以守卫改成了后端的契约测试。

### 必须处理的事件

`SseEventMap` 声明了全部 25 个事件，store 的 switch 处理其中 22 个。三个明确不处理：

| 不处理 | 原因 |
| --- | --- |
| `ping` | 心跳，显式 `break` |
| `meta` | 在 `sse.ts` 里消费，拿 `run_id` |
| `interact_required` / `sandbox_fallback` / `mcp_unavailable` | 枚举里有定义但后端从没 emit 过 —— 降级提示改成走 `GET /api/meta` 的字段 |

`default` 分支**绝不静默 break**，一定 `console.warn`。后端加了事件而前端没跟上时，这行是运行时唯一的发现途径。

一条教训：曾经后端发 14 种事件而前端只处理 6 种，气泡树功能等于不存在，且完全没有报错。

### 守卫测试

`backend/tests/test_events_contract.py` 扫描四处：`Ev` 枚举、`docs/api/sse-events.md` 的事件总表、`SseEventMap`、store 的 switch。不一致就失败。

这个测试之前不存在（虽然 `events.py` 的 docstring 声称它存在），于是四个事件悄悄漏出了文档：`approval_resolved` / `compacting` / `memory_recalled` / `refs_expanded`。

### 几个容易漏的事件

| 事件 | 漏了会怎样 |
| --- | --- |
| `approval_resolved` | 审批弹框一直挂着。超时（视为拒绝）时用户那边不会有任何变化，看起来像卡死 |
| `compacting` | 压缩要调一次 LLM，可能几秒。不显示"正在压缩"的话界面看起来卡住了 |
| `memory_recalled` | 记忆是自动注入的。不显示的话用户不知道回答受了什么影响，更不知道某条错误记忆正在生效 |
| `refs_expanded` | `failures` 非空时不提示的话，用户打了 `@某文件` 却没生效，他只会觉得"AI 没看我给的文件" |

`refs_expanded` 成功时**不提示** —— 那是预期行为，弹一条"引用成功"只是噪音。

## 取消

```typescript
const controller = new AbortController();

// 发起
streamChat(payload, handlers, controller.signal);

// 用户点停止
async function cancel(runId: string) {
  // 顺序很重要：先通知后端，再断开本地连接。
  // 反过来的话，后端还在生成（它不知道客户端走了），
  // 会继续消耗 token 直到写不进去才发现。
  await api.post(`/api/runs/${runId}/cancel`);
  controller.abort();
}
```

`abort()` 会让 `reader.read()` 抛 `AbortError`，需要捕获：

```typescript
try {
  await streamChat(...);
} catch (err) {
  if (err instanceof DOMException && err.name === "AbortError") {
    // 正常取消，不是错误
    return;
  }
  throw err;
}
```

**`run_id` 从 `meta` 事件拿。** `meta` 之前用户点停止只能 `abort()`，无法通知后端——这个窗口只有几十毫秒，可接受。前端在收到 `meta` 之前停止按钮显示为 disabled 状态。

## 重连

SSE 断了要不要自动重连？**不自动重连。**

`EventSource` 的自动重连在这个场景下是有害的：agent 的一次 run 是有状态的、不可重放的。断线重连后无法"接着上次的位置继续"——后端的 run 已经在独立的 asyncio task 里跑着，重连拿不到它的输出。

做法：

```typescript
// 网络错误 → 提示用户，把 run_id 留着
h.onNetworkError = () => {
  toast.error("连接中断。生成可能仍在后台进行，刷新页面可看到已保存的内容。");
};
```

**"刷新页面可看到已保存的内容"是真的**——因为消息是逐条立即落库的（见 [../architecture/agent-loop.md](../architecture/agent-loop.md#落库时机)）。这是逐条落库设计的直接收益。

### 唯一的重试场景

`fetch` 本身失败（后端没启动、端口错）时重试 3 次，指数退避 500ms / 1s / 2s。这是"连接建立失败"，还没产生任何服务端状态，重试安全。

已经开始接收数据后断开则不重试。

## 切会话：后台 run 的恢复

切会话时 `openSession` 会 abort 当前的 fetch。**这只停本地读取，不取消服务端的生成** —— 那是有意的，用户切走的意图是"让它在后台跑完"。

于是切回来时需要知道"它还在跑吗"。

### 检测 + 轮询

`openSession` 末尾调 `watchBackgroundRun()`：

```
GET /api/sessions/{id}/active-run
  → null    什么都不做
  → run_id  锁输入框（pending: true）+ 显示提示 + 每 2 秒轮询
             轮到 active-run 返回 null 时解锁
```

**必须锁输入框。** 不锁的话用户能输入，一发就撞 409（`run_in_progress`）—— 而那个错误原来被前端报成"连接中断"，措辞和真因完全对不上。

轮询间隔 2 秒：再密就是在刷后端，再疏用户会觉得卡住了。每轮同时拉一次 `listMessages` 拿增量输出。

### 为什么是轮询而不是重连 SSE

一个 run 的 EventBus 只有一个队列、一个消费者。原来的消费者（切走时那个 HTTP 连接）已经 `detach()` 了，队列被清空 —— **那些事件已经永久丢失**，没有可重连的流。

要真正支持重连，得让 bus 支持多消费者 + 事件重放缓冲，那是个大得多的改动。而 run 一直在往库里写消息，轮询就能拿到增量 —— 用户看到的是"每两秒多出一段"而不是逐字流式，但至少内容在动，而且能知道什么时候结束。

见 [../architecture/events.md](../architecture/events.md#detach没人听的时候不能阻塞)。

### 所有回调必须校验 sessionId

`send` 的回调是闭包，捕获的是发消息那一刻的 `sessionId`。用户切走后它们仍会触发，无条件 `set()` 会把上一个会话的状态写进当前界面：

| 回调 | 不校验的后果 |
| --- | --- |
| `onEvent` | 会话 A 的输出流进 B 的气泡列表 |
| `onClose` | 清掉 B 正在进行的 pending/streaming，输入框提前解锁 |
| `onClose("network")` 里的 `openSession(旧 id)` | 把 A 的历史灌进 store 而路由指向 B —— 之后在"B 的页面"发消息实际发往 A |
| `done` 事件里的 `listMessages().then()` | 往返几百毫秒，期间切走就用旧会话的消息覆盖新会话 |
| `openSession` 自己的响应 | 快速连点 A→B→C 时三个请求并发飞行，早发起的后到就会覆盖 |

统一做法：回调开头 `if (get().sessionId !== sessionId) return;`。

### 409 不能报成"连接中断"

`sse.ts` 原来把 `ApiError` 也归到 network 分支。于是 409 显示成"连接中断：该会话已有正在进行的对话" —— 而且 `onClose("network")` 还会触发一次 `openSession` 重新拉历史，把用户刚发的那条乐观插入的消息抹掉（409 发生在后端落库**之前**）。

现在分开：`onApiError` 撤掉乐观消息、显示真实错误码和 hint，并在 `run_in_progress` 时开始轮询等它跑完。

## 状态机

前端的流式状态：

```
idle
  ↓ 用户发送
connecting          （fetch 发出，未收到 meta）
  ↓ meta
streaming           （可取消）
  ↓ approval_required
waiting_approval    （弹框，仍可取消）
  ↓ approve
streaming
  ↓ done
idle
```

关键约束：

- `connecting` 状态下停止按钮 disabled（没有 run_id）
- `streaming` / `waiting_approval` 下输入框 disabled
- **`done` 事件才回到 `idle`**，不是 `agent_end`——子智能体也会发 `agent_end`，挂在那里会导致主流程还在跑时输入框就解锁了

## 事件与 UI 的映射

| 事件 | UI 表现 |
| --- | --- |
| `meta` | 插入用户消息气泡 + 空的助手气泡；启用停止按钮 |
| `agent_start` (depth>0) | 在当前气泡内插入嵌套的子智能体卡片 |
| `thinking` | 追加到折叠的思维链区域（默认折叠，标题显示"思考中…"） |
| `message` | 追加到正文，Markdown 增量渲染 |
| `tool_start` | 插入工具卡片，状态"执行中" |
| `tool_end` | 更新该卡片为完成/失败，填入 `display` |
| `approval_required` | 弹模态框，高亮 `risks`，显示倒计时 |
| `approval_resolved` | **收掉模态框**。漏了它超时后弹框会一直挂着 |
| `todo_updated` | 更新顶栏进度条 + 看板 |
| `refs_expanded` | `failures` 非空时显示警告条；成功不提示 |
| `memory_recalled` | 在气泡上方显示"用了这几条记忆" |
| `compacting` | 显示"正在压缩"（要调一次 LLM，可能几秒） |
| `compacted` | 在时间线插入"已压缩 N 条消息"分隔条 |
| `context_usage` | 更新占用条。固定开销另从 `/context-overhead` 拉 |
| `model_fallback` | toast 提示一次 |
| `title` | 更新会话列表标题 |
| `error` | 气泡内红色错误块，`retryable` 时显示重试按钮 |
| `done` | 合并 streaming 到 messages，解锁输入框 |

`context_usage` 在 run 收尾时会**再发一次**，值含 `completion_tokens`，语义是"下一轮会发多少"。只处理轮内那次的话，模型写了一大段而占用条几乎没动，用户会以为回复不占上下文。

`interact_required` / `sandbox_fallback` / `mcp_unavailable` 后端目前不发 —— 降级提示走 `GET /api/meta` 的字段，前端读它渲染常驻警示条。

## Markdown 增量渲染的性能

每个 `message` chunk 都重新 parse 整段 Markdown 会很慢（长回复下明显卡顿）。

缓解：

1. **节流**：累积 chunk，每 50ms 才触发一次渲染。用户感知不到差别。
2. **代码块单独处理**：流式期间代码块用纯 `<pre>` 显示，`done` 后才做语法高亮。高亮是最贵的操作，且流式期间代码不完整，高亮结果本来就是错的。

第 2 点还顺带解决了一个视觉问题：不完整的代码块被高亮器错误解析会导致颜色乱跳。
