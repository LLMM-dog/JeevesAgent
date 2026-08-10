# 前端架构

## 技术栈

| 项 | 选择 | 理由 |
| --- | --- | --- |
| 框架 | React 19 | 已有经验，生态最全 |
| 构建 | Vite 7 | 启动快，配置少 |
| 语言 | TypeScript 5 | |
| 样式 | Tailwind 4 + @tailwindcss/vite | 零配置，v4 用 CSS 配置替代 JS |
| 状态 | zustand | 比 Redux 轻，比 Context 好用。无样板代码 |
| 数据请求 | @tanstack/react-query | 缓存、去重、乐观更新 |
| 请求 | 原生 fetch | **不用 axios**，见下 |
| 路由 | react-router-dom 7 | |
| Markdown | react-markdown + remark-gfm + shiki 语法高亮 | |
| 图标 | lucide-react | |

### 为什么不用 axios

axios 的主要价值在拦截器里做 JWT refresh 那套逻辑（单飞刷新、跨标签页协调、登出不重放）。**本项目无鉴权，这些全不需要。**

剩下的需求（统一错误处理、baseURL）用一个 50 行的 fetch 包装就够。而 SSE 那条链路本来就必须用原生 fetch（axios 不支持流式读取），用两套请求方式反而分裂。

## 目录结构

```
frontend/src/
├── main.tsx
├── App.tsx
├── api/                  # 请求层
│   ├── client.ts         # fetch 包装 + 错误处理
│   ├── sessions.ts       # 按资源分文件
│   ├── providers.ts
│   ├── skills.ts
│   ├── settings.ts
│   └── stream.ts         # SSE 专用
├── stores/               # zustand
│   ├── sessionStore.ts
│   ├── chatStore.ts
│   ├── todoStore.ts
│   ├── configStore.ts
│   └── uiStore.ts
├── components/
│   ├── ui/               # shadcn 生成的原语
│   ├── chat/
│   ├── todo/
│   ├── settings/
│   └── common/
├── pages/
│   ├── ChatPage.tsx
│   └── SettingsPage.tsx
├── hooks/
├── types/                # 与后端对齐的类型定义
│   ├── api.ts
│   └── events.ts
├── lib/                  # 纯函数工具
└── styles/
```

### 单文件上限 300 行

超过就拆。反面教材：见过 `IngestionPage.tsx` 95KB、`globals.css` 65KB 的——这种文件没人敢改。

页面组件（`pages/`）只负责布局和数据装配，具体渲染全在 `components/` 里。

## 类型定义与后端对齐

`types/api.ts` 里的 interface **字段名与后端完全一致（snake_case）**，不做转换。

```typescript
// types/api.ts
export interface Message {
  id: string;
  seq: number;
  role: "user" | "assistant" | "tool" | "system" | "summary" | "artifact";
  agent_name: string;
  content: string;
  reasoning: string | null;
  tool_calls: ToolCall | null;
  tool_call_id: string | null;
  tool_name: string | null;
  tool_display: Record<string, unknown> | null;
  is_error: boolean;
  refs: Ref;
  attachments: string;
  artifact_kind: "file" | "code" | "doc" | null;
  artifact_path: string | null;
  run_id: string | null;
  span_id: string | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  created_at: number;
}
```

### 为什么不用 camelCase

TS 社区习惯 camelCase，这里刻意违背。理由：

**转换层是 bug 的温床。** 一旦前后端命名不同，就需要一个转换函数。然后：

- 嵌套对象要递归转换，`tool_display` 这种自由结构会被误转（它的 key 来自工具实现，不该被改名）
- 后端加了字段，前端转换器不知道，静默丢失
- 调试时 Network 面板看到的字段名和代码里的不一样，每次都要心算映射
- 日志里的字段名和前端代码对不上

省掉转换层的代价只是 `message.agent_name` 看起来不够 TS 味。这个代价远小于上面四条。

**规则**：来自后端的数据一律 snake_case；纯前端的局部变量、组件 props、hook 返回值用 camelCase。边界清晰——**带 `_` 的就是后端来的**，这本身还成了一种有用的视觉提示。

## 请求层

```typescript
// api/client.ts
const BASE = import.meta.env.VITE_API_BASE ?? "/api";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public hint: string | null,
  ) { super(message); }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!resp.ok) {
    // 后端所有错误统一为 { detail: {code, message, hint} }，含 422。
    // 见 api/conventions.md
    const body = await resp.json().catch(() => null);
    const d = body?.detail ?? {};
    throw new ApiError(resp.status, d.code ?? "unknown", d.message ?? resp.statusText, d.hint ?? null);
  }
  if (resp.status === 204) return undefined as T;
  return resp.json();
}

export const api = {
  get: <T>(p: string) => request<T>(p),
  post: <T>(p: string, body?: unknown) =>
    request<T>(p, { method: "POST", body: JSON.stringify(body ?? {}) }),
  patch: <T>(p: string, body: unknown) =>
    request<T>(p, { method: "PATCH", body: JSON.stringify(body) }),
  put: <T>(p: string, body: unknown) =>
    request<T>(p, { method: "PUT", body: JSON.stringify(body) }),
  del: <T>(p: string) => request<T>(p, { method: "DELETE" }),
};
```

**不做统一的错误 toast。** 每个调用点决定怎么展示——有些错误要弹框（模型未配置），有些要行内提示（路径被拒），有些要静默（后台轮询失败）。在 client 里统一弹 toast 会让"静默"变得不可能。

### 上传单独处理

`multipart/form-data` 不能带 `Content-Type` 头（浏览器要自己加 boundary）：

```typescript
export async function upload<T>(path: string, form: FormData): Promise<T> {
  // 不设 Content-Type —— 浏览器会自动加上带 boundary 的正确值。
  // 手动设置会导致 boundary 缺失，后端解析失败且报错信息很晦涩。
  const resp = await fetch(`${BASE}${path}`, { method: "POST", body: form });
  ...
}
```

## Store 划分

按**变更频率**划分，不按数据类型。高频变更的和低频的分开，避免不必要的重渲染。

| Store | 内容 | 变更频率 |
| --- | --- | --- |
| `chatStore` | 当前会话的消息、流式状态、run_id | **极高**（流式期间每个 chunk） |
| `todoStore` | 当前会话的 Todo | 中 |
| `sessionStore` | 会话列表、当前会话元信息 | 低 |
| `configStore` | 供应商、模型、绑定、技能、宏、meta | 极低（启动时拉一次） |
| `uiStore` | 侧栏折叠、弹框开关、提词器状态 | 中 |

### chatStore 的流式性能

流式期间每个 `message` 事件都要更新状态。如果整个消息列表在一个 store 字段里，每次更新都会让所有消息组件重渲染——长会话下会明显卡顿。

做法：

```typescript
interface ChatState {
  messages: Message;           // 已完成的消息
  streaming: {                   // 正在流式的那一条单独放
    message_id: string;
    content: string;
    reasoning: string;
    tool_calls: StreamingToolCall;
  } | null;
}
```

流式期间只更新 `streaming`，`messages` 不动。`done` 事件后把 `streaming` 合并进 `messages` 并清空。

这样重渲染只影响最后一个气泡。

再配合 `react-virtuoso` 做虚拟滚动，几千条消息也不卡。

## 页面结构

只有两个页面。

### ChatPage

```
┌─────────┬────────────────────────────────────┐
│         │ 顶栏: 标题 · Todo进度 · 上下文占用   │
│ 会话    │      检查/自动 · 私密 · 视觉        │
│ 列表    ├────────────────────────────────────┤
│         │                                    │
│ [新建]  │  消息流（虚拟滚动）                 │
│ 会话1   │                                    │
│ 会话2   │                                    │
│         ├────────────────────────────────────┤
│         │ 引用条 + 输入框（可拖拽调高）+ 发送  │
└─────────┴────────────────────────────────────┘
```

顶栏的开关都是**会话级**的，切换会话时要重新读取。

### SettingsPage

侧栏 tab：模型 / 技能 / 宏 / MCP / 人设 / 路径安全 / 工作区。

## 静态资源托管

生产模式下前端构建产物由后端托管：

```python
app.mount("/", StaticFiles(directory="frontend/dist", html=True), name="static")
```

**必须最后 mount**，在所有 API 路由注册之后——否则 `/` 的通配会吃掉 `/api/*`。

开发模式下不 mount，前端跑 Vite dev server（5173）通过 CORS 访问后端（8000）。Vite 配 proxy 也可以，但 CORS 更简单且和生产行为一致。

`html=True` 让 SPA 路由生效（任何未匹配路径回落到 `index.html`）。

## 不做的事

| 不做 | 理由 |
| --- | --- |
| SSR / Next.js | 本地工具，无 SEO 需求，无首屏优化必要 |
| 状态持久化到 localStorage | 数据在后端。只存 UI 偏好（侧栏宽度、主题） |
| 国际化 | 单人使用，中文 |
| 主题系统 | 只做浅色 + 深色两套 Tailwind 变量 |
| PWA / 离线 | |
| 单元测试全覆盖 | 只测纯函数（SSE 解析器、格式化工具）。UI 靠手动验证 |
