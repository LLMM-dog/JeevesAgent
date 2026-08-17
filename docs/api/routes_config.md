# 配置管理 API
**路由前缀**: `/api/config`
**端点数量**: 42
---
## GET 请求
### `GET /api/bindings`
**描述**: 功能位绑定

**响应类型**: `BindingListResponse`

---
### `GET /api/endpoints`
**描述**: 模型组列表

**响应类型**: `EndpointListResponse`

---
### `GET /api/mcp/pending-approval`
**描述**: 待确认的 stdio 启动命令

---
### `GET /api/mcp/servers`
**描述**: MCP 服务器状态

---
### `GET /api/mcp/servers/{server_id}`
**描述**: 查看单个 MCP 服务器详情

**路径参数**:
- `server_id`: (从路径提取)

---
### `GET /api/meta`
**描述**: 运行时元信息

**响应类型**: `MetaResponse`

---
### `GET /api/models`
**描述**: 模型列表

**响应类型**: `ModelListResponse`

---
### `GET /api/ref-candidates`
**描述**: 引用候选（@ 提词器用）

---
### `GET /api/sessions/{session_id}/todos`
**描述**: 任务清单

**路径参数**:
- `session_id`: (从路径提取)

**响应类型**: `TodoListResponse`

---
### `GET /api/skills`
**描述**: 技能列表

---
### `GET /api/traces`
**描述**: 执行记录列表

---
### `GET /api/traces-sessions`
**描述**: 按会话汇总的执行记录

---
### `GET /api/traces-stats`
**描述**: 追踪表统计

---
### `GET /api/traces/{run_id}`
**描述**: 执行树

**路径参数**:
- `run_id`: (从路径提取)

---
### `GET /api/websearch`
**描述**: 联网搜索状态

---
## POST 请求
### `POST /api/endpoints`
**描述**: 添加模型组（API 端点）

**响应类型**: `EndpointOut`

---
### `POST /api/endpoints/probe`
**描述**: 探测模型列表

**响应类型**: `ProbeResponse`

---
### `POST /api/images/upload`
**描述**: 上传图片

---
### `POST /api/mcp/reload`
**描述**: 重载 MCP 配置

---
### `POST /api/mcp/servers`
**描述**: 添加 MCP 服务器

---
### `POST /api/models/{model_pk}/verify-vision`
**描述**: 核验图片输入能力

**路径参数**:
- `model_pk`: (从路径提取)

---
### `POST /api/sessions/{session_id}/todos/archive`
**描述**: 验收关闭

**路径参数**:
- `session_id`: (从路径提取)

---
### `POST /api/skills/reload`
**描述**: 重扫技能目录

---
### `POST /api/skills/upload`
**描述**: 上传技能包（zip）

---
### `POST /api/traces/cleanup`
**描述**: 清理过期追踪

---
## PUT 请求
### `PUT /api/bindings`
**描述**: 设置功能位（upsert）

---
### `PUT /api/websearch`
**描述**: 开关联网搜索

---
## PATCH 请求
### `PATCH /api/mcp/servers/{server_id}`
**描述**: 修改 MCP 服务器

**路径参数**:
- `server_id`: (从路径提取)

---
### `PATCH /api/mcp/servers/{server_id}/enabled`
**描述**: 开关一个 MCP 服务器

**路径参数**:
- `server_id`: (从路径提取)

---
### `PATCH /api/skills/{name}/enabled`
**描述**: 开关一个技能

**路径参数**:
- `name`: (从路径提取)

---
### `PATCH /api/todos/{todo_id}`
**描述**: 修改任务

**路径参数**:
- `todo_id`: (从路径提取)

**响应类型**: `TodoOut`

---
## DELETE 请求
### `DELETE /api/endpoints/{endpoint_id}`
**描述**: 删除模型组

**路径参数**:
- `endpoint_id`: (从路径提取)

---
### `DELETE /api/mcp/servers/{server_id}`
**描述**: 删除 MCP 服务器

**路径参数**:
- `server_id`: (从路径提取)

---
### `DELETE /api/skills/{name}`
**描述**: 删除技能

**路径参数**:
- `name`: (从路径提取)

---
### `DELETE /api/todos/{todo_id}`
**描述**: 删除任务

**路径参数**:
- `todo_id`: (从路径提取)

---
