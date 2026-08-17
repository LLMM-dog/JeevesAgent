# 配置类接口

通用约定（错误格式、分页、时间戳）见 [conventions.md](conventions.md)。对话与会话相关的接口见 [endpoints-chat.md](endpoints-chat.md)。

> 路径以代码里的路由注册为准。这份文档曾经写了六个不存在的端点（`GET /api/skills/{name}`、`DELETE /api/bindings/{id}`、`POST /api/attachments` 等），照着调只会得到 404。改动接口时请同步这里。

## 供应商与模型

### POST /api/endpoints/probe

填完 baseURL + Key 后拉模型列表，**不落库**。

```json
{ "base_url": "https://api.deepseek.com/v1", "api_key": "sk-..." }
```

响应里带 `normalized_base_url`（回显规范化后的地址）和 `suggested_name`（从地址推断的分组名，用于"添加模型"自动分组）。

### GET /api/endpoints

列出供应商（分组）。`api_key` 只回显尾 4 位，任何情况下不返回明文。

### POST /api/endpoints

新建供应商（分组），可同时带 `models` 数组一次性建好。`name` 可留空，由后端从 `base_url` 推断。同名或同地址同 Key 会并入已有分组而非报错。

### PATCH /api/endpoints/{endpoint_id}

改分组的名字 / 地址 / Key。`api_key` 传空串表示保持原 Key（前端拿不到明文，编辑时输入框留空）。

### DELETE /api/endpoints/{endpoint_id}

删供应商会级联删掉它的模型和绑定。

### GET /api/endpoints/{endpoint_id}/available-models

用已存的 Key 重新探测这个供应商，用于"供应商上了新模型"的场景。

### GET /api/models

`?endpoint_id=` 可选，`?enabled_only=true` 只返回启用的（对话页切换菜单用）。

### POST /api/models

手动加模型 —— 探测拿不到列表时（有些中转站不实现 `/models`）的兜底。

### PATCH /api/models/{model_pk}

改 `display_name`、`context_window`、`price_*`、`enabled`、`model_type`，以及 `endpoint_id`（拖动改分组）。

> 路径参数是 `model_pk` 而非 `id`：`model_id` 是供应商那边的模型名（如 `deepseek-chat`），会重复；`model_pk` 是本地主键。混用这两个名字会导致绑定指向错的行。

### DELETE /api/models/{model_pk}

### POST /api/models/{model_pk}/verify-vision

发一张 1x1 图片实测这个模型能不能读图，结果落库。

不能只信供应商文档：同一个模型名在不同中转站的多模态支持不一样。未核验的模型在前端不允许开视觉模式。

### GET /api/bindings

功能位绑定：`chat` / `vision` / `title` / `compact` / `embedding`。

### PUT /api/bindings

```json
{ "purpose": "chat", "model_pk": "mdl_..." }
```

用 PUT 而非 POST：一个功能位只能绑一个模型，语义是覆盖而非新增。

## 上下文

### GET /api/context-overhead

固定开销，`?session_id=` 可选。

```json
{
  "tools_tokens": 4298,
  "system_tokens": 1722,
  "mcp_tool_count": 8,
  "window_tokens": 131072,
  "is_estimate": true
}
```

**不需要有 run 在跑。** 新会话里没有任何 `context_usage` 事件，而固定开销此时已经确定 —— 不显示的话占用条是空的，用户以为"还没开始用 token"。

按技能开关过滤，所以关掉一个技能后这个数字会立刻变小。`is_estimate` 恒为 true：这是本地 tiktoken 数的，模型的分词器不一样。

见 [../architecture/context.md](../architecture/context.md#context_usage-事件)。

## 技能

### GET /api/skills

```json
{
  "items": [
    {
      "name": "commit-message",
      "description": "当用户要写 git 提交信息时使用……",
      "version": "1.0",
      "keywords": ["git", "提交信息"],
      "files": ["references/examples.md"],
      "enabled": true
    }
  ],
  "diagnostics": [
    { "level": "warning", "message": "缺 description，跳过", "path": "skills/x/SKILL.md" }
  ]
}
```

**诊断必须一并返回。** 用户需要知道"我上传的技能为什么没出现" —— 只写日志的话他在界面上看到的是技能凭空消失。

**这里不过滤被关掉的技能**，否则界面上看不到就没法再打开它。过滤只发生在进系统提示词那一步。

### PATCH /api/skills/{name}/enabled

```json
{ "enabled": false }
```

响应 `{"name": "...", "enabled": false}`。技能不存在返回 404 `skill_not_found`。

关掉的技能不进系统提示词，但用户明确点名时 `load_skill` 仍读得到 —— 开关控制的是常驻上下文成本，不是访问权限。见 [../architecture/skills.md](../architecture/skills.md#技能开关)。

### POST /api/skills/upload

`multipart/form-data`，字段 `file`（zip）+ `overwrite`（bool）。

响应 `{"name": ..., "files": 3, "skipped": ["a.exe"], "skill_count": 5}`。

错误码：`skill_not_zip` / `skill_empty` / `skill_invalid`（400）、`skill_exists`（409）。

### DELETE /api/skills/{name}

响应 `{"deleted": true, "skill_count": 4}`。删除前校验目标目录必须在 `skills/` 下。

### POST /api/skills/reload

重扫目录，响应 `{"count": 5, "names": [...]}`。

## 宏

### GET /api/macros

给前端提词器用，返回 `{"items": [{name, description, keywords}], "diagnostics": [...]}`。

### GET /api/macros/{name}

返回**渲染后**的正文（`${MACRO_DIR}` 已替换成真实路径），给模型用。

### GET /api/macros/{name}/source

返回**未渲染**的可编辑字段 `{name, description, body, keywords}`。

编辑界面必须用这个而不是上面那个：把替换后的绝对路径写回去，宏就跟当前机器绑死了，换台机器不能用。

### POST /api/macros

新建或更新，201。

```json
{ "name": "部署流程", "description": "当用户说发版时使用。", "body": "# ...", "keywords": [], "overwrite": false }
```

`description` 必填（`min_length=1`）—— 缺它的话加载器会**静默跳过**这个宏，只留一条 warning 诊断，而用户填完保存以为建好了。

撞名时返回 409 `already_exists`，不静默覆盖：模型起的名字撞车很常见，覆盖会悄悄冲掉用户手写的宏。要覆盖传 `overwrite: true`。

### DELETE /api/macros/{name}

响应 `{"ok": true}`。删整个目录（技能可能带附件，只删主文件会留下孤儿文件）。

### POST /api/macros/reload

响应 `{"count": 2, "names": [...]}`。

## MCP

### GET /api/mcp/servers

```json
{
  "items": [
    {
      "server_id": "filesystem",
      "transport": "stdio",
      "status": "ready",
      "error": "",
      "enabled": true,
      "tool_count": 12,
      "tools": [{ "name": "mcp__filesystem__read", "raw_name": "read", "description": "..." }],
      "estimated_tokens": 3140,
      "connected_at": 1730000000000
    }
  ],
  "config_errors": []
}
```

`enabled` 来自**配置文件**而非连接状态：关掉的服务器 manager 直接跳过，`status` 会是 `disconnected` —— 只看 status 的话"用户关掉的"和"连不上的"长得一样，而前者不该显示成错误。

`estimated_tokens` 是必须暴露的：MCP 工具定义是常驻上下文成本，每轮都重发。看不到这个数字的话用户会觉得"多开几个 MCP 没坏处"。

### PATCH /api/mcp/servers/{server_id}/enabled

```json
{ "enabled": false }
```

**会改写 `config/mcp_servers.yaml`**，然后断开重连所有服务器并重注册工具。响应 `{"server_id": ..., "enabled": false, "tools": 8}`。

配置里没有这个 id 返回 404 `server_not_found`。写 yaml 用逐行文本编辑以保留注释，见 [../architecture/mcp.md](../architecture/mcp.md#开关改-yaml-而不是存表)。

### GET /api/mcp/pending-approval

未确认启动命令的 stdio 服务器。`command` 字段**完整不截断**，`env` 只给键名不给值，另带危险模式扫描的 `warnings`。

stdio 服务器等同于任意代码执行，规范要求执行前让用户看到完整命令并确认。

### POST /api/mcp/reload

响应 `{"servers": 3, "ready": 2, "tools": 18, "config_errors": []}`。

先摘掉所有 `mcp__` 前缀的旧工具再注册新的 —— 不摘的话旧工具残留且指向已关闭的连接。

## 人设

### GET /api/personas

返回全部三份（`SOUL` / `USER` / `AGENTS`）的内容。

### PUT /api/personas/{key}

写回。`key` 是 `soul` / `user` / `agents`。

### POST /api/personas/{key}/reset

恢复成 `.example.md` 的内容。

## 路径白名单

### GET /api/whitelist

`?session_id=` 可选。返回会话级 + 全局条目。

### POST /api/whitelist

```json
{ "path": "D:/proj", "can_write": true, "note": "参考代码", "session_id": null }
```

`session_id` 为 null 表示全局。路径插入时就 `resolve()`。

### PATCH /api/whitelist/{item_id}

改 `can_write` 或 `note`。

### DELETE /api/whitelist/{item_id}

`builtin=1` 的四条内置项不允许删 —— 删了 agent 就不能读写文件了，而用户不容易想到是这个原因。

### GET /api/browse

`?path=` 目录浏览，给工作目录选择器用。返回 `entries` + 常用起点 `roots`（盘符 / 家目录 / 项目目录），让用户不用手打路径。

## 联网搜索

### GET /api/websearch

当前配置和可用的 provider。

### PUT /api/websearch

开关和 provider 选择。**默认关闭** —— 开了之后用户的搜索词会发给第三方，这个决定必须由他自己做。

改完立即生效，不需要重启。

## 定时任务

### GET /api/cron/tasks

### POST /api/cron/tasks

```json
{ "name": "每日总结", "prompt": "...", "cron": "0 9 * * *", "timezone": "Asia/Shanghai", "on_missed": "skip" }
```

### PATCH /api/cron/tasks/{task_id}

### DELETE /api/cron/tasks/{task_id}

### POST /api/cron/tasks/{task_id}/run

立即触发一次，用于验证 prompt 写得对不对，不用等到点。

### GET /api/cron/tasks/{task_id}/runs

执行历史。`scheduled_at` 与 `started_at` 的差值就是调度延迟。

### POST /api/cron/validate

校验 cron 表达式并返回接下来几次触发时间 —— 让用户在保存前就确认"这个表达式是我想的意思"。

## 追踪

### GET /api/traces

`?session_id=` / 分页。

### GET /api/traces-sessions

按会话聚合，追踪列表停在会话层而不是直接铺开所有 run。

### GET /api/traces/{run_id}

单个 run 的执行树，含 `span_totals`、`duration_ms`、`cost_usd`、`rollup_*`。

### GET /api/traces-stats

总量统计。

### POST /api/traces/cleanup

按 TTL 清理。

## 引用候选

### GET /api/ref-candidates

`?kind=file|dir|skill|tool|macro` + `?q=`。给前端 `@` / `#` / `!` 提词器用。

跳过 `node_modules` / `.venv`，按文件名优先于路径打分。

## 图片

### POST /api/images/upload

`multipart/form-data`。响应 `{data_url, mime, bytes, filename}`。

返回 data URL 而不是存储路径：图片以 base64 多模态注入，前端拿到就能直接回显缩略图。用 magic bytes 判类型，不信扩展名。

## 元信息

### GET /api/meta

```json
{
  "version": "0.1.0",
  "mcp_tool_count": 8,
  "tool_names": ["read_file", "..."],
  "skill_count": 1,
  "macro_count": 1,
  "sandbox_backend": "local",
  "sandbox_isolated": false,
  "sandbox_fallback_reason": "",
  "websearch_enabled": false
}
```

`sandbox_fallback_reason` 非空表示配了 Docker 但检测不到，已降级到本地执行。前端据此持续提示 —— 配了 docker 就是想要隔离，静默回落等于骗人。

### GET /api/health

存活探针。
