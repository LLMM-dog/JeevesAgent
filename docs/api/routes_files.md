# 文件访问 API
**路由前缀**: `/api/files`
**端点数量**: 5
---
## GET 请求
### `GET /api/browse`
**描述**: 浏览目录

**响应类型**: `BrowseResult`

---
### `GET /api/whitelist`
**描述**: 白名单列表

**响应类型**: `dict`

---
## POST 请求
### `POST /api/whitelist`
**描述**: 加白名单

**响应类型**: `WhitelistItem`

---
## PATCH 请求
### `PATCH /api/whitelist/{item_id}`
**描述**: 改白名单

**路径参数**:
- `item_id`: (从路径提取)

**响应类型**: `WhitelistItem`

---
## DELETE 请求
### `DELETE /api/whitelist/{item_id}`
**描述**: 删白名单

**路径参数**:
- `item_id`: (从路径提取)

---
