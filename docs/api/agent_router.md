# 智能体 API
**路由前缀**: `/api/agents`
**端点数量**: 5
---

## GET 请求
### `GET /api/agents`
**描述**: 智能体列表（支持 hidden/using_skill/using_mcp 过滤）

### `GET /api/agents/{agent_id}`
**描述**: 单个智能体详情

**路径参数**:
- `agent_id`: (从路径提取)

---

## POST 请求
### `POST /api/agents`
**描述**: 创建智能体

---

## PATCH 请求
### `PATCH /api/agents/{agent_id}`
**描述**: 修改智能体（字段全可选，只更新非 None 的）

**路径参数**:
- `agent_id`: (从路径提取)

---

## DELETE 请求
### `DELETE /api/agents/{agent_id}`
**描述**: 删除智能体（默认智能体不可删）

**路径参数**:
- `agent_id`: (从路径提取)

---
