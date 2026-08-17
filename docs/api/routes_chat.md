# 会话与对话 API
**路由前缀**: `/api/sessions`
**端点数量**: 13
---
## GET 请求
### `GET /api/sessions`
**描述**: 会话列表

**响应类型**: `SessionListResponse`

---
### `GET /api/sessions/{session_id}`
**描述**: 会话详情

**路径参数**:
- `session_id`: (从路径提取)

**响应类型**: `SessionDetail`

---
### `GET /api/sessions/{session_id}/active-run`
**描述**: 这个会话有没有正在跑的 run

**路径参数**:
- `session_id`: (从路径提取)

---
### `GET /api/sessions/{session_id}/export`
**描述**: 导出会话

**路径参数**:
- `session_id`: (从路径提取)

---
### `GET /api/sessions/{session_id}/messages`
**描述**: 会话消息（不分页）

**路径参数**:
- `session_id`: (从路径提取)

**响应类型**: `MessageListResponse`

---
## POST 请求
### `POST /api/chat`
**描述**: 对话（SSE 流式）

---
### `POST /api/runs/{run_id}/answer`
**描述**: 回答交互提问

**路径参数**:
- `run_id`: (从路径提取)

---
### `POST /api/runs/{run_id}/approve`
**描述**: 审批工具调用

**路径参数**:
- `run_id`: (从路径提取)

---
### `POST /api/runs/{run_id}/cancel`
**描述**: 取消生成

**路径参数**:
- `run_id`: (从路径提取)

**响应类型**: `CancelResponse`

---
### `POST /api/sessions`
**描述**: 新建会话

**响应类型**: `SessionDetail`

---
## PATCH 请求
### `PATCH /api/sessions/{session_id}`
**描述**: 修改会话

**路径参数**:
- `session_id`: (从路径提取)

**响应类型**: `SessionDetail`

---
## DELETE 请求
### `DELETE /api/sessions/{session_id}`
**描述**: 删除会话

**路径参数**:
- `session_id`: (从路径提取)

---
### `DELETE /api/sessions/{session_id}/messages/{message_id}`
**描述**: 从该消息处截断（用于重发）

**路径参数**:
- `session_id`: (从路径提取)
- `message_id`: (从路径提取)

---
