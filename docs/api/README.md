# Jeeves API 文档索引

> **前端开发必读**：所有后端 API 接口的完整文档

---

## 🚀 快速开始

**新手？从这里开始！**

1. **[快速上手指南](./QUICK_START.md)** ⭐ 推荐首读
   - 5 分钟掌握核心用法
   - 完整的请求/响应示例
   - TypeScript + React 集成代码
   - 常见场景解决方案

2. **[API 总览](./API_OVERVIEW.md)** - 技术细节
   - 基础信息（Base URL、认证）
   - 通用响应格式
   - 数据模型定义
   - 错误处理
   - SSE 流式响应

3. 按模块查看详细端点文档（见下方）

---

## 📂 模块列表

| 模块 | 端点数 | 路由前缀 | 文档 |
|------|--------|----------|------|
| 会话与对话 | 13 | `/api/sessions` | [routes_chat.md](./routes_chat.md) |
| 配置管理 | 42 | `/api/config` | [routes_config.md](./routes_config.md) |
| 定时任务 | 7 | `/api/cron` | [routes_cron.md](./routes_cron.md) |
| 文件访问 | 5 | `/api/files` | [routes_files.md](./routes_files.md) |
| 模型管理 | 5 | `/api/models` | [routes_models.md](./routes_models.md) |
| 智能体 | 3 | `/api/agents` | [agent_router.md](./agent_router.md) |
| 记忆系统 | 7 | `/api/memory` | [memory_router.md](./memory_router.md) |

**总计**: 82 个端点

---

## 🔥 最常用的端点

### 会话管理
```
GET    /api/sessions                       # 会话列表
POST   /api/sessions                       # 新建会话
GET    /api/sessions/{session_id}          # 会话详情
PATCH  /api/sessions/{session_id}          # 修改会话
DELETE /api/sessions/{session_id}          # 删除会话
```

### 对话
```
POST   /api/chat                            # 发送消息（SSE 流式）
GET    /api/sessions/{session_id}/messages # 消息历史
POST   /api/runs/{run_id}/cancel           # 取消生成
```

### 配置
```
GET    /api/config/endpoints                # 端点列表
GET    /api/config/models                   # 模型列表
GET    /api/config/purposes                 # 功能位列表
PATCH  /api/config/purposes/{purpose}       # 绑定默认模型
```

### 文件
```
GET    /api/files/list?path=/workspace      # 列出目录
GET    /api/files/read?path=/file.txt       # 读取文件
POST   /api/files/write                     # 写入文件
```

---

## 🎯 按场景查找

### 场景 1: 初始化前端应用
1. `GET /api/config/endpoints` - 加载端点配置
2. `GET /api/config/models` - 加载模型列表
3. `GET /api/sessions` - 加载会话列表
4. `GET /api/agents` - 加载智能体列表

### 场景 2: 创建新会话并对话
1. `POST /api/sessions` - 创建会话
2. `POST /api/chat` - 发送消息（SSE）
3. `GET /api/sessions/{session_id}/messages` - 获取历史

### 场景 3: 配置 LLM 端点
1. `POST /api/config/endpoints` - 添加端点
2. `POST /api/config/models` - 添加模型
3. `PATCH /api/config/purposes/chat` - 设为默认

### 场景 4: 工作空间文件操作
1. `GET /api/files/list?path=/` - 浏览目录
2. `GET /api/files/read?path=/file.txt` - 读取文件
3. `POST /api/files/write` - 保存文件

### 场景 5: 记忆管理
1. `GET /api/memory/list?agent_id=alice` - 列出记忆
2. `POST /api/memory/search` - 搜索记忆
3. `DELETE /api/memory/delete?uri=...` - 删除记忆

---

## 📝 数据模型快速参考

### SessionDetail
```typescript
{
  id: string
  title: string
  workspace_id: string
  pinned: boolean
  message_count: number
  last_message_at: number
  approval_mode: "auto" | "manual"
  work_dir: string
  model_pk: string
  agent_id: string
  context_window: number
  // ... 其他字段
}
```

### MessageOut
```typescript
{
  id: string
  seq: number
  role: "user" | "assistant" | "tool"
  content: string
  tool_calls: ToolCall[] | null
  refs: Reference[] | null
  attachments: string[] | null
  // ... 其他字段
}
```

### EndpointOut
```typescript
{
  pk: string
  name: string
  provider: string
  base_url: string
  api_key_encrypted: string
  enabled: boolean
  default_model: string
}
```

完整模型定义见 [API_OVERVIEW.md](./API_OVERVIEW.md#数据模型)

---

## ⚠️ 重要说明

### 字段命名约定
- **使用 `snake_case`**（与数据库和 TypeScript 一致）
- **不做 camelCase 转换**（避免跨层 bug）

### SSE 流式响应
- 对话接口 `/api/chat` 返回 SSE 流
- 事件类型：`delta`, `done`, `error`
- 详见 [API_OVERVIEW.md - SSE 流式响应](./API_OVERVIEW.md#sse-流式响应)

### 错误处理
- 所有错误返回统一格式：`{ detail: { code, message, hint } }`
- HTTP 状态码遵循 RESTful 标准
- 详见 [API_OVERVIEW.md - 错误处理](./API_OVERVIEW.md#错误处理)

---

## 🛠️ 开发工具

### 自动生成文档
```bash
python scripts/generate_api_docs.py
```

### TypeScript 类型生成
建议根据 `backend/app/api/schemas.py` 手动或自动生成类型定义。

### API 客户端示例
参考 [API_OVERVIEW.md - 开发建议](./API_OVERVIEW.md#开发建议)

---

## 📚 相关资源

- **后端源码**: `backend/app/api/` 和 `backend/app/modules/*/router.py`
- **Schema 定义**: `backend/app/api/schemas.py`
- **主应用**: `backend/app/main.py`

---

**文档版本**: v1.0  
**最后更新**: 2026-08-15  
**生成脚本**: `scripts/generate_api_docs.py`

