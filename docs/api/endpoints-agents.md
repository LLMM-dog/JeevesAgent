# 多智能体系统 — API 设计

> 所有接口遵循 `docs/api/conventions.md`：成功直接返回数据，错误用 HTTP 状态码 + `detail`

---

## 智能体定义

### `GET /api/agents`

列出所有智能体（不含已删除）。

```
Response 200:
[
  {
    "id": "adf_7bK2mQ9xR4Lp",
    "name": "代码审查员",
    "description": "审查代码质量、安全性和架构",
    "avatar": null,
    "system_prompt": "你是资深代码审查员...",
    "model_id": "anthropic/claude-sonnet-4",
    "skill_names": ["code-review"],
    "mcp_servers": [],
    "permission_read": true,
    "permission_write": false,
    "permission_shell": false,
    "permission_network": false,
    "permission_subagent": false,
    "created_at": 1723100000000,
    "updated_at": 1723100000000
  }
]
```

### `POST /api/agents`

创建智能体。

```
Request:
{
  "name": "研究员",              // 必填，唯一
  "description": "...",
  "system_prompt": "...",        // 默认空
  "model_id": null,              // 默认跟随对话设置
  "skill_names": [],             // 默认空
  "mcp_servers": [],
  "permission_read": true,       // 默认 true
  "permission_write": false,     // 默认 false
  "permission_shell": false,
  "permission_network": false,
  "permission_subagent": false
}

Response 201:
{ "id": "adf_...", ... }
```

### `GET /api/agents/{agent_id}`

获取单个智能体详情。

### `PATCH /api/agents/{agent_id}`

部分更新智能体。传什么改什么，不传的不变。

```
Request:
{ "name": "新名称", "system_prompt": "..." }

Response 200:
{ "id": "...", "name": "新名称", ... }
```

### `DELETE /api/agents/{agent_id}`

软删除。被群组引用的智能体拒绝删除（返回 409）。

```
Response 409:
{ "detail": "智能体被以下群组引用：代码审查组 (agp_xxx)，请先从群组中移除" }
```

---

## 智能体群组

### `GET /api/agent-groups`

列出所有群组。

```
Response 200:
[
  {
    "id": "agp_3kL8nR2xQ5Mp",
    "name": "代码审查组",
    "description": "从安全和性能两个角度审查",
    "group_type": "moa",
    "aggregator_agent_id": "adf_xxx",
    "members": [
      {
        "agent_id": "adf_aaa",
        "agent_name": "安全检查员",
        "sort_order": 1,
        "role": "worker"
      },
      {
        "agent_id": "adf_bbb",
        "agent_name": "性能分析员",
        "sort_order": 2,
        "role": "worker"
      },
      {
        "agent_id": "adf_xxx",
        "agent_name": "汇总员",
        "sort_order": 3,
        "role": "aggregator"
      }
    ],
    "created_at": 1723100000000
  }
]
```

### `POST /api/agent-groups`

创建群组。

```
Request:
{
  "name": "代码审查组",
  "group_type": "moa",
  "members": [
    { "agent_id": "adf_aaa", "sort_order": 1, "role": "worker" },
    { "agent_id": "adf_bbb", "sort_order": 2, "role": "worker" },
    { "agent_id": "adf_xxx", "sort_order": 3, "role": "aggregator" }
  ]
}

Response 201:
{ "id": "agp_...", ... }
```

### `PATCH /api/agent-groups/{group_id}`

更新群组（名称、成员列表等）。

### `DELETE /api/agent-groups/{group_id}`

软删除。

---

## 对话接口改动

### `POST /api/chat` — 增加 agent_id 参数

```
Request:
{
  "session_id": "ses_xxx",
  "message": "...",
  "agent_id": "adf_xxx",        // 新增，可选。不传用默认
  "agent_group_id": "agp_xxx"   // 新增，可选。与 agent_id 互斥
}
```

当传 `agent_group_id` 时，后端启动 GroupRunner 而非单个 AgentLoop。

### `GET /api/sessions/{session_id}` — 返回当前智能体

```
Response 200:
{
  "id": "ses_xxx",
  "agent_id": "adf_xxx",        // 新增
  "agent_name": "代码审查员",    // 新增
  ...
}
```

---

## 对话页初始化

### `GET /api/chat/agents` — 获取可选智能体列表

```
Response 200:
{
  "agents": [...],             // 所有可用智能体
  "groups": [...],             // 所有可用群组
  "default_agent_id": "adf_xxx"  // 用户设定的默认智能体
}
```
