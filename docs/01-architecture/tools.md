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
    sandbox: SandboxPort
    pathguard: PathGuard
    skill_loader: SkillLoader
    approval_mode: str           # "manual" | "auto"
    registry: ToolRegistry       # subagent 工具要用它给子会话建工具集
    agent_name: str
    depth: int                   # 子智能体嵌套深度，防无限递归
```

新增工具需要新依赖时只改这一处。反面做法是给 `run()` 加参数——那要改所有工具签名。

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

## 内置工具清单

按模块归类。`审批` 列指 manual 模式下是否需人工确认。

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
| `memory_list` | 否 | 列长期记忆条目 |
| `memory_read` | 否 | 读某条 |
| `memory_write` | 否 | 新建/更新 |
| `memory_delete` | 否 | 删除 |

私密模式下 `memory_write` 直接返回"当前为私密模式，未写入"，不报错。

### 交互（`tools/interact.py`）

| 工具 | 审批 | 说明 |
| --- | --- | --- |
| `ask_user` | 否 | 自由问答，阻塞等用户输入 |
| `ask_choice` | 否 | 单选/多选，前端渲染选项按钮 |

实现方式与审批相同：发事件 → 阻塞等 → 前端回填。

### 子智能体（`tools/subagent.py`）

| 工具 | 审批 | 说明 |
| --- | --- | --- |
| `subagent` | 否 | 建独立子会话执行子任务，返回结论 |

`ctx.depth >= 3` 时该工具不注册，防无限递归。

### Web（`tools/web.py`）

| 工具 | 审批 | 说明 |
| --- | --- | --- |
| `web_search` | 否 | 走 `websearch` port |
| `web_fetch` | 否 | 抓 URL 转 Markdown |

### MCP（`tools/mcp_tool.py`）

动态注册，工具名 `mcp__<别名>__<工具名>`。见 [mcp.md](mcp.md)。

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
