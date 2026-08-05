# 接口：配置与扩展

通用约定见 [conventions.md](conventions.md)。

## 供应商与模型

### POST /api/providers/probe

**用户点名的核心功能。** 探测模型列表，纯查询，不落库。

```jsonc
// 请求
{
  "base_url": "https://api.deepseek.com",
  "api_key": "sk-xxxxxxxx"
}
```

```jsonc
// 200
{
  "normalized_base_url": "https://api.deepseek.com/v1",
  "models": [
    {
      "model_id": "deepseek-chat",
      "context_window": 65536,
      "window_source": "matched",
      "supports_vision": "unknown",
      "supports_tools": "unknown"
    },
    {
      "model_id": "deepseek-reasoner",
      "context_window": 65536,
      "window_source": "matched",
      "supports_vision": "unknown",
      "supports_tools": "unknown"
    }
  ]
}
```

`normalized_base_url` 要回显——用户填的可能被规范化了（补了 `/v1`），让他看到实际会用哪个地址。

失败返回 502 `provider_probe_failed`，`hint` 里给**具体原因**：

```jsonc
{
  "detail": {
    "code": "provider_probe_failed",
    "message": "无法获取模型列表",
    "hint": "端点返回 404。已尝试 https://api.example.com/v1/models 和 https://api.example.com/models。该服务可能不提供模型列表接口，可手动输入模型名。"
  }
}
```

各种失败情况对应的 hint 见 [../01-architecture/providers.md](../01-architecture/providers.md#探测失败的错误要具体)。

**探测失败仍允许手动添加模型**——有些中转站故意不开放 `/models`。

### GET /api/providers

不分页。

```jsonc
{
  "items": [{
    "id": "prv_8kL3mN9pQ2xR",
    "name": "DeepSeek",
    "base_url": "https://api.deepseek.com/v1",
    "key_hint": "a3f9",
    "enabled": true,
    "model_count": 2,
    "last_probe_at": 1785312000000,
    "created_at": 1785309000000
  }]
}
```

**永远只返回 `key_hint`，无明文。**

### POST /api/providers

创建供应商 + 一批模型（用户在 probe 结果里勾选的）。

```jsonc
{
  "name": "DeepSeek",
  "base_url": "https://api.deepseek.com",
  "api_key": "sk-xxxxxxxx",
  "models": [
    {"model_id": "deepseek-chat", "context_window": 65536},
    {"model_id": "deepseek-reasoner", "context_window": 65536}
  ]
}
```

一个事务里建 provider + 所有 model。返回 201 + provider 对象（含 models 数组）。

### PATCH /api/providers/{id}

可改 `name` / `base_url` / `api_key` / `enabled`。

**不传 `api_key` 字段则保持原值**；传了才重新加密写入。传 `null` 返回 400——不允许清空。

这个区分靠 Pydantic 的 `model_fields_set` 判断，见 [conventions.md](conventions.md#patch-的语义)。

### DELETE /api/providers/{id}

级联删除其下所有 model，进而级联删除相关 model_binding。

删除前检查：如果 chat 位的绑定会因此消失，返回 409 并提示"这是当前唯一的对话模型，请先绑定其它模型"。**不阻止用户删，但要让他知道后果**——直接删掉会导致所有对话失败且报错信息不明显。

### GET /api/models

不分页。可按 `provider_id` 过滤。

### POST /api/models

手动添加模型（probe 失败时的兜底路径）。

```jsonc
{
  "provider_id": "prv_xxx",
  "model_id": "some-custom-model",
  "context_window": 32768
}
```

`window_source` 自动设为 `manual`。

### PATCH /api/models/{id}

可改 `display_name` / `context_window` / `enabled`。

改 `context_window` 时 `window_source` 自动变 `manual`。

### POST /api/models/{id}/verify-vision

核验多模态能力。发一个带 1x1 像素图片的测试请求。

```jsonc
// 200
{ "supports_vision": "true", "checked_at": 1785312000000 }
```

按需触发，不在 probe 时对所有模型跑。见 [../01-architecture/providers.md](../01-architecture/providers.md#vision多模态)。

### GET /api/bindings

```jsonc
{
  "items": [{
    "id": "bnd_xxx",
    "agent_name": "",
    "purpose": "chat",
    "model_pk": "mdl_xxx",
    "model_id": "deepseek-chat",
    "provider_name": "DeepSeek"
  }]
}
```

返回时 join 出 `model_id` 和 `provider_name`——前端需要显示"DeepSeek / deepseek-chat"，单独再查一次是多余的往返。

### PUT /api/bindings

按 `(agent_name, purpose)` upsert。

```jsonc
{ "agent_name": "", "purpose": "compact", "model_pk": "mdl_xxx" }
```

用 PUT 而非 POST，因为语义是"设置这个位"而非"新建一条"。

### DELETE /api/bindings/{id}

解绑。之后该功能位会回落到 chat 位。

## 技能

### GET /api/skills

不分页。返回 L1 信息 + 文件清单。

```jsonc
{
  "items": [{
    "name": "pdf-report",
    "description": "当用户需要把数据整理成 PDF 报告时使用...",
    "version": "1.0",
    "keywords": ["pdf", "报告"],
    "files": ["references/layout-spec.md", "scripts/render.py"],
    "body_chars": 4820,
    "total_chars": 68300
  }],
  "l1_total_chars": 2156
}
```

`l1_total_chars` 是所有技能 L1 的合计字符数——**这是常驻上下文成本**，让用户知道装技能的代价。

技能用 `name` 作为标识（目录名），不用生成的 ID——它本质上是文件系统对象。

### GET /api/skills/{name}

含 `SKILL.md` 正文（L2）。

### GET /api/skills/{name}/files/{path:path}

读附属文件（L3）。

`path` 必须在该技能的 `files` 清单里精确命中，否则 404。**绝不拼路径直接 open**，见 [../01-architecture/skills.md](../01-architecture/skills.md#path-参数只用于查表)。

### POST /api/skills/upload

`multipart/form-data`，字段 `file`（zip）+ `overwrite`（bool，默认 false）。

```jsonc
// 201
{
  "name": "pdf-report",
  "file_count": 12,
  "total_chars": 68300,
  "skipped_files": [
    {"path": "assets/demo.mp4", "reason": "扩展名不在白名单"}
  ]
}
```

`skipped_files` 必须返回——静默跳过会让用户以为技能完整，实际缺文件。

校验失败返回 400 `skill_package_invalid`，`hint` 里说明具体问题（无 SKILL.md / 多个 SKILL.md / 超限额 / 路径穿越）。

同名已存在且 `overwrite=false` 返回 409 `skill_already_exists`。

### DELETE /api/skills/{name}

删除目录。

### POST /api/skills/reload

重扫 `skills/` 目录，刷新内存索引。上传/删除后自动调用，也可手动触发（用户直接在文件系统里放了技能目录时）。

```jsonc
{ "skill_count": 7, "l1_total_chars": 2156 }
```

## 宏

### GET /api/macros

```jsonc
{
  "items": [{
    "name": "daily-standup",
    "description": "整理当天工作内容成日报格式",
    "category": "工作流",
    "keywords": ["日报", "站会"]
  }]
}
```

前端的 `!` 提词器数据源。

### GET /api/macros/{name}

含正文。

### POST /api/macros/reload

## MCP

### GET /api/mcp/servers

```jsonc
{
  "items": [{
    "server_id": "filesystem",
    "enabled": true,
    "description": "本地文件系统扩展工具",
    "transport": "stdio",
    "status": "connected",
    "tool_count": 11,
    "estimated_tokens": 2340,
    "error_message": null
  }]
}
```

`estimated_tokens` 是该服务器所有工具定义的估算 token 数——**这是常驻上下文成本**，让用户知道开一堆 MCP 的代价。见 [../01-architecture/mcp.md](../01-architecture/mcp.md#与技能的区别)。

`status`：`connected` / `disconnected` / `error`

### POST /api/mcp/reload

重读 `config/mcp_servers.yaml`，断开重连所有服务器。

```jsonc
{
  "servers": [
    {"server_id": "filesystem", "status": "connected", "tool_count": 11},
    {"server_id": "my-remote", "status": "error", "error_message": "连接超时"}
  ]
}
```

**部分失败不算整体失败**，返回 200 并在结果里标注。

## 人设

### GET /api/personas/{kind}

`kind` ∈ `soul` / `user` / `agents`，对应 `SOUL.md` / `USER.md` / `AGENTS.md`。

```jsonc
{ "kind": "soul", "content": "你是...", "updated_at": 1785312000000 }
```

### PUT /api/personas/{kind}

```jsonc
{ "content": "..." }
```

写文件。下一轮对话立即生效（每次组装上下文时重读文件，不缓存）。

**不缓存**是刻意的：用户改完希望立即看到效果，加缓存就要处理失效逻辑，而文件读取成本可忽略。

## 设置

### GET /api/settings/whitelist

```jsonc
{
  "items": [{
    "id": "pth_xxx",
    "path": "D:/proj/jeeves/workspace",
    "can_write": true,
    "note": "默认工作区",
    "builtin": true
  }]
}
```

### POST /api/settings/whitelist

```jsonc
{ "path": "D:/mycode", "can_write": true, "note": "个人项目" }
```

插入前 `resolve()`。路径不存在返回 400。

### DELETE /api/settings/whitelist/{id}

`builtin=true` 的返回 409——删了 agent 就完全不能读写文件，且用户不容易想到是这个原因。

### POST /api/settings/blocker

在指定目录放拒止锚。

```jsonc
{ "path": "D:/重要文档" }
```

创建 `.jeeves_blocker` 文件。返回 201。

### DELETE /api/settings/blocker

```jsonc
{ "path": "D:/重要文档" }
```

删除该目录的锚文件。用 body 传 path 而非路径参数——Windows 路径含 `:` 和 `\`，放 URL 里要多层编码，容易出错。

### GET /api/settings/blockers

扫描白名单范围内所有拒止锚的位置。

```jsonc
{ "items": [{"dir": "D:/重要文档", "created_at": 1785312000000}] }
```

## 工作区

### GET /api/workspaces
### POST /api/workspaces

```jsonc
{ "name": "个人项目", "root_path": "D:/mycode" }
```

创建时**自动加进路径白名单**——否则建了工作区但 agent 读不了里面的文件，这个关联用户想不到。

### PATCH /api/workspaces/{id}
### DELETE /api/workspaces/{id}

默认工作区（`is_default=true`）不可删。有会话归属时返回 409。

## 元信息

### GET /api/meta

前端启动时拉一次，用于判断哪些功能可用。

```jsonc
{
  "version": "0.1.0",
  "sandbox_backend": "local",
  "sandbox_docker_available": false,
  "websearch_backend": "none",
  "has_chat_model": true,
  "host_is_localhost": true,
  "skill_count": 7,
  "macro_count": 3,
  "mcp_tool_count": 11
}
```

`has_chat_model=false` 时前端引导去设置页配置。`host_is_localhost=false` 时显示无鉴权警示条。

### GET /api/health

```jsonc
{ "status": "ok" }
```

存活探针，不查任何依赖。**不做 `/health/ready`**——没有 k8s 不需要外部依赖探测。
