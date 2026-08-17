# 智能体系统

## 两种智能体，两种定义方式

| | AgentDefinition（用户定义） | AgentSpec（代码定义） |
| --- | --- | --- |
| 用途 | 用户在设置页创建、管理、选择 | 内置子智能体模板 |
| 存储 | `agent_defs` 表 | `specs.py` 硬编码 + `agents/*.md` 覆盖 |
| 包含 | 提示词、模型、权限、技能、MCP、额外 LLM 参数 | 提示词、工具白名单、max_turns |
| 数量 | 不限 | 2 个内置（researcher, reviewer） |

**AgentDefinition 用于用户在对话页选择的"主智能体"**，可以绑定模型、权限、技能、MCP、额外 LLM 参数。

**AgentSpec 用于 subagent 工具可以委派的目标**，是"工具型智能体"的模板。

## AgentSpec

一个子智能体 = 提示词 + 工具白名单 + 回合上限。用 frozen dataclass 声明：

```python
@dataclass(frozen=True)
class AgentSpec:
    name: str
    description: str              # 给主智能体看的"什么时候派这个子智能体"
    prompt: str                   # 子智能体的 system prompt
    tools: tuple[str, ...] = ("read_file", "list_dir", "glob", "grep")
    max_turns: int = 12
    source: str = "builtin"       # "builtin" 或 agents/*.md 文件路径
```

工具白名单默认只给只读工具（`read_file / list_dir / glob / grep`），**不给全集**。不声明 tools 时走安全默认。

### 加载方式

`specs.load_specs()` 合并两层：

1. 内置 spec（`specs.py` 的 `BUILTIN_SPECS`）
2. `agents/*.md` 文件（frontmatter + Markdown）

用户定义同名时**覆盖内置**。每份文件独立降级，一个坏定义不影响其它。

```markdown
---
name: researcher
description: 当需要读大量文件后给结论时派它
tools: read_file, glob, grep
max_turns: 20
---

（正文就是 system prompt）
```

`description` 是主模型选择子代理的唯一依据——写"什么时候用"而非"这是什么"。正文为空则跳过。

### AgentRegistry

```python
@dataclass
class AgentRegistry:
    specs: dict[str, AgentSpec]
    diagnostics: list[Diagnostic]  # 加载时的警告信息

    def get(name) -> AgentSpec | None: ...
    def names() -> list[str]: ...
    def catalog() -> list[tuple[str, str]]: ...  # 给 subagent 工具描述用的
```

- `get_registry()`：懒加载单例
- `reload()`：重新扫描 `agents/*.md`（用户改文件后即时生效）

## SubAgent 是一个工具

不做独立的编排层。`subagent` 就是一个普通工具：

```python
async def run(self, ctx, agent: str, task: str) -> ToolResult:
    """
    agent: 目标子智能体名
    task:  完整的任务描述（子智能体看不到父会话的历史，必须自包含）
    """
```

### task 必须自包含

子智能体**不继承父会话的消息历史**。它只看到 `task` 这一段文字。

这是刻意的：继承历史的话，子智能体的上下文和父会话一样大，"派子智能体来省上下文"的目的就落空了。

### 深度限制是双保险

**第一道**：`subagent` 不在任何子智能体的工具白名单里（`NEVER_FOR_SUBAGENT`）。模型看不到的工具不会去调。

**第二道**：ContextVar 深度计数，上限 2（`MAX_DEPTH`）。

为什么用 ContextVar 而非全局计数器：全局计数器会把 5 个并行子代理误判成深度 5。`finally` 里用 `reset(token)` 恢复而非 `-1` 递减——异常场景下递减会算错。

### 并发上限、超时、取消级联

```python
MAX_PARALLEL = 6        # 一次 tool_calls 里最多几个委派
MAX_CONCURRENCY = 3     # 实际同时跑几个
TIMEOUT_S = 600.0       # 单个子代理墙钟上限
```

超时转成**给模型的错误字符串**，不向上抛——父代理应该能决定"拆小重试"还是"自己做"。

取消级联靠 `await` 直连（子任务天然挂在父 Task 树上）。

### 子智能体执行

`subagent_runner.py` 的 `run_subagent()`：

1. 从 spec 构造子智能体的工具白名单：`spec.allowed_tools(可用工具)`
2. 模型独立决议：走 `resolve(agent_name=spec.name)`，子代理可以有自己绑定的便宜模型
3. **不继承父会话历史**：子代理从空白开始，只有一条 user 消息（task）
4. **记忆线隔离**：`agent_name=spec.name`，子代理消息不污染主线
5. 构建独立的 system prompt：环境部分共用，技能清单和工具描述按白名单裁剪

### 返回：必须截断，且分层可见

子智能体的**最终答复文本**（不含中间 tool_calls 过程）作为 `ToolResult.content` 返回。

**50KB 硬上限**，按 UTF-8 字节截断。

```
模型可见   截断后的结论 + "完整结果在工具详情里，不需要重新委派"
UI 可见    display.full_text 全量
```

委派存在的**唯一理由**是省上下文。结果原样回灌等于收益归零。

## 两个内置子智能体

只放两个。多了反而让主模型难选——委派本身是有成本的决策。

### researcher

调研型：读大量文件后给结论。工具集 `read_file / list_dir / glob / grep / load_skill*`，`max_turns=16`。

为什么单独抽出来：调研会产生大量中间内容，全塞进主会话会迅速撑爆。实测同一个"读 6 个文件提取结论"的任务，父上下文从 8399 降到 5489 token。

### reviewer

代码审查型：只报告具体问题（带 `文件:行号`），不改代码。工具集 `read_file / list_dir / glob / grep`，`max_turns=14`。

提示词里明确写了"**编造问题比漏掉问题更糟**"——它会让对方浪费时间去改不存在的缺陷。

### 用户可覆盖

`agents/*.md` 同名覆盖内置。内置只是默认值，想改 researcher 的提示词应该能直接改，不用换个名字。

## 每个智能体可绑不同模型

子智能体的模型不写在 spec 的 frontmatter 里，走 `model_binding` 表的 `agent_name` 字段（在设置页绑定）。

主智能体的模型走 AgentDefinition 的 `model_id` 字段（NULL=跟随全局绑定位）。

解析顺序：
```
1. (agent_name, purpose) 精确匹配
2. ("", purpose) 全局默认
3. ("", "chat") 兜底
4. 都没有 → ProviderError
```

## 主智能体的记忆线

`message.agent_name` 字段：
- 空串 `""` = 用户可见的主线
- 非空 = 该智能体的私有记忆线（子智能体用）

一条会话里并存多条线，互不污染。组装上下文时按 `(session_id, agent_name)` 过滤。前端默认只显示主线。

## AgentDefinition（用户定义的智能体）

用户在设置页创建和管理的智能体，是**第一公民**。主智能体实际上就是一个 AgentDefinition。

```python
class AgentDefinition(Base, TimestampMixin):
    id: str                      # "adf_7bK2mQ9xR4Lp"
    name: str                    # "代码审查员"
    description: str             # 一行描述
    avatar: str | None           # emoji
    system_prompt: str           # 自定义提示词
    model_id: str | None         # 绑定的模型，NULL=跟随会话
    skill_names: str             # JSON 数组
    mcp_servers: str             # JSON 数组
    permission_read: int         # 0/1
    permission_write: int
    permission_shell: int
    permission_network: int
    permission_subagent: int
    extra_llm_params: str       # 智能体级额外 LLM 参数（自由格式，解析后进请求 body）
    hidden: int                  # 是否在对话页选择器中隐藏
    max_turns: int | None        # NULL=使用全局默认
    deleted_at: int | None       # 软删除
```

Agent API（`/api/agents`）：CRUD + 默认智能体种子。权限过滤在 `chat_service.py` 的 `_filter_tools_by_permissions()` 中实现。详见 [multi-agent-roadmap.md](multi-agent-roadmap.md)。
