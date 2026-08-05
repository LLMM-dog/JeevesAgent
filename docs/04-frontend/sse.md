# SSE 消费

事件定义见 [../03-api/sse-events.md](../03-api/sse-events.md)。本文只讲前端怎么消费。

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

## dispatch

```typescript
function dispatch(e: { event: string; data: any }, h: EventHandlers): void {
  switch (e.event) {
    case "meta":              h.onMeta(e.data); break;
    case "agent_start":       h.onAgentStart(e.data); break;
    case "thinking":          h.onThinking(e.data); break;
    case "message":           h.onMessage(e.data); break;
    case "tool_start":        h.onToolStart(e.data); break;
    case "tool_end":          h.onToolEnd(e.data); break;
    case "approval_required": h.onApprovalRequired(e.data); break;
    case "interact_required": h.onInteractRequired(e.data); break;
    case "todo_updated":      h.onTodoUpdated(e.data); break;
    case "artifact_updated":  h.onArtifactUpdated(e.data); break;
    case "compacted":         h.onCompacted(e.data); break;
    case "context_usage":     h.onContextUsage(e.data); break;
    case "model_fallback":    h.onModelFallback(e.data); break;
    case "sandbox_fallback":  h.onSandboxFallback(e.data); break;
    case "mcp_unavailable":   h.onMcpUnavailable(e.data); break;
    case "agent_end":         h.onAgentEnd(e.data); break;
    case "title":             h.onTitle(e.data); break;
    case "error":             h.onError(e.data); break;
    case "cancelled":         h.onCancelled(e.data); break;
    case "done":              h.onDone(e.data); break;
    case "ping":              break;                    // 心跳，显式忽略
    default:
      // 绝不静默 break。后端加了事件而前端没跟上时，这行是唯一的发现途径。
      // 一条教训：后端 14 种事件前端只处理 6 种，气泡树功能等于不存在，
      // 且完全没有报错。
      console.warn("[sse] 未处理的事件:", e.event, e.data);
  }
}
```

`EventHandlers` 是一个**所有字段必需**的 interface（不用 `?` 可选）。这样后端加事件时，TS 编译会在 handler 定义处报错，强制处理。

这比运行时 `console.warn` 更早发现问题。两者都要有。

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

**"刷新页面可看到已保存的内容"是真的**——因为消息是逐条立即落库的（见 [../01-architecture/agent-loop.md](../01-architecture/agent-loop.md#落库时机)）。这是逐条落库设计的直接收益。

### 唯一的重试场景

`fetch` 本身失败（后端没启动、端口错）时重试 3 次，指数退避 500ms / 1s / 2s。这是"连接建立失败"，还没产生任何服务端状态，重试安全。

已经开始接收数据后断开则不重试。

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
| `interact_required` | 弹模态框或在气泡内渲染选项按钮 |
| `todo_updated` | 更新顶栏进度条 + 看板 |
| `compacted` | 在时间线插入"已压缩 N 条消息"分隔条 |
| `context_usage` | 更新顶栏占用条 |
| `model_fallback` | toast 提示一次 |
| `sandbox_fallback` | **常驻警示条**，不是 toast |
| `title` | 更新会话列表标题 |
| `error` | 气泡内红色错误块，`retryable` 时显示重试按钮 |
| `done` | 合并 streaming 到 messages，解锁输入框 |

## Markdown 增量渲染的性能

每个 `message` chunk 都重新 parse 整段 Markdown 会很慢（长回复下明显卡顿）。

缓解：

1. **节流**：累积 chunk，每 50ms 才触发一次渲染。用户感知不到差别。
2. **代码块单独处理**：流式期间代码块用纯 `<pre>` 显示，`done` 后才做语法高亮。高亮是最贵的操作，且流式期间代码不完整，高亮结果本来就是错的。

第 2 点还顺带解决了一个视觉问题：不完整的代码块被高亮器错误解析会导致颜色乱跳。
