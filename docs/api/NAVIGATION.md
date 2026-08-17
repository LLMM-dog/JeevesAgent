# Jeeves API 文档导航图

```
docs/api/
│
├─── 📘 入口文档（从这里开始）
│    ├─ README.md ..................... 文档索引，所有链接的起点
│    ├─ QUICK_START.md ................ ⭐ 新手必读：5分钟上手指南
│    ├─ API_OVERVIEW.md ............... 技术总览：数据模型、错误处理
│    ├─ MAINTENANCE.md ................ 维护指南：如何更新文档
│    └─ SUMMARY.md .................... 生成总结：本次任务完成报告
│
├─── 📗 端点文档（按模块查阅）
│    │
│    ├─ 会话与对话 (13 端点) ......... routes_chat.md
│    │  ├─ GET    /api/sessions
│    │  ├─ POST   /api/sessions
│    │  ├─ GET    /api/sessions/{session_id}
│    │  ├─ PATCH  /api/sessions/{session_id}
│    │  ├─ DELETE /api/sessions/{session_id}
│    │  ├─ GET    /api/sessions/{session_id}/messages
│    │  ├─ POST   /api/chat ............... 流式对话（SSE）
│    │  ├─ POST   /api/runs/{run_id}/cancel
│    │  ├─ POST   /api/runs/{run_id}/approve
│    │  └─ ...
│    │
│    ├─ 配置管理 (42 端点) ........... routes_config.md
│    │  ├─ GET    /api/config/endpoints ... 端点列表
│    │  ├─ POST   /api/config/endpoints ... 添加端点
│    │  ├─ GET    /api/config/models ...... 模型列表
│    │  ├─ POST   /api/config/models ...... 添加模型
│    │  ├─ GET    /api/config/purposes .... 功能位列表
│    │  ├─ PATCH  /api/config/purposes/{purpose}
│    │  ├─ GET    /api/config/skills ....... 技能列表
│    │  ├─ POST   /api/config/skills/install
│    │  ├─ GET    /api/config/mcp .......... MCP 服务器
│    │  └─ ...
│    │
│    ├─ 定时任务 (7 端点) ............ routes_cron.md
│    │  ├─ GET    /api/cron/jobs
│    │  ├─ POST   /api/cron/jobs
│    │  ├─ PATCH  /api/cron/jobs/{job_id}
│    │  ├─ DELETE /api/cron/jobs/{job_id}
│    │  └─ ...
│    │
│    ├─ 文件访问 (5 端点) ............ routes_files.md
│    │  ├─ GET    /api/files/list
│    │  ├─ GET    /api/files/read
│    │  ├─ POST   /api/files/write
│    │  ├─ POST   /api/files/mkdir
│    │  └─ DELETE /api/files/delete
│    │
│    ├─ 模型管理 (5 端点) ............ routes_models.md
│    │  ├─ GET    /api/models
│    │  ├─ GET    /api/models/purposes/{purpose}
│    │  ├─ GET    /api/models/{pk}
│    │  └─ ...
│    │
│    ├─ 智能体 (3 端点) .............. agent_router.md
│    │  ├─ GET    /api/agents
│    │  ├─ POST   /api/agents
│    │  └─ GET    /api/agents/{agent_id}
│    │
│    └─ 记忆系统 (7 端点) ............ memory_router.md
│       ├─ GET    /api/memory/list
│       ├─ GET    /api/memory/read
│       ├─ POST   /api/memory/write
│       ├─ POST   /api/memory/search
│       ├─ POST   /api/memory/vectorize
│       ├─ DELETE /api/memory/delete
│       └─ ...
│
└─── 🛠️ 工具与脚本
     └─ scripts/generate_api_docs.py ... 文档自动生成脚本
```

---

## 🚦 快速导航

### 我想...

| 需求 | 前往 |
|------|------|
| 快速了解如何使用 API | [QUICK_START.md](./QUICK_START.md) ⭐ |
| 查看所有可用的端点 | [README.md](./README.md) → 模块列表 |
| 了解数据模型和错误处理 | [API_OVERVIEW.md](./API_OVERVIEW.md) |
| 创建会话并发送消息 | [routes_chat.md](./routes_chat.md) |
| 配置 LLM 端点和模型 | [routes_config.md](./routes_config.md) |
| 管理工作空间文件 | [routes_files.md](./routes_files.md) |
| 使用记忆系统 | [memory_router.md](./memory_router.md) |
| 创建定时任务 | [routes_cron.md](./routes_cron.md) |
| 管理智能体 | [agent_router.md](./agent_router.md) |
| 更新或维护文档 | [MAINTENANCE.md](./MAINTENANCE.md) |

---

## 📚 推荐阅读顺序

### 前端开发者（首次接触）

1. **[QUICK_START.md](./QUICK_START.md)** (15 分钟)
   - 了解基本概念
   - 查看完整示例
   - 复制 TypeScript 代码

2. **[routes_chat.md](./routes_chat.md)** (5 分钟)
   - 会话管理端点
   - 对话流程

3. **[API_OVERVIEW.md](./API_OVERVIEW.md)** (按需查阅)
   - 数据模型定义
   - 错误处理
   - SSE 流式响应

4. **其他模块文档** (按需查阅)
   - 根据功能需求查看对应模块

### 后端开发者（添加新端点）

1. **[MAINTENANCE.md](./MAINTENANCE.md)** (10 分钟)
   - 了解文档生成流程
   - 查看最佳实践

2. **修改代码** → 运行生成脚本 → 提交

3. **[API_OVERVIEW.md](./API_OVERVIEW.md)** (可选)
   - 如需更新数据模型说明

---

## 🔑 关键概念速查

### 会话 (Session)
- **ID**: `ses_xxx`
- **作用**: 管理对话历史和上下文
- **配置**: 工作目录、模型、审批模式等
- **端点**: `/api/sessions`

### 消息 (Message)
- **角色**: `user`, `assistant`, `tool`
- **内容**: 文本、工具调用、附件
- **元数据**: token 数、时间戳、trace ID
- **端点**: `/api/sessions/{session_id}/messages`

### 对话 (Chat)
- **方式**: SSE 流式响应
- **事件**: `delta`, `done`, `error`
- **端点**: `POST /api/chat`

### 端点配置 (Endpoint)
- **作用**: LLM 提供商配置（API Key、Base URL）
- **类型**: OpenAI, Anthropic, Ollama 等
- **端点**: `/api/config/endpoints`

### 模型 (Model)
- **绑定**: 关联到端点
- **能力**: 视觉、工具调用、流式
- **功能位**: chat, title, embedding, extract 等
- **端点**: `/api/config/models`

### 记忆 (Memory)
- **类型**: preferences, events, entities, experiences
- **作用域**: global, agent, session
- **索引**: 向量搜索
- **端点**: `/api/memory`

---

## 📊 端点统计

| 模块 | GET | POST | PATCH | PUT | DELETE | 总计 |
|------|-----|------|-------|-----|--------|------|
| 会话与对话 | 5 | 5 | 1 | 0 | 2 | 13 |
| 配置管理 | 18 | 7 | 15 | 1 | 1 | 42 |
| 定时任务 | 2 | 2 | 1 | 0 | 2 | 7 |
| 文件访问 | 2 | 2 | 0 | 0 | 1 | 5 |
| 模型管理 | 4 | 0 | 0 | 0 | 1 | 5 |
| 智能体 | 2 | 1 | 0 | 0 | 0 | 3 |
| 记忆系统 | 3 | 3 | 0 | 1 | 0 | 7 |
| **总计** | **36** | **20** | **17** | **2** | **7** | **82** |

---

## 🎯 常见任务快捷方式

```typescript
// 1. 初始化应用
GET /api/config/endpoints    // 加载端点
GET /api/config/models        // 加载模型
GET /api/sessions             // 加载会话

// 2. 创建新会话
POST /api/sessions { title: "..." }

// 3. 发送消息
POST /api/chat { session_id: "...", content: "..." }

// 4. 配置 LLM
POST /api/config/endpoints { name: "...", provider: "...", ... }
POST /api/config/models { endpoint_pk: "...", ... }
PATCH /api/config/purposes/chat { default_model_pk: "..." }

// 5. 文件操作
GET /api/files/list?path=/workspace
GET /api/files/read?path=/file.txt
POST /api/files/write { path: "...", content: "..." }
```

---

## 🔗 外部资源

- **FastAPI 文档**: https://fastapi.tiangolo.com/
- **Pydantic 文档**: https://docs.pydantic.dev/
- **SSE 规范**: https://html.spec.whatwg.org/multipage/server-sent-events.html
- **TypeScript 手册**: https://www.typescriptlang.org/docs/

---

**文档版本**: v1.0  
**最后更新**: 2026-08-15  
**维护者**: LLMM-dog
