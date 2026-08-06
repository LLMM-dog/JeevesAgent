# MCP 接入

通过 Model Context Protocol 接外部工具，不改代码即可扩展能力。

## 分层

| 层 | 文件 | 职责 |
| --- | --- | --- |
| 协议层 | `modules/mcp/manager.py` | 与 MCP 服务器通信，`list_tools` / `call_tool` |
| 接线层 | `modules/mcp/tools.py` | 把 MCP 工具包装成本项目的 `Tool` |
| 配置层 | `config/mcp_servers.yaml` | 用户声明有哪些服务器 |

**分层的价值**：如果将来要改成"在 Web 设置页里加 MCP 服务器"（存表而非读文件），只需改配置层的来源，协议层和接线层不动。这样的演进路径是常见的。

## 配置文件

`config/mcp_servers.yaml`，加入 `.gitignore`（里面有 token）。

```yaml
- server_id: filesystem
  enabled: true
  description: 本地文件系统扩展工具
  transport: stdio
  command: npx
  args: ["-y", "@modelcontextprotocol/server-filesystem", "D:/data"]
  env:
    SOME_KEY: xxx
  cwd: D:/workdir

- server_id: my-remote
  enabled: true
  transport: streamable_http
  url: https://example.com/mcp
  headers:
    Authorization: Bearer xxx
  timeout: 30
```

支持四种 transport：`stdio` / `sse` / `streamable_http` / `websocket`。

`server_id` 必需且唯一，它会成为工具名前缀。

## 工具命名

```
mcp__<server_id>__<原始工具名>
```

**双下划线分隔**，不用单下划线 —— 单下划线在工具名里太常见（`read_file`、`web_search`），单下划线分隔的话无法从名字反解出 server_id。

前缀存在的原因：两个 MCP 服务器可能都提供叫 `search` 的工具，不加前缀就冲突。

## 远端工具名必须合规化

**这是最容易漏的一点。** 远端工具名不能假设符合 OpenAI 的函数名规范（`^[a-zA-Z0-9_-]{1,64}$`）。

实测能返回的名字：`do thing`（带空格）、`a.b`（带点）、中文名、超长名。

直接拼进 `tools` 参数会让**整个请求**被 API 拒掉，返回 400 且不指明是哪个工具坏了 —— 表现为"配了某个 MCP 之后所有对话都失败"，排查方向完全找不到。

```python
def sanitize_tool_name(raw: str) -> str:
    """
    非法字符替换为 _，超长截断，保留一份 原名→安全名 的映射，
    call_tool 时用原名调用。
    """
```

映射表在包装器里持有。**不要试图从安全名反推原名**（不可逆）。

## 失败降级

MCP 是外部依赖，随时可能挂。三处都不能让对话失败：

```python
# 1. list_tools 失败 → 返回空列表
#    该服务器的工具本轮不可用，其余工具正常。
#    日志记 warning，发 mcp_unavailable 事件让前端提示一次。

# 2. call_tool 失败 → 返回错误文本给模型
#    和普通工具执行失败一样处理，模型会自己换个方式。

# 3. 服务器启动失败（stdio 的 command 不存在）→ 跳过该服务器
#    启动时记录，不阻止服务启动。
```

**原则：外部依赖挂掉不该让整轮对话失败。** 一个配错的 MCP 服务器不应该让整个 agent 不可用。

## 加载时机

```
启动时     读 yaml → 逐个连接 → list_tools → 注册到进程级 registry
热加载     POST /api/mcp/reload → 断开全部 → 重新连接 → 重新注册
```

热加载后不需要重启服务，也不影响正在进行的对话（它们用的是 `forked()` 的副本）。

### stdio 服务器的进程管理

stdio transport 会启动子进程，需要管生命周期：

```
服务启动    → 启动子进程，保持运行
热加载      → 先 terminate 旧进程再启新的
服务关闭    → lifespan shutdown 里 terminate 全部
子进程崩溃  → 标记该服务器不可用，不自动重启
```

**不自动重启**。反复崩溃的服务器会造成无限重启循环，日志被刷满。手动 reload 更可控。

## 前端呈现

MCP 工具在前端**统一用通用的 `ToolCallCard` 展示**，不为每个 MCP 工具写专属组件。

理由：MCP 工具是运行时动态的，写不出对应的组件。通用卡片显示工具名 + 参数 JSON + 结果，够用。

内置工具才有专属气泡（`read_file` 显示文件路径和行数，`run_shell` 显示命令和输出，`todo_write` 显示看板）。

## 与技能的区别

容易混淆：

| | MCP | 技能 |
| --- | --- | --- |
| 本质 | 可执行的工具 | 文本知识/流程 |
| 模型怎么用 | 直接调用 | 先 `load_skill` 读，再自己动手 |
| 扩展方式 | 配 yaml，接外部服务 | 放 markdown 文件 |
| 需要外部进程 | 通常是 | 否 |
| 占用常驻上下文 | 是（工具定义常驻） | 只占 L1（name + description） |

**MCP 工具定义是常驻上下文的**，这是它的隐性成本。一个 MCP 服务器提供 20 个工具，每个工具的 JSON Schema 约 100~300 token，合计几千 token 常驻。

所以：不要一次性开一堆 MCP 服务器。`enabled: false` 关掉不用的（也可以在设置页点开关，见下）。

设置页显示每个服务器的"工具数 + 估算 token 占用"，让用户知道代价。

## 开关：改 yaml 而不是存表

`PATCH /api/mcp/servers/{server_id}/enabled` 直接改 `config/mcp_servers.yaml` 的 `enabled` 字段，然后断开重连所有服务器并重注册工具。

### 为什么继续以 yaml 为唯一真源

manager 本来就在读 `cfg.enabled`。把开关状态存到数据库的话就有**两个真源** —— 用户手工编辑 yaml 和在界面点开关会互相打脸（改了文件界面不动，点了开关文件没变）。

而 MCP 配置本身就是用户的文件，改它没有"污染第三方内容"的问题 —— 这和技能不一样，技能是 zip 装进来的，所以技能开关走表（见 [skills.md](skills.md#技能开关)）。

### 写 yaml 用逐行文本编辑

不用 `yaml.safe_load` + `dump`：那会丢掉全部注释、重排键顺序、把中文转成 `\uXXXX`。`mcp_servers.yaml` 是用户手写并且要继续手写的文件，点一次开关就把他的注释全删了是不能接受的。

逐行编辑只碰 `enabled` 那一行，其余字节原样不动。比任何 round-trip 库都保守，而且不需要新增依赖（项目只有 pyyaml，它没有保留注释的能力）。

看不懂的格式返回 `False`（表现为 404「配置里没有这个服务器」）而不是硬写 —— 那个文件里可能有 token。

### 状态里的 enabled 来自配置，不是连接状态

`GET /api/mcp/servers` 每项的 `enabled` 从 `load_configs()` 取，不从 manager 的连接状态推。

关掉的服务器 manager 直接跳过，`states()` 里那一项的 `status` 是 `disconnected` —— 只看 status 的话"用户关掉的"和"连不上的"长得一样，而前者不该显示成错误。

### 工具重注册是共享的

单个开关和整体 reload 都走 `_reregister_mcp_tools()`：先摘掉所有 `mcp__` 前缀的旧工具再注册新的。

不摘的话旧工具会残留，而它们指向已关闭的连接 —— 模型调用时才会发现连接没了，那个报错完全不指向"这个服务器已经被关掉了"。

抽成函数是因为两处要做同样的事。复制一遍的话症状是"用开关关掉的服务器工具还在，用 reload 关掉的就没了"。

## 启动命令确认

`GET /api/mcp/pending-approval` 列出未确认启动命令的 stdio 服务器。

stdio 服务器以与本应用相同的权限执行命令 —— 等同于任意代码执行。MCP 规范要求客户端在执行前必须让用户看到**完整命令**并确认，所以：

- `command_approved` 必须显式设为 `true` 才会连接
- 返回的 `command` 字段**完整不截断**（截断会让用户看不到真正危险的那一段）
- 只返回 env 的**键名**不返回值（值里常有 token）
- 扫描危险模式（`curl | sh` 之类）并附 `warnings`
