# SSE 事件协议

**本文件是事件名与字段的唯一真源。** 后端 `emit()` 和前端 `switch` 都以此为准。新增事件必须先改这里。

## 传输方式

```
POST /api/chat
Content-Type: application/json
Accept: text/event-stream
```

**必须用 POST。** 请求体里有消息内容、引用列表、模式开关。

**前端不能用 `EventSource`** —— 它只能发 GET。必须 `fetch` + `response.body.getReader()`。见 [../04-frontend/sse.md](../04-frontend/sse.md)。

### 线格式

```
event: message
data: {"delta":"你好"}

event: tool_start
data: {"call_id":"call_abc","tool_name":"read_file","args":{"path":"a.py"}}

```

每个事件以空行结束。`data` 是单行 JSON（`ensure_ascii=False`，无裸换行）。

### 所有事件的公共字段

每个事件的 `data` 里都有：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ts` | int | 事件产生时间，UTC 毫秒 |
| `span_id` | string \| null | 当前执行单元 |
| `parent_span_id` | string \| null | 父执行单元 |
| `depth` | int | 嵌套深度，0 = 主智能体 |

下面各事件的字段表只列**特有字段**，公共字段不重复列出。

## 事件总表

| 事件名 | 类别 | 可丢弃 | 说明 |
| --- | --- | --- | --- |
| `meta` | 结构 | 否 | run 开始，带 run_id / message_id |
| `agent_start` | 结构 | 否 | 一个智能体开始工作 |
| `thinking` | 增量 | **是** | 推理模型的思维链增量 |
| `message` | 增量 | **是** | 正文增量 |
| `tool_start` | 结构 | 否 | 工具开始执行 |
| `tool_end` | 结构 | 否 | 工具执行完成 |
| `approval_required` | 结构 | 否 | 需人工审批，前端弹框 |
| `interact_required` | 结构 | 否 | 需用户回答，前端弹框 |
| `todo_updated` | 结构 | 否 | Todo 清单变化 |
| `artifact_updated` | 结构 | 否 | 产物更新 |
| `compacted` | 结构 | 否 | 上下文已压缩 |
| `context_usage` | 结构 | 否 | 上下文占用情况 |
| `model_fallback` | 结构 | 否 | 模型绑定降级 |
| `sandbox_fallback` | 结构 | 否 | 沙箱后端降级 |
| `mcp_unavailable` | 结构 | 否 | 某 MCP 服务器不可用 |
| `agent_end` | 结构 | 否 | 一个智能体结束 |
| `title` | 结构 | 否 | 会话标题已生成 |
| `error` | 结构 | 否 | 出错 |
| `cancelled` | 结构 | 否 | 被用户取消 |
| `done` | 结构 | 否 | run 结束，流即将关闭 |
| `ping` | 心跳 | 是 | 每 15 秒，前端忽略 |

**可丢弃**列：队列满时增量类事件直接丢弃不阻塞；结构类事件必须保序入队，哪怕阻塞。见 [../01-architecture/events.md](../01-architecture/events.md#eventbus)。

## 各事件字段

### meta

run 的第一个事件。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `run_id` | string | 用于取消和审批 |
| `session_id` | string | |
| `user_message_id` | string | 刚落库的用户消息 |
| `assistant_message_id` | string | 预分配的助手消息 ID |

前端拿到 `run_id` 后才能启用取消按钮。

### agent_start

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `agent_name` | string | `main` / `researcher` / ... |
| `task` | string \| null | 子智能体的任务描述 |

### thinking

| 字段 | 类型 |
| --- | --- |
| `delta` | string |

思维链增量。前端追加到折叠区域，**不混入正文**。

### message

| 字段 | 类型 |
| --- | --- |
| `delta` | string |

正文增量。前端追加渲染。

### tool_start

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `call_id` | string | 与 `tool_end` 配对 |
| `tool_name` | string | |
| `args` | object | 完整参数 |

### tool_end

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `call_id` | string | 对应 `tool_start` |
| `tool_name` | string | |
| `is_error` | bool | |
| `duration_ms` | int | |
| `display` | object \| null | `ToolResult.display`，前端渲染用 |
| `content_preview` | string | 截断到 500 字符，前端折叠显示 |

**`content` 不完整发送。** 完整内容已落库，前端需要时拉 message 接口。工具输出可能几万字符，走 SSE 会拖慢流。

### approval_required

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `call_id` | string | 回填时要带 |
| `tool_name` | string | |
| `args` | object | |
| `risks` | array | `[{pattern_name, matched_text}]`，风险标注 |
| `timeout_at` | int | 超时时刻，前端显示倒计时 |

前端弹框，用户点允许/拒绝后调 `POST /api/runs/{run_id}/approve`。

超时视为拒绝。

### interact_required

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `call_id` | string | |
| `kind` | string | `text` / `single` / `multi` |
| `question` | string | |
| `options` | array \| null | `kind` 非 text 时的选项 |
| `timeout_at` | int | |

回填走 `POST /api/runs/{run_id}/answer`。

### todo_updated

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `items` | array | 完整列表，元素结构同 `todo` 表 |
| `stats` | object | `{total, completed, in_progress, pending, cancelled}` |

**发完整列表而非 diff。** diff 需要前端维护一致性，丢一个事件就永久错位。

### artifact_updated

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `message_id` | string | artifact 消息 ID |
| `artifact_kind` | string | `file` / `code` / `doc` |
| `artifact_path` | string \| null | |
| `size_chars` | int | |

内容不走事件，前端按 `message_id` 拉。

### compacted

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `before_tokens` | int | 压缩前 |
| `after_tokens` | int | 压缩后 |
| `compacted_count` | int | 被折叠的消息条数 |
| `summary_message_id` | string | 摘要消息 ID |

前端在时间线上插一条"已压缩 N 条消息"的分隔提示，可点开看摘要。

### context_usage

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `used_tokens` | int | 真实 prompt_tokens |
| `window_tokens` | int | 模型窗口 |
| `ratio` | float | used / window |
| `compact_at` | int | 触发压缩的绝对 token 数 |
| `is_estimate` | bool | true 表示本次是估算值（首轮） |

### model_fallback

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `purpose` | string | 哪个功能位 |
| `requested` | string \| null | 原本要用的 |
| `used` | string | 实际用的 |
| `reason` | string | |

降级必须可见。前端提示一次（toast 级别）。

### sandbox_fallback

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `from` | string | `docker` |
| `to` | string | `local` |
| `reason` | string | |

**前端要显示常驻警示条**（不是一闪而过的 toast）——用户需要一直知道当前不是隔离环境。

注意：`from` 是 Python 关键字，后端字段名用 `from_`，但**序列化到 JSON 时必须是 `from`**（Pydantic `alias`）。

### mcp_unavailable

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `server_id` | string | |
| `reason` | string | |
| `lost_tool_count` | int | 因此不可用的工具数 |

### agent_end

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `agent_name` | string | |
| `stop_reason` | string | `final` / `max_turns` / `error` |
| `turns` | int | |
| `prompt_tokens` | int | |
| `completion_tokens` | int | |

### title

| 字段 | 类型 |
| --- | --- |
| `session_id` | string |
| `title` | string |

首轮对话后生成。前端更新会话列表里的标题。

### error

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `code` | string | 同 [错误码清单](conventions.md#错误码清单) |
| `message` | string | |
| `hint` | string \| null | |
| `retryable` | bool | 前端是否显示"重试"按钮 |

`retryable=false` 的典型场景：LLM 返回 400（这是程序 bug，重试无意义）。

### cancelled

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `run_id` | string | |
| `partial_saved` | bool | 已生成的部分是否已落库 |

`partial_saved` 应该总是 `true` —— 这是 journal sink 机制保证的。如果出现 `false`，说明落库路径有 bug。

### done

流的最后一个事件。前端收到后关闭 reader、恢复输入框。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `run_id` | string | |
| `status` | string | `done` / `cancelled` / `error` |

**`done` 一定会发**，无论成功、失败还是取消。前端的"恢复输入框"逻辑挂在这里，不要挂在 `agent_end`（子智能体也会发 `agent_end`）。

### ping

无特有字段。前端 `switch` 里显式 `break`，不做任何处理。

## 事件顺序保证

一个正常 run 的事件序列：

```
meta
agent_start (depth=0)
  thinking* message*
  tool_start tool_end
  [approval_required → 等待]
  thinking* message*
agent_end (depth=0)
title            (仅首轮)
done
```

保证：

1. `meta` 一定是第一个
2. `done` 一定是最后一个
3. `tool_start` / `tool_end` 按 `call_id` 配对，且 `tool_start` 先到
4. `agent_start` / `agent_end` 按 `span_id` 配对
5. **`thinking` 和 `message` 可能丢失**（队列满时），前端不能依赖它们的完整性

第 5 条的含义：最终的完整正文以数据库为准。前端在 `done` 之后可以选择重新拉一次 message 校正（可选优化，M0 不做）。

## 前端 switch 必须有 default 分支

```typescript
default:
  // 不能静默 break。后端加了新事件而前端没跟上时，
  // 这行 warn 是唯一的发现途径。
  console.warn("[sse] 未处理的事件:", event, data);
  break;
```

一条教训：后端发 14 种事件，前端只处理 6 种，`agent_start` 和 `span_id` 完全没落地，气泡树功能等于不存在——而且没有任何报错。
