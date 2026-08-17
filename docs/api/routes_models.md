# 模型管理 API
**路由前缀**: `/api/models`
**端点数量**: 5
---
## GET 请求
### `GET /api/context-overhead`
**描述**: 固定上下文开销

---
### `GET /api/endpoints/{endpoint_id}/available-models`
**描述**: 拉取端点可用的模型列表

**路径参数**:
- `provider_id`: (从路径提取)

**响应类型**: `dict`

---
## POST 请求
### `POST /api/models`
**描述**: 添加单个模型

**响应类型**: `ModelOut`

---
## PATCH 请求
### `PATCH /api/models/{model_pk}`
**描述**: 改模型

**路径参数**:
- `model_pk`: (从路径提取)

**响应类型**: `ModelOut`

---
## DELETE 请求
### `DELETE /api/models/{model_pk}`
**描述**: 删除单个模型

**路径参数**:
- `model_pk`: (从路径提取)

---
