# 后端分层

## 三层划分

不用 `api/service/model/schema` 水平分层，改用 **`core` / `infra` / `modules` 三层 + 模块内垂直切分**。

理由：水平分层在 agent 项目里会把一个功能拆到四个目录，改一处要跳四个文件；而 agent 项目的变更几乎总是"整个功能一起动"。垂直切分让"找实现"这件事有唯一答案——**代码位置跟着表和 service 走**。

```
backend/
  app/
    main.py              # create_app() 工厂
    core/                # 无外部依赖的通用能力
    infra/               # 外部依赖的适配器，一律 port + impl
    modules/             # 业务模块，每个含 models/schemas/service/router
    prompts/             # 提示词 .md，外置
  migrations/            # alembic
  alembic.ini
```

**依赖方向单向**：`modules → infra → core`。`core` 不许 import `infra` 或 `modules`；`infra` 不许 import `modules`。

## core/

只放不依赖任何外部服务的东西。全部可单元测试，无需 mock。

| 文件 | 职责 |
| --- | --- |
| `config.py` | pydantic-settings 嵌套分组配置，`.env` 绝对路径解析 |
| `ids.py` | 带类型前缀的 ID 生成 |
| `events.py` | `ContextVar` 事件总线，见 [events.md](events.md) |
| `trace_context.py` | `span_id` / `parent_span_id` / `depth` 的 ContextVar 传递 |
| `exceptions.py` | `AppError` 体系 |
| `crypto.py` | Fernet 加解密，密文带 `v1:` 前缀 |
| `logging.py` | structlog 配置 |
| `time.py` | `now_ms()` 统一时间源 |
| `runtime_state.py` | 会话级运行时状态（审批模式、审批等待） |

### 为什么 `time.py` 单独存在

全项目只允许通过 `core.time.now_ms()` 取当前时间。散落的 `datetime.now()` 会出现三种写法（本地时区/UTC/秒），排查时间相关 bug 时无从下手。测试里也只需 patch 这一处。

## infra/

每个子目录一个外部依赖，**一律 `port.py` 定义协议 + 具体实现文件**。上层只 import port。

```
infra/
  db/         base.py session.py
  llm/        port.py openai_compat.py
  sandbox/    port.py local.py docker.py factory.py
```


### port + 适配器不是过度设计

三个已知的换实现场景：

1. **sandbox** 有本地和 Docker 两个实现，运行时按配置选，且 Docker 不可用要降级。没有 port 就得在调用点写 if/else。
2. **websearch** 的 Tavily 要 API Key 且收费，DuckDuckGo 免费零配置。用哪个取决于用户当时有什么。
3. **llm** 虽然只有 OpenAI 兼容一个实现，但 port 的价值在**测试**——`FakeLLM` 直接实现 port，不用 mock HTTP。

反例：`db` 不做 port。SQLAlchemy 本身已是抽象层，再包一层纯属浪费。

### infra/http/client.py 的两个硬性配置

```python
# trust_env=False：httpx 默认会读环境变量与 Windows 注册表里的系统代理。
# 实测：本机开着 Clash 时，同一个长生成请求走代理 32s 被掐断，绕过代理 252s 正常完成。
# 代理软件的空闲连接超时远短于一次长推理，而推理阶段不吐任何字节，连接看起来就是"空闲"的。
# timeout=300：60s 会让代码生成必然失败。实测一次演示请求耗时 220s，
# 22921 个 completion token 里 20994 是推理 token（91%），这段时间流里没有任何数据。
```

## modules/

按业务垂直切分，每个模块内部统一四件套：

| 文件 | 职责 |
| --- | --- |
| `models.py` | SQLAlchemy 模型 |
| `service.py` | 业务逻辑，不碰 HTTP（或 `repo.py`，数据访问） |
| `router.py` | FastAPI 路由，只做参数校验与调用 service |

模块清单：

```
modules/
  session/       会话与消息的 CRUD、截断重发、导出
  agent/         智能体循环、工具、压缩、AgentSpec、run 注册表、路径守卫
    loop.py compaction.py hooks.py subagent_runner.py specs.py
    prompts.py tokens.py messages.py refs.py models.py
    tools/   内置工具（file, exec, todo, skill, subagent, context, asset, web）
  endpoint/      模型端点（原 provider）、模型探测、窗口匹配、视觉检测
  skill/         技能加载、扫描、宏、打包、持久化
  mcp/           MCP 连接管理、工具注册
  cron/          定时任务：调度、错过窗口、时区、无头运行
  memory/        文件记忆：schema 注册表、提取编排、写入、召回、向量索引
  todo/          Todo 表定义（工具逻辑在 agent/tools/todo.py）
  trace/         run/span 记录、脱敏、成本计算
  web/           联网搜索、网页抓取、正文提取
```

### 模块结构约定

**只要一个模块有自己的表，它就必须有自己的服务（service/repo），端点统一在 API 层注册。** 不要把所有端点写在一个大 router 里——那会把不相关的关注点混在一起。

## main.py 装配顺序

顺序不能改，每一步都依赖前一步：

```
1. 配置 logging（最先，否则后续步骤的日志丢失）
2. 启动期安全校验（配置不合法直接拒绝启动）
3. create_app() 建 FastAPI 实例
4. 注册中间件（CORS → TraceRequest）
5. 注册异常 handler
6. 注册 router
7. lifespan：建表/检查迁移、初始化 httpx 单例、加载 MCP、加载技能索引
```

### router 在函数内局部导入

```python
def _register_routers(app: FastAPI) -> None:
    # 局部导入：router 模块会 import service，service 会 import models。
    # 放在模块级会形成 main → router → service → models → (某个 core 里回引 main 的东西) 的环。
    # 局部导入让环在函数调用时才成立，此时所有模块已加载完毕。
    from app.modules.session.router import router as session_router
    ...
```

### 启动期安全校验分档

```python
# 分两档，因为后果不同：
# - 拒绝启动：encryption_key 缺失。没有它 API Key 无法解密，
#   所有对话都会失败，且失败信息是"解密错误"而非"没配密钥"，极难排查。
# - 仅告警：host 绑定到非 127.0.0.1。这可能是用户主动要局域网访问，
#   但必须让他知道当前无鉴权。
```

## 异常体系

`core/exceptions.py`：

| 类 | HTTP 状态 | 用途 |
| --- | --- | --- |
| `AppError` | 500 | 基类，带 `code` / `message` |
| `BadRequestError` | 400 | 参数不合法 |
| `NotFoundError` | 404 | 资源不存在 |
| `ConflictError` | 409 | 状态冲突（如重复固定会话） |
| `PathDeniedError` | 403 | 路径白名单/拒止锚拦截 |
| `SandboxError` | 500 | 沙箱执行失败 |
| `ProviderError` | 502 | 上游 LLM / MCP 失败 |

**注意**：工具执行中抛出的异常不走这套体系——它们被 `ToolRegistry.execute()` 捕获转成错误文本给模型。见 [tools.md](tools.md#异常处理)。

## 前后端目录

```
jeeves/
  backend/          见上
  frontend/         Vite + React + TS
  workspace/        默认工作区（gitignore）
  skills/           技能包
  macros/           宏
  personas/         SOUL.md / USER.md / AGENTS.md
  config/           mcp_servers.yaml 等运行时配置（gitignore）
  data/             jeeves.db、上传文件、日志（gitignore）
  docs/             本文档
  scripts/          setup / start
```

运行时文件布局细节见 [../architecture/data-files.md](../architecture/data-files.md)。
