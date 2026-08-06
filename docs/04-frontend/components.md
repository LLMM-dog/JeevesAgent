# 组件清单

前端分层与 store 划分见 [architecture.md](architecture.md)，SSE 消费见 [sse.md](sse.md)。

> 这份文档曾经列了约 40 个不存在的组件名（`ChatInput`、`MessageItem`、`ToolCallCard`、`TopBar`……），而实际的命名体系完全不同。当作导航用会一路找不到文件。现在按 `frontend/src/` 的真实内容重写。

## 全局结构

```
src/
  App.tsx              路由
  main.tsx             入口
  pages/               三个页面
  components/          25 个组件，平铺不分子目录
  store/chat.ts        唯一的 zustand store
  lib/{api,sse,types}  接口层
  hooks/               自定义 hook
```

**components/ 故意平铺不分子目录。** 25 个文件在一层里靠名字就能找到；分成 `chat/` `settings/` `common/` 之后，"这个组件算 chat 还是 common"会变成每次新建文件都要纠结的问题，而纠错成本（改 import 路径）大于收益。

## 页面

| 文件 | 说明 |
| --- | --- |
| `pages/ChatPage.tsx` | 主界面：侧栏 + 消息流 + 输入框 |
| `pages/SettingsPage.tsx` | 设置页，纵向排列各个 Panel |
| `pages/CronPage.tsx` | 定时任务 |

## 对话相关

| 组件 | 说明 |
| --- | --- |
| `Sidebar` | 会话列表。固定置顶、搜索、新建、删除 |
| `MessageList` | 消息流。按 role 分发渲染，处理 streaming 占位 |
| `Markdown` | Markdown 渲染 + 代码高亮 |
| `Composer` | 输入框及其上方的工具栏 |
| `ContextBar` | 上下文占用条 |
| `ToolCard` | 工具调用卡片，可折叠看完整输入输出 |
| `SubAgentCards` | 子智能体的调用与 token 归集 |
| `TodoPanel` | Todo 进度与看板 |
| `ApprovalDialog` | 审批弹框，含倒计时、Esc 拒绝 |
| `Banner` | 顶部提示条（错误 / 警告 / 信息） |
| `RefPicker` | `@` `#` 引用提词器 |
| `MacroPicker` | `!` 宏提词器 |

### Composer 上的开关

一排都在输入框上方，都是**会话级**的：

| 开关 | 作用 |
| --- | --- |
| 审批模式 | 逐个确认 / 自动执行 |
| 视觉 | 开了才能粘图片。未核验的模型点了会拿到 400 |
| 私密 | 这轮不写记忆（仍然读） |
| 失忆 | 这轮不读记忆（仍然写） |

私密和失忆分开是因为用途不同：调试提示词时要失忆但不介意被记住，聊敏感话题时要私密但仍需要之前的上下文。

四个都走乐观更新 + 失败回滚并显示后端的原因。**静默弹回去是不行的** —— 私密开关关系到"我说的话会不会被记住"，失败必须让用户知道。

### ContextBar 的取数

固定开销从 `GET /api/context-overhead` 单独拉，不等 `context_usage` 事件。

新会话里没有任何事件，而固定开销此时已经确定 —— 不显示的话占用条是空的，用户以为"还没开始用 token"。

窗口大小的取值顺序：`usage?.window_tokens || windowTokens || overhead?.window_tokens || 0`。事件里的最准（那是模型实际报的），会话详情次之，端点兜底。

三段的百分比分母都是窗口，不是互比 —— 理由见 [../01-architecture/context.md](../01-architecture/context.md#百分比按窗口算不是分项互比)。

## 设置页的 Panel

| 组件 | 说明 |
| --- | --- |
| `ModelsPanel` | 供应商与模型，含探测、功能位绑定 |
| `AddModelForm` | 手动加模型（探测拿不到列表时的兜底） |
| `ModelSwitcher` | 会话级换模型，也出现在对话界面 |
| `SkillsPanel` | 技能：上传 zip、删除、重扫、**逐项开关**、诊断展示 |
| `MacroPanel` | 宏：新建、编辑、删除、重扫 |
| `McpPanel` | MCP：服务器状态、工具清单与 token 成本、**逐项开关**、待确认命令 |
| `MemoryPanel` | 记忆列表、在线编辑、变更历史、召回探针 |
| `PersonaPanel` | 三份人设的在线编辑 |
| `WhitelistPanel` | 路径白名单 |
| `WorkDirPicker` | 工作目录选择，走 `GET /api/browse` |
| `VisionPanel` | 视觉能力核验 |
| `WebSearchPanel` | 联网搜索开关与 provider |
| `TracePanel` | 执行树：耗时条、逐步 token 与成本 |

### 开关旁边必须显示成本

`SkillsPanel` 和 `McpPanel` 的每一项都标着它占多少 token。

不标的话开关就是个摆设 —— 用户不知道该关哪个。一个用不到的 MCP 服务器可能每轮烧几千 token，而这件事只有把数字放在开关旁边才看得出来。

两个 Panel 的开关成功后都要 `invalidateQueries(["contextOverhead"])`：关掉之后占用条上的数字要立刻变小，否则用户以为开关没生效。

### SkillsPanel 必须显示诊断

`GET /api/skills` 返回的 `diagnostics` 要渲染出来。

用户需要知道"我上传的技能为什么没出现"。缺 `description` 的技能会被静默跳过 —— 只写日志的话他在界面上看到的是技能凭空消失。

### McpPanel 的待确认区放在最上面

未确认启动命令的服务器排在列表前面，显示**完整命令**、cwd、env 键名和危险模式警告。

stdio 服务器等同于任意代码执行。截断命令会让用户看不到真正危险的那一段。

## store 与接口层

| 文件 | 说明 |
| --- | --- |
| `store/chat.ts` | 唯一的 store。对话状态、SSE 事件处理、会话操作 |
| `lib/api.ts` | HTTP 封装。所有请求走 `request()`，带 JSON body 时必须用 `json:` 参数 |
| `lib/sse.ts` | SSE 消费（`fetch` + `ReadableStream`，不能用 `EventSource`） |
| `lib/types.ts` | 全部类型。`SseEventMap` 是事件载荷的真源 |
| `hooks/useSpeechInput.ts` | 语音输入，浏览器不支持时不渲染入口 |

### api.ts 里不要用 body: JSON.stringify

`request()` 只在传 `json:` 参数时才设 `Content-Type`。用 `body: JSON.stringify(...)` 的请求**没有这个头，会被后端 422 拒掉**。

有测试盯着这件事（`test_ui_feedback_round2.py`）—— 这个坑踩过一次，而 422 的报错完全不指向"你少了个请求头"。

### 所有跨会话的回调都要校验 sessionId

`send` 的三个回调（`onEvent` / `onClose` / `onApiError`）、`done` 事件里的 `listMessages().then()`、`openSession` 自己的响应，全部要先确认 `get().sessionId === sessionId`。

这些回调是闭包，捕获的是发消息那一刻的值。用户切走之后它们仍会触发，不校验的话会把上一个会话的状态写进当前界面 —— 表现是"切过去显示了别的会话的对话"。见 [sse.md](sse.md)。
