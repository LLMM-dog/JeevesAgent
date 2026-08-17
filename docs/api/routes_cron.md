# 定时任务 API
**路由前缀**: `/api/cron`
**端点数量**: 7
---
## GET 请求
### `GET /api/cron/tasks`
**描述**: 定时任务列表

---
### `GET /api/cron/tasks/{task_id}/runs`
**描述**: 执行历史

**路径参数**:
- `task_id`: (从路径提取)

---
## POST 请求
### `POST /api/cron/tasks`
**描述**: 新建定时任务

**响应类型**: `TaskOut`

---
### `POST /api/cron/tasks/{task_id}/run`
**描述**: 立即执行一次

**路径参数**:
- `task_id`: (从路径提取)

---
### `POST /api/cron/validate`
**描述**: 校验 cron 表达式

---
## PATCH 请求
### `PATCH /api/cron/tasks/{task_id}`
**描述**: 修改定时任务

**路径参数**:
- `task_id`: (从路径提取)

**响应类型**: `TaskOut`

---
## DELETE 请求
### `DELETE /api/cron/tasks/{task_id}`
**描述**: 删除定时任务

**路径参数**:
- `task_id`: (从路径提取)

---
