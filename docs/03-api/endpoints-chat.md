# 接口：会话与对话

通用约定见 [conventions.md](conventions.md)。SSE 事件见 [sse-events.md](sse-events.md)。

## 会话

### GET /api/sessions

列表，分页。按 `pinned DESC, last_message_at DESC` 排序。

查询参数：`page` / `size` / `workspace_id`（可选过滤）/ `q`（可选，标题模糊搜索）

```jsonc
{
  "items": [{
    "id": "ses_7bK2mQ9xR4Lp",
    "title": "重构登录模块",
    "workspace_id": "wsp_3nF8kL2pQ7xY",
    "pinned": false,
    "message_count": 24,
    "last_message_at": 1785312000000,
    "created_at": 1785309000000
  }],
  "total": 137, "page": 1, "size": 20, "pages": 7
}
```

列表**不含** `approval_mode` 等模式开关——列表不需要，详情才需要。

### POST /api/sessions

```jsonc
// 请求（全部可选）
{ "title": "", "workspace_id": "wsp_xxx" }
```

`workspace_id` 不传则用默认工作区。返回 201 + 完整会话对象。

### GET /api/sessions/{id}

返回完整对象，含四个模式开关。

### PATCH /api/sessions/{id}

可改字段：`title`、`pinned`、`approval_mode`、`vision_mode`、`private_mode`、`amnesia_mode`、`work_dir`、`model_pk`。

用 `exclude_unset` 区分"没传"和"传了 null"—— 否则前端只想改标题时会把别的字段全清空。

`vision_mode` 开在未核验的模型上返回 400 `vision_unverified`，附带"去设置页核验"的 hint。前端不重复判断（能力状态在设置页才拿得到）。

设 `work_dir` 会**自动往白名单加一条可写条目**，清空时自动撤销。不自动加的话用户选完工作目录发现 agent 读不了，而他想不到还要去白名单里再加一遍。

`private_mode` / `amnesia_mode` 是两件事：私密是"别记住我说的"（拦写入侧三个记忆工具），失忆是"别拿以前的事来烦我"（拦召回入口）。拆成两个开关是因为用途不同 —— 调试提示词时要失忆但不介意被记住，聊敏感话题时要私密但仍需要之前的上下文。

### DELETE /api/sessions/{id}

级联删除该会话的 message / todo / run / span。同时清理该会话的 Docker 沙箱容器。

### GET /api/sessions/{id}/messages

**不分页**，返回全量。

查询参数：`agent_name`（默认 `""` 主线；传 `*` 返回全部线）

```jsonc
{
  "items": [{
    "id": "msg_5xN2pK8qL3mR",
    "seq": 1,
    "role": "user",
    "agent_name": "",
    "content": "帮我看看登录逻辑",
    "reasoning": null,
    "tool_calls": null,
    "tool_call_id": null,
    "tool_name": null,
    "tool_display": null,
    "is_error": false,
    "refs": [{"type": "file", "path": "src/auth.py"}],
    "attachments": ,
    "artifact_kind": null,
    "artifact_path": null,
    "run_id": "run_9mK3nQ7xR2Lp",
    "span_id": null,
    "prompt_tokens": null,
    "completion_tokens": null,
    "created_at": 1785309000000
  }]
}
```

字段与 [message 表](../02-data/schema.md#message)一一对应。`tool_calls` / `tool_display` / `refs` / `attachments` 在库里是 JSON 文本，接口返回时已解析为对象/数组。

### DELETE /api/sessions/{id}/messages/{message_id}

**截断删除**：删除该消息及其之后的全部消息（按 `seq`）。

用于"从某条消息处重发"：前端先调这个删掉，再调 `POST /api/chat` 重发。

返回 `{"deleted_count": 5}`。

这是 "消息节点化编辑"的简化实现。不做完整分支树——分支树需要额外的树形结构、UI 上的分支切换器，而实际使用中"改掉重来"占绝大多数。

### GET /api/sessions/{id}/export

导出会话。`?fmt=markdown`（默认）或 `?fmt=json`。

返回文件流，带 `Content-Disposition: attachment`。

| 格式 | 用途 | 取舍 |
| --- | --- | --- |
| `markdown` | 给人读、贴进笔记或 issue | 工具结果折叠成 `<details>` 并截断到 2KB；不含 reasoning |
| `json` | 备份和迁移 | 保留全部字段（含 reasoning、token 计数、run_id）；带 `schema_version` |

两种格式都**不内联 base64 图片** —— 一张图几百 KB，几张就让文件大到无法处理。Markdown 里标注张数，JSON 里给 `attachment_count`。

文件名由标题清理后生成（模型生成的标题可能含 Windows 非法字符），并按 RFC 5987 编码 —— 第 83 条。

导出包含子智能体消息（`agent_name` 非空的那些）。只导主线的话，导出的对话里会出现"我派个子智能体去查"然后突然有了结论。

## 对话

### POST /api/chat

**SSE 流式响应。这是整个项目最核心的接口。**

```jsonc
// 请求
{
  "session_id": "ses_7bK2mQ9xR4Lp",
  "content": "帮我把这个函数改成异步的",
  "refs": [
    {"type": "file", "path": "D:/proj/src/auth.py"},
    {"type": "dir", "path": "D:/proj/src/utils"},
    {"type": "url", "href": "https://docs.python.org/3/library/asyncio.html"},
    {"type": "text", "content": "之前提到的那段代码", "source_message_id": "msg_xxx"},
    {"type": "skill", "name": "async-refactor"},
    {"type": "tool", "name": "run_python"},
    {"type": "macro", "name": "daily-standup"}
  ],
  "attachment_ids": ["att_2kL9mN3pQ7xR"],
  // 图片以 data URL 传，服务端校验魔数、大小、数量上限
  "images": ["data:image/png;base64,..."]
}
```

响应：`text/event-stream`，事件见 [sse-events.md](sse-events.md)。

#### refs 的七种类型

| type | 必需字段 | 效果 |
| --- | --- | --- |
| `file` | `path` | 文件内容注入（经白名单校验） |
| `dir` | `path` | 目录树注入（不含文件内容） |
| `url` | `href` | 抓取转 Markdown 注入 |
| `text` | `content` | 直接注入。`source_message_id` 可选，仅用于前端显示来源 |
| `skill` | `name` | 强制加载该技能的 L2 正文 |
| `tool` | `name` | 提示模型优先用该工具（不强制） |
| `macro` | `name` | 注入该宏的正文 |

`skill` / `tool` 对应前端输入框的 `@` / `#` 提词器，`macro` 对应 `!`。

#### 校验顺序

必须按此顺序，先失败先返回：

```
1. session 存在？          → 404
2. chat 位有绑定模型？      → 400 no_model_bound
3. refs 里的 file/dir 路径过白名单？ → 403
4. refs 里的 skill/macro 存在？     → 404
5. attachment_ids 存在？    → 404
6. 该 session 有正在运行的 run？    → 409 run_in_progress
7. 开始流式响应
```

第 6 条：一个会话同时只允许一个 run。前端在流未结束时应禁用发送按钮，但后端必须也拦——用户可能开两个标签页。

**一旦开始流式响应，就不能再返回 HTTP 错误码**（响应头已发出）。此后的所有错误走 `error` 事件。所以上面 1~6 的校验必须在流开始前完成。

### POST /api/runs/{run_id}/cancel

```jsonc
// 响应
{ "run_id": "run_xxx", "status": "cancelled" }
```

幂等：已取消或已结束的 run 重复调用返回 200，不报错。用户可能连点。

找不到 run_id 返回 404 `run_not_found`。

### POST /api/runs/{run_id}/approve

```jsonc
// 请求
{ "call_id": "call_abc", "approved": true }
```

`call_id` 来自 `approval_required` 事件。

不幂等：重复审批同一个 `call_id` 返回 409 `run_already_finished`。

超时后再来审批也返回 409——超时已被视为拒绝，工具已经执行完（返回了"审批超时"）。

### POST /api/runs/{run_id}/answer

回答 `interact_required`。

```jsonc
{ "call_id": "call_abc", "answer": "用第二个方案" }
// 或选择题
{ "call_id": "call_abc", "selected": ["option_1", "option_3"] }
```

`kind=text` 用 `answer`，`kind=single`/`multi` 用 `selected`。

### GET /api/sessions/{id}/active-run

这个会话有没有正在后台跑的 run。

```json
{ "run_id": "run_9xK2mQ7pR4Lp" }
```

没有则返回 `null`。

#### 为什么需要这个接口

用户在 auto 模式下**切走会话时，服务端的 run 会继续跑完** —— 那是有意的，他要的就是"让它自己跑"。

但切回来时前端只会 `listMessages`，看到的是切走那一刻的历史，之后再没有新内容。而后台其实一直在写库。用户以为卡死了，一发消息还会撞 409（`run_in_progress`）。

有了这个接口，前端切回来能知道"还在跑"，于是锁上输入框、显示提示、轮询拉增量，跑完自动解锁。

#### 为什么不返回进度

进度在事件流里，而事件流已经随着上一个连接的 `detach()` 消失了 —— 那个队列被清空，那些事件永久丢失。这里能诚实给出的只有"在跑 / 不在跑"。

多返回一个猜测的进度比不返回更糟。要真支持重连得让 EventBus 支持多消费者 + 事件重放缓冲，见 [../01-architecture/events.md](../01-architecture/events.md#detach没人听的时候不能阻塞)。

> 单个 run 的执行树看 `GET /api/traces/{run_id}`，见 [endpoints-config.md](endpoints-config.md#追踪)。

### GET /api/sessions/{id}/todos

不分页。默认只返回未归档的。

查询参数：`include_archived`（默认 false）

```jsonc
{
  "items": [{
    "id": "todo_4mK8nP2qL7xR",
    "content": "实现 JWT 签发",
    "status": "in_progress",
    "priority": "high",
    "order_index": 1,
    "archived_at": null,
    "created_at": 1785312000000
  }],
  "stats": {"total": 5, "completed": 3, "in_progress": 1, "pending": 1, "cancelled": 0}
}
```

`stats` 与 `todo_updated` 事件里的结构一致。

### PATCH /api/todos/{id}

用户手动改状态或内容。可改：`content` / `status` / `priority` / `order_index`

改成 `in_progress` 时，自动把该会话其它 `in_progress` 降为 `pending`（保证唯一）。

**用户的手动修改对模型可见**——下一轮 `todo_read` 能读到。

### DELETE /api/todos/{id}

### POST /api/sessions/{id}/todos/archive

验收关闭：把该会话所有未归档 Todo 标记 `archived_at`。

```jsonc
{ "archived_count": 5 }
```

## 附件

### 图片上传

见 [endpoints-config.md](endpoints-config.md#图片)：`POST /api/images/upload`。

它返回 data URL 而不是附件 id —— 图片以 base64 多模态注入，前端拿到就能直接回显缩略图，不需要再请求一次。所以 `POST /api/chat` 的 `images` 字段收的是 data URL。
