# 工具系统

## Tool 是 Protocol，不是基类

```python
@runtime_checkable
class Tool(Protocol):
    name: str
    description: str
    requires_approval: bool          # manual 模式下是否需人工确认

    def parameters(self) -> dict: ...          # JSON Schema
    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult: ...
```

用 Protocol 而非 ABC，因为工具的来源不止一处：内置工具是自己写的类，MCP 工具是运行时动态构造的，技能里的脚本工具也是动态的。强制继承会让动态构造变别扭。

## ToolContext

所有请求级依赖装在一个 dataclass 里，不层层传参：

```python
@dataclass
class ToolContext:
    session_id: str
    run_id: str
    workspace: Path              # 当前工作区根目录
    db: AsyncSession
    llm: LLMPort
    agent_name: str = ""
    depth: int = 0               # 子智能体嵌套深度，防无限递归
    registry: ToolRegistry | None = None   # subagent 要用它给子会话建工具集
    current_call_id: str = ""    # 审批要靠它把前端的回复配对回来
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def approval_mode(self) -> ApprovalMode: ...   # 每次读都从 runtime_state 取
```

新增工具需要新依赖时只改这一处。反面做法是给 `run()` 加参数——那要改所有工具签名。

### 为什么 sandbox / pathguard 不在这里

它们通过模块级的取值函数拿（`get_guard()`、沙箱在 `exec.py` 内部构造），不当字段传。原因是它们是**会话级**的：白名单按会话查库、审批模式可能在流式进行中被用户切换。放进 dataclass 就是在 ctx 构造那一刻快照，之后的变更读不到。

`approval_mode` 是 property 而不是字段，同一个理由 —— 见 [agent-loop.md](agent-loop.md) 里关于 ContextVar 的说明。

### extra 是给单个工具的逃生口

`compact_context` 需要回调进 loop 请求压缩。给 ToolContext 加一个只有它用的字段，会让其它 19 个工具都带上这个无关依赖 —— 所以走 `extra`。

只在主 agent 注入（`depth == 0`）：子 agent 的上下文独立且短暂，压缩它等于白花一次 LLM 调用。

## ToolResult

```python
@dataclass
class ToolResult:
    content: str                      # 给模型看的文本
    display: dict | None = None       # 给前端渲染的结构化数据
    artifact_id: str | None = None    # 若产出了 artifact
    is_error: bool = False
```

**`content` 与 `display` 分离**是关键设计。模型需要的是紧凑文本（省 token），前端需要的是结构化数据（好渲染）。

例：`read_file` 给模型的 `content` 是带行号的文件内容，给前端的 `display` 是 `{"path": "...", "lines": 120, "language": "python"}` —— 前端只需显示一张"读取了 xxx.py（120 行）"的卡片，不需要把文件内容再渲染一遍。

例：`run_shell` 的 `content` 是截断后的输出，`display` 里带完整输出和退出码，前端可折叠展开。

## ToolRegistry

```python
class ToolRegistry:
    def register(self, tool: Tool) -> None: ...
    def get(self, name: str) -> Tool | None: ...
    def names(self) -> list[str]: ...
    def to_specs(self) -> list[dict]: ...        # 转成 LLM 的 tools 参数
    def forked(self) -> ToolRegistry: ...        # 浅拷贝
    async def execute(self, ctx, name, args) -> ToolResult: ...
```

### forked() 为什么必须存在

进程级的 `ToolRegistry` 单例被所有请求共享。往里加请求级工具（MCP 工具、某个会话专属工具）会污染全局——**一个会话配的 MCP 工具会出现在所有会话的工具列表里**。

所以：请求开始时 `registry.forked()` 拿到浅拷贝，请求级工具只往拷贝里加。

### 异常处理

```python
async def execute(self, ctx, name, args) -> ToolResult:
    tool = self.get(name)
    if tool is None:
        # 未知工具不抛异常。模型偶尔会幻觉出不存在的工具名，
        # 或者在 MCP 服务器掉线后继续调它的工具。
        # 返回错误文本让模型自我纠正，比让整轮对话崩掉好得多。
        return ToolResult(
            content=f"工具 {name} 不存在。可用工具：{', '.join(self.names())}",
            is_error=True,
        )
    try:
        return await tool.run(ctx, **args)
    except PathDeniedError as e:
        # 路径被拒也是给模型的信息，不是程序错误
        return ToolResult(content=f"路径访问被拒绝：{e}", is_error=True)
    except Exception as e:
        logger.exception("tool_failed", tool=name, args=args)
        return ToolResult(content=f"工具执行失败：{type(e).__name__}: {e}", is_error=True)
```

**一条铁律：`execute()` 永不向上抛异常。** 工具失败是 agent 的正常工作状态，不是系统故障。

唯一例外是 `asyncio.CancelledError` —— 它必须往上传，否则取消功能失效。

## 钩子系统

`ToolRegistry.hooks` 提供两个拦截点，在 `execute()` 内部触发：

| 钩子点 | 触发时机 | 能做什么 |
|--------|----------|----------|
| `BEFORE_TOOL` | 审批通过后，`tool.run()` 之前 | 返回 `str` → 阻止执行（作为错误文本返回给模型）；返回 `None` → 放行 |
| `AFTER_TOOL` | `tool.run()` 之后（无论成功/失败） | 观察结果、记录日志、收集指标 |

### 注册方式

```python
from app.modules.agent.hooks import HookPoint

# 安全审计：记录所有 shell 命令
def audit_shell(ctx: BeforeToolContext) -> str | None:
    if ctx.tool_name == "run_shell":
        log.info("shell_command", args=ctx.args, session=ctx.session_id)
    return None  # 放行

# 策略层阻止：特定会话不允许写文件
def block_writes(ctx: BeforeToolContext) -> str | None:
    if ctx.tool_name in ("write_file", "edit_file") and ctx.session_id in DENY_LIST:
        return "当前会话不允许修改文件"
    return None

registry.hooks.on(HookPoint.BEFORE_TOOL, audit_shell)
registry.hooks.on(HookPoint.BEFORE_TOOL, block_writes)

# 性能观测：记录所有工具耗时
def observe(ctx: AfterToolContext) -> None:
    metrics.record(ctx.tool_name, ctx.elapsed_ms, is_error=ctx.result.is_error)

registry.hooks.on(HookPoint.AFTER_TOOL, observe)
```

### 设计约束

- **钩子做策略，不做机制**：阻止"这类操作在当前会话不允许"可以，替代 `PathGuard` 不行
- **钩子异常不崩 loop**：单个钩子抛异常被捕获并记录，不影响其他钩子和工具执行
- **`forked()` 不共享钩子**：子 agent 拿到全新的空 HookRegistry
- **未知工具不触发钩子**：在 `get()` 阶段就被拦截了
- **`has_hooks` 快速路径**：无钩子时零开销跳过

详见 `backend/app/modules/agent/hooks.py` 和 `tests/test_hooks.py`。

## 内置工具清单

20 个（配了搜索后端是 21 个）。按模块归类，`审批` 列指 manual 模式下是否需人工确认。

### 文件（`tools/file.py`）

| 工具 | 审批 | 说明 |
| --- | --- | --- |
| `read_file` | 否 | 带行号返回。二进制文件拒绝，图片走视觉模式 |
| `write_file` | **是** | 覆盖写。新建文件也走这个 |
| `edit_file` | **是** | 精确字符串替换，`old_string` 必须唯一命中 |
| `list_dir` | 否 | 列目录，标注子目录 |
| `glob` | 否 | 按 pattern 找文件 |
| `grep` | 否 | 按正则搜内容，返回 `path:line` |

全部经 `PathGuard` 校验，见 [security.md](security.md)。

### 执行（`tools/exec.py`）

| 工具 | 审批 | 说明 |
| --- | --- | --- |
| `run_shell` | **是** | 经 sandbox 执行 shell 命令 |
| `run_python` | **是** | 经 sandbox 执行 Python 代码 |

### Todo（`tools/todo.py`）

| 工具 | 审批 | 说明 |
| --- | --- | --- |
| `todo_write` | 否 | **全量替换**当前会话的清单 |
| `todo_read` | 否 | 读当前清单 |

见 [todo.md](todo.md)。

### 技能（`tools/skill.py`）

| 工具 | 审批 | 说明 |
| --- | --- | --- |
| `load_skill` | 否 | 读 `SKILL.md` 正文（L2） |
| `load_skill_file` | 否 | 读技能内附属文件（L3） |

见 [skills.md](skills.md)。

### 记忆（`tools/memory.py`）

| 工具 | 审批 | 说明 |
| --- | --- | --- |
| `remember` | 否 | 写一条长期记忆，必须给 reason |
| `recall` | 否 | 按关键词召回，零 LLM 调用 |
| `update_memory` | 否 | 改已有条目，变更进 history |
| `forget_memory` | 否 | 归档而非真删 |

**私密模式在三个写工具里分别拦**（`remember` / `update_memory` / `forget_memory`），返回 `display={"skipped": True, "reason": "private_mode"}` 而不报错。

只在召回侧拦是不够的 —— 这是实测抓到的 bug：模型看不到会话开关，照样调 `remember`，而那时它真写进去了。查不到会话时**默认按禁止写处理**。

失忆模式拦在召回入口（`chat_service`），不在工具里 —— 那一层根本不会把记忆注进上下文。见 [memory 相关章节](context.md)。

### 上下文（`tools/context.py`）

| 工具 | 审批 | 说明 |
| --- | --- | --- |
| `compact_context` | 否 | 主动压缩上下文，返回省了多少 token |

原来只有被动压缩（涨到窗口 75% 才触发）。那个时机不由模型决定 —— 它只看总量，不知道"调研阶段已经结束、几十条工具输出已经没用了"。

不需要审批：不动文件、不执行命令，最坏结果是白花一次 LLM 调用。要审批的话每次弹窗，模型会因为怕打扰用户而不敢用。

"现在没什么可压的"返回 `is_error=False` —— 标成错误会让模型以为工具坏了、开始重试。

### 宏与技能管理（`tools/asset.py`）

| 工具 | 审批 | 说明 |
| --- | --- | --- |
| `manage_asset` | **是** | 宏/技能的增删改查 + reload |

一个工具带 `action`（list/read/create/update/delete/reload）和 `kind`（macro/skill），不是六个工具 —— 工具定义每轮都进上下文，拆开的话 schema 加起来 800+ token，而参数几乎完全一样。

需要审批：它写的是**会进后续所有对话上下文**的文件。一个错误的技能描述能影响模型之后的全部行为，比改一个源码文件影响面更大。

**技能是目录，不止一个文件。** 这个工具只负责 `SKILL.md`（要拼 frontmatter、要校验 description）；`references/`、脚本之类的附件用 `write_file` 直接写 —— `skills/` 和 `macros/` 都在可写白名单里。

加完附件**必须 reload**：索引是进程内单例，不重扫的话新文件不在 `meta.files` 白名单里，`load_skill_file` 会拒绝读它。而那个错误完全不指向"你需要 reload"。

`create` / `read` 返回**绝对路径**。这是实测踩的坑：模型用相对路径 `skills/xxx/references/detail.md` 写附件，而 `write_file` 的相对路径基准是**工作区** —— 文件落到 `workspace/skills/xxx/` 去了。那里本来就可写，所以**不报错**，模型回复"已完成"，而附件永远不会被索引。

### 子智能体（`tools/subagent.py`）

| 工具 | 审批 | 说明 |
| --- | --- | --- |
| `subagent` | 否 | 建独立子会话执行子任务，返回结论 |

深度超限时该工具不注册，防无限递归。见 [agents.md](agents.md)。

### Web（`modules/web/tools.py`）

| 工具 | 审批 | 说明 |
| --- | --- | --- |
| `web_search` | 否 | 走 websearch provider，**没配后端时不注册** |
| `web_fetch` | 否 | 抓 URL 转 Markdown，只依赖 httpx 所以恒定注册 |

`web_search` 没配后端就不注册：注册了用不了的工具会让模型反复调它，而工具定义每轮都在烧 token。

### MCP（`modules/mcp/tools.py`）

动态注册，工具名 `mcp__<server_id>__<工具名>`，整体截断到 64 字符（OpenAI 函数名规范）。

`requires_approval` **恒为真** —— 注解由服务器自述，据此跳过审批等于让被审查对象填审查结论。

每个服务器有 `enabled` 开关（`config/mcp_servers.yaml`），关掉的不连接、工具定义不占上下文。见 [mcp.md](mcp.md)。

## 工具描述怎么写

工具的 `description` 是模型唯一的使用说明，写不好模型就不用或用错。三条规则：

1. **写"什么时候用"，不只写"是什么"。** `read_file` 的描述里要说"修改文件前必须先读"。
2. **写清约束。** `edit_file` 要说明 `old_string` 必须唯一命中，否则会失败。
3. **参数描述里给例子。** `glob` 的 pattern 参数写 `如 "src/**/*.ts"`。

反例：`description="读取文件"`。模型不知道该不该先读再改，不知道能不能读二进制，不知道路径是相对还是绝对。

## 审批机制

manual 模式（默认）下，`requires_approval=True` 的工具执行前：

```
1. 发 approval_required 事件，带 tool_name / args / call_id
2. 在 asyncio.Event 上等待，超时 300s
3. 前端弹框，用户点允许/拒绝
4. POST /api/runs/{run_id}/approve 回填结果，set event
5. 允许 → 正常执行；拒绝 → 返回"用户拒绝执行"文本给模型
6. 超时 → 视为拒绝，返回"审批超时"
```

超时视为拒绝而非允许。用户离开电脑时不应该有命令自己执行。

auto 模式下跳过整个流程直接执行。这个开关是**会话级**的，不是全局的——不同会话可以有不同的信任级别。
