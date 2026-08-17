# 记忆系统 API
**路由前缀**: `/api/memory`
**端点数量**: 15
---

## 向量
- `GET /vectors` — 向量新鲜度统计（never/model/content/fresh）
- `POST /vectors/rebuild` — 一键重算向量（`only_stale=true` 只算失效的）
- `DELETE /vectors` — 清空所有向量（回落关键词搜索）

## 设置
- `GET /settings` — 可调设置项与当前值
- `PUT /settings` — 修改设置（立即生效）
- `POST /settings/reset` — 恢复默认设置

## 记忆 CRUD
- `GET /search` — 语义搜索（q + agent_id/session_id/memory_type 三层范围）
- `GET /list` — 列举记忆元数据（管理视角，全部智能体 = 全局 + 所有 agent）
- `GET /read` — 读取单条记忆全文（uri）
- `POST /write` — 写入/更新记忆（scope + memory_type + fields）
- `DELETE /delete` — 删除记忆（uri，同时删文件 + 索引）

## 维护
- `POST /vectorize` — 手动向量化指定 uri 列表
- `POST /init-agent` — 初始化智能体记忆目录（幂等）

## 痕迹
- `GET /traces` — 列举记忆变更痕迹（按 agent/session 过滤，agent 留空列全部）
- `GET /traces/{extraction_id}` — 读取单次提取的痕迹详情
