# Jeeves 驾驭工程改进方案

> 基于对 Pi / OpenCode / Claude Code / Aider / Goose / Codex CLI / Hermes 共 7 个项目的 6 维度源码调研
> 2026-08-08

---

## 总评

Jeeves 在**机制层**做得很好——agent loop 设计、压缩三铁律、路径白名单、Protocol 工具系统、journal 分离——这些都是多数项目没有的。但在**策略层**缺口明显：缺少结构化流程约束、缺少验证闭环、缺少错误记忆、缺少分层规则体系。

以下是按投入产出比排序的改进方案。

---

## 第一优先：结构化流程 — 让 Agent "小步快跑"

### 问题

当前 Agent 拿到复杂任务时，会一次性尝试：规划 → 读文件 → 改代码 → 跑命令，全部塞在一个 turn 里。结果：要么超 token 截断，要么改错了但没验证就继续。

### 方案：Plan/Build 双阶段

借鉴 OpenCode 的 plan/build 模式，但不是两个独立 Agent——而是同一 Agent 在两种模式间切换：

```
Plan 阶段（只读）:
  └── 只能 read_file / grep / glob / list_dir
  └── 禁止 write_file / edit_file / run_shell
  └── 产出：一份 Markdown 计划（写入 artifact）

Build 阶段（执行）:
  └── 全工具访问
  └── 每完成一个步骤后自检：跑测试、看输出
  └── 遇到阻碍回退到 Plan 阶段
```

**实现方式**：不是新建 Agent，而是给 `ToolRegistry.execute()` 加一个 `mode` 过滤器。Plan 模式下硬拒绝写操作（利用已有的钩子系统！）：

```python
# 利用我们刚实现的钩子系统——不改 AgentLoop
def plan_mode_guard(ctx: BeforeToolContext) -> str | None:
    if ctx.extra.get("mode") == "plan" and ctx.tool_name in WRITE_TOOLS:
        return f"Plan 模式下禁止 {ctx.tool_name}。请先完成计划，切换到 Build 模式执行。"
    return None

registry.hooks.on(HookPoint.BEFORE_TOOL, plan_mode_guard)
```

**改动量**：~50 行（钩子 + UI 切换按钮）。零 AgentLoop 改动。

---

## 第二优先：验证闭环 — 让 Agent 自己检查自己

### 问题

Agent 写完代码就声称"完成了"，但实际上：测试没过、端口绑错了、依赖没装。它**不知道**自己错了，因为它从没跑过验证。

### 方案：每 turn 后自动跑验证命令

借鉴 Aider 的 `lint → test → feedback` 循环：

```python
# AgentLoop 在 _act 完成后，自动注入验证 turn
async def _verify_and_retry(self, ai_msg: Msg) -> bool:
    """如果本次修改了文件，自动跑测试。失败则注入错误信息让模型修。"""
    if not self._files_changed_this_turn(ai_msg):
        return False  # 没改文件，不需要验证

    # 跑用户配置的验证命令（从设置页配的）
    test_result = await self._run_verify_command()
    if test_result.exit_code == 0:
        return False  # 通过了，不用修

    # 注入错误信息，让模型自修
    self._append(Msg(role="user", content=f"""
[自动验证] 测试失败：

{test_result.output[:2000]}

请修复以上错误后重新验证。
"""))
    return True  # 继续循环，让模型修
```

参考实现：
- Aider `base_coder.py:1590-1624` — `lint_edited()` + `cmd_test()` 的反馈循环
- Claude Code PostToolUse hook — 外部脚本决定"继续还是停止"

**改动量**：~80 行（AgentLoop 加一个可配置的验证步骤）。设置页加一个"测试命令"输入框。

---

## 第三优先：结构化压缩 — 让压缩后的记忆更有用

### 问题

当前压缩摘要是自由文本，模型自己决定保留什么。结果是：关键错误信息被丢弃、进度描述过于模糊、下次 resume 时上下文断裂。

### 方案：固定模板压缩

借鉴 Pi 的 6-section 模板和 OpenCode 的 5-section 模板：

```
## 目标
[用户最初要做什么，一句话]

## 进度
[已完成的关键步骤，按时间顺序]
- 读了三份文件：xxx.py, yyy.py, zzz.md
- 修改了 xxx.py 的 handle_request 函数
- 跑了单元测试，3 个通过

## 关键决策
[做的选择及原因]
- 用 httpx 替代 requests：因为需要 async 支持
- 数据库表加了 index：查询慢的问题

## 阻碍
[遇到的错误和未解决的问题]
- 端口 9000 被占用，暂时用 9001

## 涉及文件
- xxx.py（已修改，测试通过）
- yyy.py（只读取）

## 下一步
[接下来该做什么，具体可操作]
1. 修改 zzz.md 里的 API 文档
2. 重新跑一次完整测试
3. 把端口改回 9000
```

实现是改压缩 prompt 模板——不改算法逻辑：

```python
COMPACT_SYSTEM_PROMPT = """你是对话压缩助手。将以下对话历史压缩为结构化摘要。

必须按以下格式输出：

## 目标
...

## 进度
...

## 关键决策
...

## 阻碍
...

## 涉及文件
...

## 下一步
...

规则：
- 目标必须保留用户原始意图
- 阻碍中必须包含具体错误信息（不要只说"遇到问题"）
- 涉及文件要区分"已修改"和"只读取"
- 下一步要具体可操作，不能是"继续完成任务"
"""
```

**改动量**：~30 行（只改压缩 prompt 模板）。

---

## 第四优先：工具返回优化 — 让工具结果和任务相关

### 问题

`read_file` 读 3000 行的文件，全部塞进 context。Agent 只关心其中 20 行——但被 2980 行噪音淹没。

### 方案：工具输出截断 + 文件卸载

借鉴 OpenCode 的输出截断策略和 Claude Code 的磁盘持久化：

```python
# 在 ToolResult 中加一个字段
@dataclass
class ToolResult:
    content: str           # 给模型的摘要（已截断）
    full_content_path: str | None = None  # 完整内容存文件，模型需要时可再读
    display: dict | None = None  # 给前端
```

具体规则：
- `read_file`：返回 ≤2000 行的 head+tail，中间省略部分标注行号范围。完整文件存 `data/tool_outputs/`，模型可以 `read_file(offset=...)` 分段读
- `grep`：返回 ≤50 条结果，标注总命中数。模型可以缩小范围重新查
- `run_shell`：返回 ≤2000 行的 head+tail，完整输出存文件

**改动量**：~60 行（ToolResult 加字段 + 各工具适配输出截断）。

---

## 第五优先：分层规则 — 让用户和项目有自己的"法"

### 问题

当前只有 `personas/AGENTS.md` 一套规则，混在一起。用户想"在这个项目里用 pytest 而不是 unittest"——只能写到全局规则里。

### 方案：规则分层

借鉴 Claude Code 的 CLAUDE.md 层级和 Pi 的 context files 发现链：

```
Jeeves 规则加载顺序（后面覆盖前面）:
1. 内置规则           （AGENTS.example.md — 只读，提交通用规范）
2. 用户全局规则        （~/.jeeves/rules.md — 跨项目偏好）
3. 项目规则            （<项目>/AGENTS.md — 项目级约束）
4. 会话临时规则        （/rules 命令设置 — 本次会话特有）
```

每层按 glob 模式匹配：
```yaml
# 项目规则示例（AGENTS.md）
rules:
  - pattern: "**/test_*.py"
    pytest: true
    coverage: 80
  - pattern: "backend/**"
    type_check: mypy
  - pattern: "frontend/**"
    lint: eslint
```

**改动量**：~100 行（规则加载器 + 注入到 system prompt 的逻辑）。

---

## 暂不做的方向

| 方向 | 原因 |
|------|------|
| MCP 延迟发现（Claude Code ToolSearch） | 当前 20 个工具的 context 开销可接受。等超过 30 个工具时再做 |
| 独立评估 Agent（Claude Code Auto-mode Classifier） | 成本太高（每次调额外 LLM），当前审批机制够用 |
| State Machine 编排（Goose StateMachine） | while 循环已验证有效，State Machine 增加调试成本 |
| RepoMap（Aider tree-sitter） | `search_files` + `grep` 对大仓库已够用。等实测发现 Agent 频繁"找不到文件"时再做 |
| Chrome DevTools 验证（Codex CLI） | Jeeves 是后端/全栈助手，不是前端专项工具 |

---

## 实施路线图

```
第一轮（本周）:
  ✅ 钩子系统
  ✅ 启动体验优化
  🔲 Plan/Build 双阶段模式（利用钩子）
  🔲 结构化压缩模板

第二轮（两周内）:
  🔲 自动验证闭环（lint + test + feedback）
  🔲 工具输出截断 + 文件卸载

第三轮（一个月内）:
  🔲 分层规则体系
  🔲 验证命令配置（设置页）
```

---

## 参考

所有调研文档位于 `docs/research/harness/`：

| 文件 | 维度 |
|------|------|
| `orchestration-engineering.md` | 执行编排：Plan/Build、步数限制、子智能体 |
| `evaluation-engineering.md` | 结果评估：验证闭环、自评失真、代码质量 |
| `context-engineering.md` | 上下文工程：记忆分层、压缩模板、增量更新 |
| `tool-engineering.md` | 工具系统：输出格式、渐进式披露、描述设计 |
| `state-management.md` | 状态管理：中间记忆、工作区认知、产物管理 |
| `constraint-engineering.md` | 约束与错误学习：硬护栏、错误记忆、规则分层 |
