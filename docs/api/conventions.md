# 接口通用约定

## 基础

- 前缀：`/api`
- 编码：UTF-8，`Content-Type: application/json`
- 时间：全部 UTC 毫秒整数
- 字段命名：**snake_case**，前后端一致，不做转换

## 成功响应直接返回数据

不套信封。

```jsonc
// GET /api/sessions/ses_7bK2mQ9xR4Lp
{
  "id": "ses_7bK2mQ9xR4Lp",
  "title": "重构登录模块",
  "workspace_id": "wsp_3nF8kL2pQ7xY",
  "pinned": false,
  "approval_mode": "manual",
  "created_at": 1785312000000
}
```

**为什么不用 `{code, message, data}` 信封**：

有一种做法是统一包 `Result[T]`，前端在拦截器里再解一层 `payload.data`。这在多用户企业项目里有价值（统一业务错误码给前端做国际化）。个人项目里它只带来成本：

- 每个接口的类型定义要包一层
- HTTP 状态码和业务码两套语义并存，"到底该看哪个"每次都要想
- FastAPI 自动生成的 OpenAPI 文档变得难读

HTTP 状态码本身就是标准的错误信道，够用。

## 错误响应

用 HTTP 状态码 + FastAPI 默认的 `detail` 结构：

```jsonc
// 404
{
  "detail": {
    "code": "session_not_found",
    "message": "会话不存在",
    "hint": null
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `code` | 机器可读的错误标识，snake_case |
| `message` | 给用户看的中文说明 |
| `hint` | 可选。修复建议 |

### 状态码用法

| 码 | 场景 |
| --- | --- |
| 200 | 成功（含 DELETE 成功） |
| 201 | 创建成功，响应体是新建的资源 |
| 400 | 参数不合法 |
| 403 | 路径被白名单/拒止锚拒绝 |
| 404 | 资源不存在 |
| 409 | 状态冲突（重名技能、run 已结束却来审批） |
| 422 | FastAPI 的 Pydantic 校验失败（自动产生） |
| 500 | 未预期的服务端错误 |
| 502 | 上游 LLM / MCP 失败 |

不用 401/403 做鉴权语义——本项目无鉴权。403 专用于路径拒绝。

### 422 的处理

FastAPI 的 `RequestValidationError` 默认返回的结构和上面不一致。注册一个 handler 统一成同样的形状：

```python
{
  "detail": {
    "code": "validation_error",
    "message": "请求参数不合法",
    "hint": "body.title: 字段不能为空"
  }
}
```

前端只需处理一种错误结构。

## 错误码清单

| code | 状态 | 说明 |
| --- | --- | --- |
| `validation_error` | 422 | 参数校验失败 |
| `session_not_found` | 404 | |
| `message_not_found` | 404 | |
| `run_not_found` | 404 | |
| `run_already_finished` | 409 | 对已结束的 run 做审批/取消 |
| `todo_not_found` | 404 | |
| `provider_not_found` | 404 | |
| `model_not_found` | 404 | |
| `no_model_bound` | 400 | 未配置 chat 位模型 |
| `provider_probe_failed` | 502 | 模型探测失败，`hint` 里带具体原因 |
| `upstream_error` | 502 | LLM 调用失败 |
| `path_denied` | 403 | 白名单拒绝 |
| `path_blocked_by_anchor` | 403 | 拒止锚阻断 |
| `skill_not_found` | 404 | |
| `skill_already_exists` | 409 | 上传同名技能未确认覆盖 |
| `skill_package_invalid` | 400 | zip 校验失败，`hint` 带具体问题 |
| `file_too_large` | 400 | |
| `mcp_server_unavailable` | 502 | |
| `sandbox_error` | 500 | |
| `encryption_not_configured` | 500 | 加密密钥缺失 |

## 列表接口

统一分页参数，不做游标分页。

```
GET /api/sessions?page=1&size=20
```

```jsonc
{
  "items": [ /* ... */ ],
  "total": 137,
  "page": 1,
  "size": 20,
  "pages": 7
}
```

`page` 从 1 开始，`size` 上限 100，默认 20。

**为什么不用游标分页**：个人项目的数据量下 offset 分页的性能问题不会出现，而游标分页让"跳到第 5 页"这种前端需求变复杂。

### 例外：不分页的接口

以下返回全量，因为数据量天然有界：

- `GET /api/sessions/{id}/messages` —— 一个会话的消息（前端需要完整时间线）
- `GET /api/sessions/{id}/todos` —— 几十条
- `GET /api/endpoints` —— 几个
- `GET /api/skills` —— 几十个
- `GET /api/settings/whitelist` —— 几条

消息接口如果某个会话真的膨胀到几千条，再加分页。届时前端要做虚拟滚动（`react-virtuoso`）配合。

## 命名规范

| 操作 | 方法与路径 |
| --- | --- |
| 列表 | `GET /api/{资源复数}` |
| 详情 | `GET /api/{资源复数}/{id}` |
| 创建 | `POST /api/{资源复数}` |
| 全量更新 | `PUT /api/{资源复数}/{id}` |
| 部分更新 | `PATCH /api/{资源复数}/{id}` |
| 删除 | `DELETE /api/{资源复数}/{id}` |
| 子资源 | `GET /api/{资源复数}/{id}/{子资源复数}` |
| 动作 | `POST /api/{资源复数}/{id}/{动词}` |

路径用**复数**（`/api/sessions`），术语表里的实体名是**单数**（`session`）。表名也是单数。这三者的差异是刻意的：REST 路径习惯复数，而表名和类型名单数更自然。

动作类端点用动词，如 `POST /api/runs/{id}/cancel`、`POST /api/mcp/reload`。不硬套 REST。

## PATCH 的语义

只更新请求体里出现的字段。未出现的字段保持不变。

```jsonc
// PATCH /api/sessions/ses_xxx
{ "pinned": true }        // 只改 pinned，title 等不动
```

区分"字段不存在"和"字段为 null"：Pydantic 里用 `Field(default=UNSET)` 模式或 `model_fields_set` 判断。

**这个区分很重要**：`api_key: null` 应该报错（不能设为空），而不传 `api_key` 表示保持原值。

## 幂等性

| 方法 | 幂等 |
| --- | --- |
| GET / PUT / DELETE | 是 |
| POST | 否，除了下面几个 |

`POST /api/runs/{id}/cancel` 幂等——重复取消返回 200，不报错。用户可能连点两次。

`POST /api/runs/{id}/approve` **不幂等**——重复审批返回 409 `run_already_finished`。

## CORS

开发时前端跑在 5173，后端 8000，需要 CORS。

```python
allow_origins = ["http://localhost:5173", "http://127.0.0.1:5173"]
allow_credentials = False      # 无 cookie 鉴权，不需要
```

**不用 `allow_origins=["*"]`**。虽然本项目无鉴权，通配符仍然允许任意网页向本地 API 发请求（一个恶意网页可以让你的 agent 执行命令）。显式列出开发端口。

生产（前端构建后由后端托管静态文件）时同源，不需要 CORS。

## OpenAPI 文档

FastAPI 自动生成，`/docs` 可访问。

要求：

- 每个端点有 `summary`（中文一句话）
- Pydantic 模型的字段用 `Field(description=...)` 标注
- 错误响应用 `responses={404: {...}}` 声明

自动文档不能替代本目录的手写文档——自动文档说明"接口长什么样"，手写文档说明"为什么这样设计、有什么约束"。

## 请求日志

`TraceRequestMiddleware` 给每个请求生成 `request_id`，注入 structlog 上下文。响应头带 `X-Request-Id`。

排查问题时，前端控制台的 `X-Request-Id` 能直接对应到日志里的一整串记录。
