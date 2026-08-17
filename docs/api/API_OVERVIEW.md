# Jeeves API 完整文档

> **前端开发必读**：所有后端 API 接口的详细说明

## 📋 目录

- [概述](#概述)
- [认证与授权](#认证与授权)
- [通用响应格式](#通用响应格式)
- [API 模块](#api-模块)
- [数据模型](#数据模型)
- [错误处理](#错误处理)

---

## 概述

### 基础信息

- **Base URL**: `http://localhost:9000/api` (开发环境)
- **协议**: HTTP/1.1
- **数据格式**: JSON
- **字符编码**: UTF-8
- **字段命名**: `snake_case` (与数据库和前端 TypeScript 保持一致)

### 环境变量

```bash
JEEVES_APP__PORT=9000              # 服务端口
JEEVES_API__PREFIX=/api            # API 路由前缀
```

---

## 认证与授权

**当前版本**：暂无认证机制（单用户本地应用）

> 📌 **待办**：多用户版本将实现基于 JWT 的认证

---

## 通用响应格式

### 成功响应

```json
{
  "data": { /* 具体数据 */ },
  "status": "success"
}
```

### 错误响应

```json
{
  "detail": {
    "code": "NOT_FOUND",
    "message": "会话不存在",
    "hint": "session_id: abc123"
  }
}
```

**常见错误码**:
- `BAD_REQUEST` (400) - 请求参数错误
- `NOT_FOUND` (404) - 资源不存在
- `CONFLICT` (409) - 资源冲突
- `INTERNAL_ERROR` (500) - 服务器内部错误

---

## API 模块

### 1. 会话与对话 (`/api/sessions`)

**总计**: 13 个端点  
**文档**: [routes_chat.md](./routes_chat.md)

核心功能：
- 会话 CRUD（创建、查询、修改、删除）
- 流式对话（SSE）
- 消息历史
- Run 控制（取消、审批、回答）

**关键端点**:
```
GET    /api/sessions                       # 会话列表
POST   /api/sessions                       # 新建会话
GET    /api/sessions/{session_id}          # 会话详情
PATCH  /api/sessions/{session_id}          # 修改会话配置
DELETE /api/sessions/{session_id}          # 删除会话
GET    /api/sessions/{session_id}/messages # 消息历史
POST   /api/chat                            # 对话（SSE 流式）
POST   /api/runs/{run_id}/cancel           # 取消生成
POST   /api/runs/{run_id}/approve          # 审批工具调用
GET    /api/sessions/{session_id}/export   # 导出为 Markdown
```

---

### 2. 配置管理 (`/api/config`)

**总计**: 42 个端点  
**文档**: [routes_config.md](./routes_config.md)

涵盖所有系统配置：
- **端点配置** (Endpoints): LLM 提供商、API 密钥
- **模型配置** (Models): 模型列表、功能位绑定
- **技能管理** (Skills): 技能安装、启用、禁用
- **宏管理** (Macros): 快捷指令
- **MCP 服务器** (MCP): Model Context Protocol 集成
- **追踪配置** (Tracing): 日志与监控

**关键端点**:
```
# 端点
GET    /api/config/endpoints
POST   /api/config/endpoints
PATCH  /api/config/endpoints/{pk}
DELETE /api/config/endpoints/{pk}

# 模型
GET    /api/config/models
POST   /api/config/models
GET    /api/config/purposes                # 功能位列表
PATCH  /api/config/purposes/{purpose}      # 绑定默认模型

# 技能
GET    /api/config/skills
POST   /api/config/skills/install          # 安装技能
PATCH  /api/config/skills/{skill_id}       # 启用/禁用

# MCP
GET    /api/config/mcp
POST   /api/config/mcp
GET    /api/config/mcp/{server}/tools      # 查看 MCP 工具
```

---

### 3. 定时任务 (`/api/cron`)

**总计**: 7 个端点  
**文档**: [routes_cron.md](./routes_cron.md)

管理后台定时任务：
- 列表、创建、修改、删除任务
- 启用/禁用任务
- 查看执行历史

**关键端点**:
```
GET    /api/cron/jobs
POST   /api/cron/jobs
PATCH  /api/cron/jobs/{job_id}
DELETE /api/cron/jobs/{job_id}
GET    /api/cron/jobs/{job_id}/history     # 执行记录
```

---

### 4. 文件访问 (`/api/files`)

**总计**: 5 个端点  
**文档**: [routes_files.md](./routes_files.md)

工作空间文件管理：
- 列出目录内容
- 读取文件
- 写入文件
- 创建目录

**关键端点**:
```
GET    /api/files/list                     # 列出目录（查询参数: path）
GET    /api/files/read                     # 读取文件（查询参数: path）
POST   /api/files/write                    # 写入文件
POST   /api/files/mkdir                    # 创建目录
DELETE /api/files/delete                   # 删除文件/目录
```

---

### 5. 模型管理 (`/api/models`)

**总计**: 5 个端点  
**文档**: [routes_models.md](./routes_models.md)

模型下拉选择和能力查询：
- 可用模型列表（按功能位筛选）
- 模型能力检查（视觉、工具调用）

**关键端点**:
```
GET    /api/models                         # 所有可用模型
GET    /api/models/purposes/{purpose}      # 特定功能位的模型
GET    /api/models/{pk}                    # 模型详情
GET    /api/models/{pk}/capabilities       # 模型能力
```

---

### 6. 智能体 (`/api/agents`)

**总计**: 3 个端点  
**文档**: [agent_router.md](./agent_router.md)

智能体管理：
- 列表、创建、更新智能体
- 查询智能体详情

**关键端点**:
```
GET    /api/agents
POST   /api/agents
GET    /api/agents/{agent_id}
```

---

### 7. 记忆系统 (`/api/memory`)

**总计**: 7 个端点  
**文档**: [memory_router.md](./memory_router.md)

长期记忆管理：
- 列出记忆（按类型/作用域）
- 读取/写入记忆
- 向量化/搜索记忆
- 删除记忆

**关键端点**:
```
GET    /api/memory/list                    # 列出记忆（查询: agent_id, session_id, memory_type）
GET    /api/memory/read                    # 读取记忆（查询: uri）
POST   /api/memory/write                   # 写入记忆
POST   /api/memory/vectorize               # 向量化记忆
POST   /api/memory/search                  # 搜索记忆
DELETE /api/memory/delete                  # 删除记忆（查询: uri）
POST   /api/memory/init-agent              # 初始化智能体记忆
```

---

## 数据模型

### 会话 (Session)

**SessionBrief** (列表项)
```typescript
{
  id: string                // 会话 ID
  title: string             // 会话标题
  workspace_id: string      // 工作空间 ID
  pinned: boolean           // 是否置顶
  message_count: number     // 消息数量
  last_message_at: number   // 最后消息时间（Unix 时间戳）
  created_at: number        // 创建时间
}
```

**SessionDetail** (详细信息)
```typescript
{
  ...SessionBrief,
  approval_mode: "auto" | "manual"  // 工具调用审批模式
  work_dir: string                  // 工作目录（空串 = 未设置）
  model_pk: string                  // 模型主键（空串 = 默认）
  context_window: number            // 上下文窗口大小
  private_mode: boolean             // 隐私模式（对话不进记忆）
  amnesia_mode: boolean             // 健忘模式（不读历史记忆）
  vision_mode: boolean              // 视觉模式（支持图片）
  agent_id: string                  // 智能体 ID（空串 = 默认）
}
```

### 消息 (Message)

**MessageOut**
```typescript
{
  id: string
  seq: number                       // 序号
  role: "user" | "assistant" | "tool"
  agent_name: string                // 智能体名称
  content: string                   // 消息内容
  reasoning: string | null          // 推理过程（o1 模型）
  tool_calls: ToolCall[] | null     // 工具调用
  tool_call_id: string | null       // 工具调用 ID
  tool_name: string | null          // 工具名称
  tool_display: object | null       // 工具展示信息
  is_error: boolean                 // 是否错误
  refs: Reference[] | null          // 引用（RAG）
  attachments: string[] | null      // 附件路径
  artifact_kind: string | null      // 附件类型
  artifact_path: string | null      // 附件路径
  run_id: string | null             // Run ID
  span_id: string | null            // Trace Span ID
  prompt_tokens: number | null      // 输入 token
  completion_tokens: number | null  // 输出 token
  created_at: number                // 创建时间
}
```

### 端点配置 (Endpoint)

**EndpointOut**
```typescript
{
  pk: string                        // 主键
  name: string                      // 名称
  provider: string                  // 提供商（openai/anthropic/ollama...）
  base_url: string                  // API 基础 URL
  api_key_encrypted: string         // 加密的 API Key
  enabled: boolean                  // 是否启用
  default_model: string             // 默认模型
  extra: object                     // 额外配置
  created_at: number
  updated_at: number
}
```

### 模型配置 (Model)

**ModelOut**
```typescript
{
  pk: string                        // 主键
  endpoint_pk: string               // 端点主键
  name: string                      // 模型名称（显示用）
  model_name: string                // 模型 ID（API 调用用）
  context_window: number            // 上下文窗口
  max_output_tokens: number         // 最大输出 token
  supports_vision: boolean          // 是否支持视觉
  supports_tools: boolean           // 是否支持工具
  supports_streaming: boolean       // 是否支持流式
  enabled: boolean
  pricing: {                        // 定价
    input_per_million: number
    output_per_million: number
  }
  created_at: number
  updated_at: number
}
```

### 记忆 (Memory)

**MemoryItem**
```typescript
{
  uri: string                       // 唯一标识（如 agents/alice/preferences/Python.md）
  scope: "global" | "agent" | "session"
  memory_type: string               // preferences/events/entities/experiences
  agent_id: string
  session_id: string
  peer_agent_id: string
  title: string                     // 标题
  body: string                      // 正文（Markdown）
  version: number                   // 版本号
  updated_at: number
}
```

---

## 错误处理

### HTTP 状态码

| 状态码 | 说明 | 示例场景 |
|--------|------|----------|
| 200 | 成功 | 正常返回数据 |
| 201 | 创建成功 | 新建会话/端点 |
| 204 | 成功无内容 | 删除操作 |
| 400 | 请求错误 | 参数缺失/格式错误 |
| 404 | 未找到 | 会话/模型不存在 |
| 409 | 冲突 | 资源已存在 |
| 500 | 服务器错误 | 内部异常 |

### 错误响应示例

```json
{
  "detail": {
    "code": "NOT_FOUND",
    "message": "会话不存在",
    "hint": "session_id: abc123"
  }
}
```

### 前端错误处理建议

```typescript
async function apiCall() {
  try {
    const response = await fetch('/api/sessions');
    
    if (!response.ok) {
      const error = await response.json();
      // 处理错误
      console.error(error.detail.message);
      throw new Error(error.detail.message);
    }
    
    return await response.json();
  } catch (error) {
    // 网络错误或解析错误
    console.error('API 调用失败:', error);
    throw error;
  }
}
```

---

## SSE 流式响应

### 对话接口 (`POST /api/chat`)

**请求**:
```json
{
  "session_id": "abc123",
  "content": "你好",
  "attachments": []
}
```

**响应** (SSE):
```
event: delta
data: {"content": "你"}

event: delta
data: {"content": "好"}

event: delta
data: {"content": "！"}

event: done
data: {"message_id": "msg_123", "tokens": {"prompt": 10, "completion": 5}}
```

**前端示例**:
```typescript
const eventSource = new EventSource('/api/chat', {
  method: 'POST',
  body: JSON.stringify(request),
});

eventSource.addEventListener('delta', (e) => {
  const data = JSON.parse(e.data);
  appendToMessage(data.content);
});

eventSource.addEventListener('done', (e) => {
  const data = JSON.parse(e.data);
  finishMessage(data.message_id);
  eventSource.close();
});

eventSource.addEventListener('error', (e) => {
  console.error('SSE error:', e);
  eventSource.close();
});
```

---

## 快速开始

### 1. 创建会话

```bash
curl -X POST http://localhost:9000/api/sessions \
  -H "Content-Type: application/json" \
  -d '{"title": "测试会话"}'
```

### 2. 发送消息

```bash
curl -X POST http://localhost:9000/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "abc123",
    "content": "你好，Jeeves！"
  }'
```

### 3. 查看消息历史

```bash
curl http://localhost:9000/api/sessions/abc123/messages
```

---

## 开发建议

### TypeScript 类型定义

建议根据 `backend/app/api/schemas.py` 生成 TypeScript 类型：

```typescript
// 自动生成或手动维护
export interface SessionDetail {
  id: string;
  title: string;
  workspace_id: string;
  // ... 其他字段
}
```

### API 客户端封装

```typescript
class JeevesClient {
  private baseURL = 'http://localhost:9000/api';

  async getSessions(): Promise<SessionListResponse> {
    const response = await fetch(`${this.baseURL}/sessions`);
    return response.json();
  }

  async createSession(title: string): Promise<SessionDetail> {
    const response = await fetch(`${this.baseURL}/sessions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    });
    return response.json();
  }

  // ... 其他方法
}
```

---

## 附录

### 相关文档

- [会话与对话 API](./routes_chat.md)
- [配置管理 API](./routes_config.md)
- [定时任务 API](./routes_cron.md)
- [文件访问 API](./routes_files.md)
- [模型管理 API](./routes_models.md)
- [智能体 API](./agent_router.md)
- [记忆系统 API](./memory_router.md)

### 后端代码位置

- **路由定义**: `backend/app/api/*.py` 和 `backend/app/modules/*/router.py`
- **数据模型**: `backend/app/api/schemas.py`
- **主应用**: `backend/app/main.py`

---

**文档版本**: v1.0  
**最后更新**: 2026-08-15  
**总端点数**: 82
