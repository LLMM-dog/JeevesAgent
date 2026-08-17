# Jeeves 文档

跑在本机的 AI 助手。Python 后端（FastAPI，纯 while 循环 agent loop）+ React 前端。

## 怎么读

- 想用起来：看 [architecture/tools.md](architecture/tools.md) 和 [architecture/skills.md](architecture/skills.md)
- 想改代码：从 [architecture/agent-loop.md](architecture/agent-loop.md) 开始
- 查接口：看 [api/](api/)
- 产品定位：看 [guides/product.md](guides/product.md)

| 目录 | 内容 |
| --- | --- |
| [architecture/](architecture/) | 系统设计：后端分层、agent loop、工具/事件/上下文/技能/沙箱/安全、数据层、前端架构、多智能体 |
| [architecture/tools.md](architecture/tools.md) | 16 个内置工具能做什么 |
| [architecture/execution.md](architecture/execution.md) | 命令执行与审批。**安全边界写在开头，动手前先看** |
| [architecture/skills.md](architecture/skills.md) | 技能与宏：三级渐进披露、上传校验、模型自己建技能 |
| [architecture/cron.md](architecture/cron.md) | 定时任务：调度、错过窗口、时区、无头执行 |
| [architecture/context.md](architecture/context.md) | 上下文：占用计算、压缩时机与预算 |
| [architecture/memory.md](architecture/memory.md) | 记忆系统：三层隔离（全局/智能体/会话）、peer、存储形态、merge 语义 |
| [architecture/memory-schema.md](architecture/memory-schema.md) | 记忆类型 YAML：字段说明、内置 10 种类型、如何自己加一种 |
| [architecture/multi-agent-roadmap.md](architecture/multi-agent-roadmap.md) | 多智能体路线图：验证增强、编排、技能进化 |
| [api/](api/) | HTTP 接口、SSE 事件协议 |
| [guides/](guides/) | 产品定位、术语表 |
| [development/](development/) | 环境搭建、代码规范、测试策略 |
| [adr/](adr/) | 架构决策记录（ADR）。为什么选了 A 而不是 B |

## 全局硬约束速查

违反以下任一条，一定会产生跨层不一致的 bug。写任何一层代码前先扫一遍。

### 命名与序列化

1. **全栈 snake_case。** 数据库列、Python 字段、JSON key、TypeScript interface 字段，全部 snake_case。不做 camelCase 转换。理由见 [architecture/frontend.md](architecture/frontend.md#为什么不用-camelcase)。
2. **ID 带类型前缀**，格式 `<前缀>_<base62(12)>`，如 `ses_7bK2mQ9xR4Lp`。前缀表见 [architecture/data-schema.md](architecture/data-schema.md#id-规范)。
3. **时间戳统一为 UTC 毫秒整数**，字段名以 `_at` 结尾（`created_at`、`updated_at`）。不存 ISO 字符串，不存秒。

### 接口

4. **成功响应直接返回数据对象**，不套 `{code, data}` 信封。错误用 HTTP 状态码 + `{"detail": {...}}`。见 [api/conventions.md](api/conventions.md)。
5. **`POST /api/chat` 是 POST + SSE**，前端必须用 `fetch` + `ReadableStream` 消费，**不能用 `EventSource`**（它只能发 GET）。这是 jeeves 前端实际踩过的坑。
6. **SSE 事件名与字段以 [api/sse-events.md](api/sse-events.md) 的表为唯一真源。** 后端新增事件必须同步更新该表和前端 `switch`，否则前端静默丢事件。

### Agent 内核

7. **`messages` 是会被压缩重写的工作副本**，`journal` 是 append-only 全量流水。落库读 journal，发给 LLM 的是压缩后的 messages。见 [architecture/agent-loop.md](architecture/agent-loop.md#messages-与-journal-的分离)。
8. **从 DB 组装上下文时必须做一致性校验**：孤立的 `tool_calls`（无对应 tool 结果）要补占位，孤立的 `tool` 消息要丢弃。否则下一轮直接 400。取消、崩溃、断电都会产生这种不一致。详见 [architecture/context.md](architecture/context.md#组装前的一致性校验)。
9. **运行中可能被修改的状态不能用 ContextVar**（如 `approval_mode`）。ContextVar 在 task 创建时快照，外部 `set()` 对已运行的 task 不可见。用模块级 dict + session_id。
10. **压缩切点绝不拆开 `tool_calls` 与其 `tool` 结果**，否则 LLM 直接返回 400。
11. **摘要以 `user` 角色注入，不用 `system`。** artifact 不参与压缩，单独钉在上下文末尾。
12. **工具执行异常转成错误文本返回给模型，不向上抛。** 未知工具同理。
13. **技能正文以工具返回值形态进上下文**，不进 system 位——用户上传的内容是数据不是指令。

### 安全

14. **所有文件工具的路径必须过白名单校验**，`resolve()` 后判 `is_relative_to`。模型输出的 `path` 只用于查表/校验，绝不直接 `open()` 拼接。
15. **默认在宿主机直接执行命令**（`sandbox__backend=local`）。这种模式下 `run_shell` 能做的事没有上界，路径白名单管不到它，边界只有人工确认。要真隔离就配 `sandbox__backend=docker`——`--network none` + 资源限制 + `cap-drop ALL`，只挂工作区。检测不到 Docker 时会降级到本地执行并**在界面上持续提示**（配了 docker 就是想要隔离，静默回落等于骗人）。见 [architecture/sandbox.md](architecture/sandbox.md)。
16. **API 默认只绑 `127.0.0.1` 且无鉴权。** 这是单机个人项目的显式取舍，绑到 `0.0.0.0` 前必须先加鉴权。见 [architecture/security.md](architecture/security.md)。
17. **API Key 加密存储**（Fernet，密文带 `v1:` 前缀），只回显尾 4 位。任何接口不返回明文。

### 配置

18. **`.env` 路径用绝对路径解析**（基于 `__file__`），不用相对路径——相对路径按进程 cwd 解析，换个目录启动就静默读不到配置。

## 文档规范

- 每个 `.md` 控制在 300 行内，超了拆分。
- 写"为什么这样设计"，不写"这段代码做了什么"。踩过的坑、实测数据要留在文档里。
- 表结构、事件名、接口路径这三类内容只在其唯一真源文件里定义，其它地方一律用链接引用，不复制。
