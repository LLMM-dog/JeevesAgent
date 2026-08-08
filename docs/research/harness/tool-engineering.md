# 工具系统工程设计深度研究

> **研究日期**: 2026-08-08
> **覆盖项目**: OpenCode、Claude Code、Aider、Codex CLI
> **研究方向**: 工具数量策略、选择机制、返回格式、描述设计

---

## 目录

1. [OpenCode — Zod Schema 驱动的类型安全工具框架](#1-opencode--zod-schema-驱动的类型安全工具框架)
2. [Claude Code — 渐进式披露与 54 个工具的治理](#2-claude-code--渐进式披露与-54-个工具的治理)
3. [Aider — 模型-工具匹配配置与格式硬约束](#3-aider--模型-工具匹配配置与格式硬约束)
4. [Codex CLI — V4A Patch 格式的单次多文件操作](#4-codex-cli--v4a-patch-格式的单次多文件操作)
5. [对比总结与设计启示](#5-对比总结与设计启示)

---

## 1. OpenCode — Zod Schema 驱动的类型安全工具框架

### 1.1 工具数量：14 个内置工具 + MCP + 插件

OpenCode 的内置工具列表（`builtins.ts` 第 31-47 行）：

```typescript
// packages/core/src/tool/builtins.ts:31-47
export const node = makeLocationNode({
  name: "built-in-tools",
  layer: Layer.empty,
  deps: [
    ApplyPatchTool.node,   // apply_patch — V4A Patch 格式多文件操作
    BashTool.node,         // bash — Shell 命令执行
    EditTool.node,         // edit — 精确文本替换（Search/Replace）
    GlobTool.node,         // glob — 文件发现
    GrepTool.node,         // grep — 正则内容搜索
    QuestionTool.node,     // question — 向用户提问
    ReadTool.node,         // read — 读文件/目录/图片
    SkillTool.node,        // skill — 加载技能文件
    TodoWriteTool.node,    // todowrite — 任务列表管理
    WebFetchTool.node,     // webfetch — HTTP 请求
    WebSearchTool.node,    // websearch — 网络搜索
    WriteTool.node,        // write — 写入文件
  ],
})
```

**工具数量策略**：

- **出厂内置 12 个精简工具**，覆盖文件操作、Shell 执行、Web 访问、用户交互四大类别
- 通过 **MCP + Plugin 机制**动态扩展工具池，每个 Location（工作区）独立注册
- TODO 注释中明确还有待移植的工具（第 26-29 行）：edit fuzzy parity、task、LSP、repo_clone、repo_overview、plan_exit、Rune/code mode

### 1.2 工具定义格式：`Tool.make()` + Zod Schema

每个工具定义包含 4 个核心部分，以 bash 工具为例（`bash.ts` 第 108-197 行）：

```typescript
// packages/core/src/tool/bash.ts:108-197
Tool.make({
  // 1. 描述：一段完整自然语言段落
  description: `Execute one shell command string with the host user's 
    filesystem, process, and network authority. The active Location is 
    the default working directory. Relative workdir values resolve from 
    that Location. ...`,
  
  // 2. 输入 Schema（Zod）
  input: Input,  // Schema.Struct({ command, workdir?, timeout? })
  
  // 3. 输出 Schema（Zod）
  output: Output,  // Schema.Struct({ exit, output, truncated, timeout?, warnings? })
  
  // 4. 执行函数
  execute: (input, context) => Effect.gen(function* () { ... }),
  
  // 5. 结构化输出（仅关键字段给模型看）
  structured: StructuredOutput,  // { exit?, truncated, timeout? }
  toStructuredOutput: ({ output }) => ({ truncated, ... }),

  // 6. 模型可读输出（更友好的文本）
  toModelOutput: ({ output }) => [
    { type: "text", text: output.output },
    { type: "text", text: modelOutput(output) },
  ],
})
```

**核心设计模式**：`Tool.make()` 返回一个 **不透明的类型标记** (`Definition<Input, Structured>`)，不是对象——这防止直接访问内部实现。所有交互通过 `permission(name, tool)`、`definition(name, tool)`、`settle(tool, call, context)` 三个导出函数进行（`tool.ts` 第 148-156 行）。

**描述设计**（以该任务关注的 4 个工具为例）：

| 工具 | 描述长度 | 描述策略 |
|------|---------|---------|
| bash | ~150 词 | 功能说明 + 默认值 + 参数约束 + 平台差异 + 权限说明 |
| edit | ~45 词 | 精确操作说明 + 路径解析规则 + 外部目录权限 |
| read | ~45 词 | 多模式说明（文件/图片/目录）+ 路径解析 |
| write | ~45 词 | 功能 + 路径解析 + 权限 |
| question | ~75 词 | 使用场景列举 + 使用提示 |
| grep | ~40 词 | 约束 + 参数用途 + 输出格式 |
| glob | ~30 词 | 最简洁的描述 |
| todowrite | ~25 词 | 职责单一 |
| apply_patch | ~60 词 | 原子性说明 + 操作顺序 + 限制说明 |
| websearch | ~80 词 | Provider 说明 + 参数控制 + 当前年份注入 |

**描述设计原则**：
1. **完整段落，不用列表** — 因为 Anthropic/OpenAI 模型对自然段落的理解优于结构化列表
2. **包含当前年份** — WebSearch 描述中内联 `new Date().getFullYear()` 动态注入
3. **明确路径解析规则** — 每个文件工具都描述相对/绝对路径 + 外部目录权限
4. **参数默认值内联** — `Timeout values are milliseconds (default: 120000; maximum: 600000)`

### 1.3 工具选择机制：两级注册 + 权限过滤

```typescript
// 注册有两个层级：

// 层级 1: ApplicationTools（全局、跨 Location 共享）
// packages/core/src/tool/application-tools.ts:22-26
export interface Interface {
  readonly register: (tools) => Effect.Effect<void>
  readonly entries: () => ReadonlyMap<string, Entry>
}

// 层级 2: ToolRegistry（Location 级别，每个工作区独立）
// packages/core/src/tool/registry.ts:106-122
materialize: (permissions) => {
  // 1. 收集全局 tools
  const registrations = new Map(applications.entries())
  
  // 2. 覆盖本地注册（同名替换）
  for (const [name, entries] of local) {
    const registration = entries.at(-1)?.registration
    if (registration) registrations.set(name, registration)
  }
  
  // 3. 权限过滤
  for (const [name, registration] of registrations)
    if (whollyDisabled(permission(registration.tool, name), permissions))
      registrations.delete(name)
  
  // 4. 生成 definitions（给模型的 JSON Schema 列表）
  return {
    definitions: Array.from(registrations, ([name, reg]) => 
      definition(name, reg.tool)),
    settle: (input) => { /* 执行时再匹配 */ }
  }
}
```

**权限过滤**（`registry.ts` 第 132-135 行）：

```typescript
function whollyDisabled(action: string, rules: PermissionV2.Ruleset) {
  // 找到最后一条匹配该 action 的规则
  const rule = rules.findLast((rule) => Wildcard.match(action, rule.action))
  // 如果该规则是 "deny *"（全部拒绝），工具不可见
  return rule?.resource === "*" && rule.effect === "deny"
}
```

**关键设计**：
- `findLast()` — 规则的**最后一条生效**（后覆盖前），与前端的规则配置直觉一致
- `resource === "*" && effect === "deny"` — 只有明确的 "全部拒绝" 才移除工具；部分拒绝保留工具但执行时被权限系统拦截
- 去重逻辑：本地注册覆盖同名全局注册

### 1.4 返回结果格式：三层输出结构

每个工具返回三层输出（`tool.ts` 第 91-126 行）：

```typescript
// 三层输出结构
{
  // 第 1 层: structured — 给程序消费的结构化数据
  structured: {
    truncated: true,     // 或 { exit: 0, truncated: false, timeout: false }
  },
  
  // 第 2 层: content — 给模型看的可读文本
  content: [
    { type: "text", text: "<命令的实际输出>" },
    { type: "text", text: "Command exited with code 0." },
    // 或 { type: "file", data: "base64...", mime: "image/png", name: "screenshot.png" }
  ],
}
```

**输出截断与持久化**（`tool-output-store.ts` 第 138-174 行）：

```typescript
// 硬约束：MAX_LINES = 2000, MAX_BYTES = 50KB
const bound = (input) => {
  // 1. 提取 text content
  const contextual = text.map(item => item.text).join("")
  
  // 2. 如果行数/字节数在限制内 → 直接返回
  if (lineCount(contextual) <= maxLines && 
      byteLength(contextual) <= maxBytes)
    return { output: input.output, outputPaths: [] }
  
  // 3. 超限 → 写磁盘 + 保留首尾预览
  const outputPath = write(contextual)  // → tool-output/tool_<id>
  const marker = `... output truncated; full content saved to ${outputPath} ...`
  
  // 4. head-tail 截断：前一半行/字节 + 后一半行/字节
  return {
    output: {
      structured: input.output.structured,
      content: [{
        type: "text",
        text: boundedPreview(contextual, marker, maxLines, maxBytes)
      }, ...media],
    },
    outputPaths: [outputPath]
  }
}
```

**截断策略细节**（`tool-output-store.ts` 第 74-104 行）：
- 行数内：取前 `ceil(maxLines/2)` 行 + 后 `floor(maxLines/2)` 行
- 字节超限：按 UTF-8 边界精确截断（不破坏多字节字符）
- 存储位置：`$HERMES_HOME/tool-output/tool_<ascending_id>`
- 保留时效：7 天自动清理
- 模型可通过后续 `read` 工具读取完整输出文件

**模型可读输出（toModelOutput）示例**：

```typescript
// bash 工具: 
// "ls -la\n...\nCommand exited with code 0."

// edit 工具:
// "Edited file successfully: src/app.ts\nReplacements: 1\n\`\`\`diff\n- old\n+ new\n\`\`\`"

// grep 工具:
// "Found 3 matches\nsrc/app.ts:\n  Line 42: const foo = ..."

// question 工具:
// Q: "What approach?" → "User has answered: "What approach?"="Functional style". Continue."
```

### 1.5 避免"选错工具"的设计

**1. 精确语义命名**：
- `read` vs `grep` vs `glob` — 三者操作层面明确不同：读文件内容 vs 搜内容 vs 找文件
- `edit` vs `write` vs `apply_patch` — 精确替换 vs 完整覆写 vs 多文件 Patch
- `bash` 的 `workdir` 参数 + `timeout` 参数让 shell 工具仍然精确可控

**2. 错误反馈引导下一轮**：
```typescript
// edit 工具在找不到匹配时返回精确错误
"Could not find oldString in the file. It must match exactly, 
 including whitespace and indentation."

// 多个匹配时
"Found multiple exact matches for oldString. Provide more 
 surrounding context or set replaceAll to true."
```

**3. 参数级别的 Schema 验证**：输入参数通过 `Schema.decodeUnknownEffect` 严格校验，格式错误在调用时立即失败并反馈——而不是执行后才发现。

---

## 2. Claude Code — 渐进式披露与 54 个工具的治理

> **数据来源**：npm source map 泄漏事件公开的 TypeScript 源码、社区逆向分析、Anthropic 官方文档

### 2.1 工具数量：最多 54 个 + 分层治理

Claude Code 的工具池采用 **5 步组装 Pipeline**：

```
Base enumeration (up to 54 tools)
    → Mode filtering (按权限模式过滤)
    → Deny rule pre-filtering (移除被 deny 的)
    → MCP integration (添加 MCP 工具)
    → Deduplication (去重)
```

**工具分类**（~19 个内置类型 + MCP 动态工具）：

| 类别 | 工具 | 数量 |
|------|------|------|
| 核心文件操作 | Read, Write, Edit, Glob, Grep, LSP | 6 |
| Shell 执行 | Bash, PowerShell | 2 |
| 版本控制 | Git | 1 |
| Web | WebFetch, WebSearch | 2 |
| Notebook | NotebookEdit | 1 |
| Agent 协调 | Agent, SkillTool, TaskCreate/List/Get/Update, SendMessage, TeamCreate/Delete | 8+ |
| MCP 动态 | `mcp__*` | 可变 |

### 2.2 核心创新：ToolSearch 渐进式披露

**问题**：MCP 工具多时，所有工具 schema 一次性加载到 context 可能消耗 67K+ tokens。

**解决方案**（`claude-code.md` 第 459-473 行）：

```
正常模式（ToolSearch 关闭）：
  启动时 → 所有 MCP 工具 schema 加载到 context → 可能消耗 67K+ tokens

ToolSearch 模式（当 MCP 描述超过 context 的 10% 时自动启用）：
  启动时 → 只加载工具名称 + searchHint → ~10K tokens
  Model 需要时 → 关键词搜索 → 按需加载完整 schema
```

**实测效果**：节省 **85% context token**。

**实现细节**：

```typescript
// 工具接口中的关键字段
interface Tool {
    name: string                  // 主标识符
    searchHint?: string           // 3-10 词能力描述，ToolSearch 使用
    aliases?: string[]            // 向后兼容名
    description(input, options): Promise<string>  // 完整 schema，按需调用
}

// ToolSearch 匹配逻辑
function toolSearch(query: string, toolPool: Tool[]): Tool[] {
    return toolPool.filter(t => 
        keywordMatch(t.name, t.searchHint, query)
    ).map(t => loadFullSchema(t))
}
```

**`searchHint` 设计原则**：
- 3-10 个词
- 描述工具能力的简短短语
- 不包含句号
- 示例：`"Search code across repository"`, `"Query PostgreSQL database"`

### 2.3 工具安全标记：isConcurrencySafe / isReadOnly / isDestructive

Claude Code 在每个工具接口上声明 3 个行为属性：

```typescript
// 完整工具接口（从社区逆向分析重建）
type Tool<Input, Output> = {
    readonly name: string
    searchHint?: string
    
    // ⭐ 三大安全标记
    isConcurrencySafe(input): boolean   // 能否与其他工具并发执行？
    isReadOnly(input): boolean          // 是否只读？（default 模式自动通过）
    isDestructive?(input): boolean      // 是否破坏性？（auto 模式分类器重点审查）
    
    call(args, context, canUseTool, parentMessage, onProgress?): Promise<ToolResult>
    description(input, options): Promise<string>
    inputSchema: ZodType
    
    checkPermissions(input, context): Promise<PermissionResult>
    userFacingName(input): string
    maxResultSizeChars: number          // 每个工具可自定义输出截断阈值
}
```

这三个标记驱动了智能调度：

1. **`isConcurrencySafe`** — harness 可安全地并行执行多个标记为 safe 的工具调用，提高吞吐量
2. **`isReadOnly`** — `default` 模式下只读工具自动通过（不弹窗），减少审批疲劳
3. **`isDestructive`** — `auto` 模式下的 ML 分类器重点审查破坏性操作

### 2.4 输出截断：更激进的策略

| 参数 | 默认值 | 说明 |
|------|--------|------|
| Tool result cap | ~50K chars | 硬编码，不可用户配置 |
| Preview | ~2KB | context 中保留的预览量 |
| 磁盘持久化 | 超过阈值即触发 | 完整结果写入 `/tmp/claude-{id}.txt` |
| MCP `maxResultSizeChars` | 可到 500K chars | MCP server 可通过 `_meta` 自定义 |
| 消息级聚合 budget | 200K chars | N 个并行工具结果总和不能超过此值 |

**关键设计决策**（`claude-code.md` 第 578-588 行）：

```python
def maybe_persist_large_result(tool_result, threshold=50000):
    if len(tool_result.content) <= threshold:
        return tool_result  # 直接保留
    
    filepath = write_to_disk(tool_result.content, tool_result.id)
    preview = tool_result.content[:2000]
    return ToolResult(
        content=f"Output too large ({size}). Saved to: {filepath}\n"
                f"Preview:\n{preview}\n...\n"
    )
```

- Read 工具 `threshold = Infinity`，豁免持久化（避免 Read → 写文件 → 再 Read 循环）
- 空结果替换为 `(toolName completed with no output)`——防止模型将空结果误认为对话边界

---

## 3. Aider — 模型-工具匹配配置与格式硬约束

### 3.1 工具数量策略：极少的内置工具

Aider 的核心设计哲学是**不把"工具选择"作为问题**。它用**编辑格式（Edit Format）**替代传统的多工具模式：

| 编辑格式 | 对应 Coder 类 | 工具模型 |
|----------|-------------|---------|
| `diff` | EditBlockCoder | SEARCH/REPLACE 块（默认） |
| `whole` | WholeFileCoder | 输出完整文件内容 |
| `udiff` | UnifiedDiffCoder | unified diff 格式 |
| `architect` | ArchitectCoder | 自然语言指令 + Editor 执行 |
| `editor-diff` | EditorEditBlockCoder | 接收指令 + 生成 SEARCH/REPLACE |

**没有"Grep/Glob/Read"工具**——Aider 通过 `/add` 命令让用户显式加载文件到 chat，模型只需输出编辑结果。

### 3.2 模型-工具匹配：3128 行的 model-settings.yml

Aider 的核心创新之一是 **中央化的模型-格式匹配配置**（`aider/resources/model-settings.yml`，3128 行，覆盖几乎所有主流模型）：

```yaml
# 不同模型的最佳编辑格式不同：
- name: gpt-4o
  edit_format: diff           # SEARCH/REPLACE
  use_repo_map: true
  lazy: true
  reminder: sys
  examples_as_sys_msg: true
  editor_edit_format: editor-diff

- name: gpt-4-turbo
  edit_format: udiff          # 旧模型用 udiff

- name: o1-preview
  edit_format: architect      # o1 用双模型模式
  editor_model_name: gpt-4o
  editor_edit_format: editor-diff

- name: claude-3.5-sonnet
  edit_format: diff
  use_repo_map: true
  examples_as_sys_msg: true
```

**运行时匹配**（`models.py`）：还支持基于名称模式的模糊匹配：
- `deepseek.*v3` → `diff`
- `deepseek.*r1` → `diff` + `reasoning_tag = "think"`
- `gpt-4.*` → `diff`

### 3.3 SEARCH/REPLACE 格式的硬约束设计

Aider 的 `system_reminder` 是**每轮注入**的格式规则（`aider/coders/editblock_prompts.py` 第 66-89 行）：

```python
system_reminder = """# *SEARCH/REPLACE block* Rules:

Every *SEARCH/REPLACE block* must use this format:
1. The *FULL* file path alone on a line, verbatim. No bold, no quotes...
2. The opening fence and code language: ```python
3. The start of search block: <<<<<<< SEARCH
4. A contiguous chunk of lines to search for in the existing source code
5. The dividing line: =======
6. The lines to replace into the source code
7. The end of the replace block: >>>>>>> REPLACE
8. The closing fence: ```

Every *SEARCH* section must *EXACTLY MATCH* the existing file content,
character for character, including all comments, docstrings, etc.

*SEARCH/REPLACE* blocks will *only* replace the first match occurrence.
Keep *SEARCH/REPLACE* blocks concise.
Break large blocks into a series of smaller blocks.

ONLY EVER RETURN CODE IN A *SEARCH/REPLACE BLOCK*!
"""
```

**三个关键设计决策**：

1. **精确匹配原则** — SEARCH 段必须逐字符相同。Fuzzy matching 代码存在但已禁用（`editblock_coder.py` 中有 `return` 提前退出模糊匹配路径）。设计哲学是：宁可让模型修正也不默默猜错。

2. **省略号容错**（`try_dotdotdots`）— 当模型用 `...` 表示"此处省略 N 行"时，Aider 能按 `...` 分割 SEARCH，分段独立精确匹配：

```python
def try_dotdotdots(whole, part, replace):
    dots_re = re.compile(r"(^\s*\.\.\.\n)", re.MULTILINE | re.DOTALL)
    part_pieces = re.split(dots_re, part)
    replace_pieces = re.split(dots_re, replace)
    # 逐段精确匹配和替换
```

3. **"Did you mean" 错误反馈** — SEARCH 失败时不仅报错，还返回相似行提示让模型修正：

```python
res = f"# {len(failed)} SEARCH/REPLACE blocks failed to match!\n"
for edit in failed:
    # 提供 "Did you mean..." 建议
    did_you_mean = find_similar_lines(original, content)
    if did_you_mean:
        res += f"Did you mean to match some of these actual lines from {path}?\n..."
```

### 3.4 文件管理策略：用户显式 `/add`

Aider 不使用文件发现工具（如 Glob/Grep），而是要求用户显式通过 `/add` 命令加载文件：

```python
# 模型不能自主决定读哪个文件——必须请求用户添加
main_system = """...
1. Decide if you need to propose *SEARCH/REPLACE* edits to any files 
   that haven't been added to the chat...
"""
```

**设计理由**：
- 用户完全控制模型能看到哪些文件（安全性 + 隐私性）
- 不需要复杂的安全检查（模型看不到就不会泄漏）
- 减少模型的决策空间（不需要"我应该读哪个文件"的 meta-level 决策）
- 避免上下文浪费（不会被无关文件占据 token）

---

## 4. Codex CLI — V4A Patch 格式的单次多文件操作

### 4.1 工具数量策略：极简 3 工具核心 + 插件扩展

Codex CLI 的核心工具集极其精简：

```python
# prompt.md 中定义的三个核心工具
1. update_plan   # 计划管理（PEV 架构核心）
2. shell         # Shell 命令执行（PTY 模拟）
3. apply_patch   # 文件编辑（V4A Patch 格式）
```

**设计哲学**：**"Give the agent bash and let it build the tools it needs on the fly."**

### 4.2 V4A Patch 格式语法

这是 Codex 编辑系统最独特的设计——一种精简的、面向文件的 Diff 格式。

```
*** Begin Patch
*** Add File: path/to/new_file.py
+新文件内容行1
+新文件内容行2
*** Update File: path/to/existing.py
*** Move to: path/to/renamed.py
@@ class MyClass
 保留的上下文行
-删除的行
+新增的行
 保留的上下文行
*** Delete File: path/to/obsolete.txt
*** End Patch
```

**语法定义**（BNF 风格）：

```
Patch       := Begin { FileOp } End
Begin       := "*** Begin Patch" NEWLINE
End         := "*** End Patch" NEWLINE
FileOp      := AddFile | DeleteFile | UpdateFile
AddFile     := "*** Add File: " path NEWLINE { "+" line NEWLINE }
DeleteFile  := "*** Delete File: " path NEWLINE
UpdateFile  := "*** Update File: " path NEWLINE [ MoveTo ] { Hunk }
MoveTo      := "*** Move to: " newPath NEWLINE
Hunk        := "@@" [ header ] NEWLINE { HunkLine }
HunkLine    := (" " | "-" | "+") text NEWLINE
```

**关键设计约束**：

1. **文件引用必须用相对路径** — 禁用绝对路径
2. **默认 3 行上下文** — 每个修改前后各显示 3 行作为定位上下文
3. **`@@` 定位符** — 当 3 行上下文不够唯一时，用 `@@ class ClassName` 定位
4. **多级 `@@` 嵌套** — 代码块重复过多时，多个 `@@` 跳转到正确上下文
5. **单次调用多文件操作** — Add + Update + Delete 都在一个 patch 中

### 4.3 为什么选择 Unified Diff 风格

从 OpenAI 的设计推断：

1. **模型专项训练** — GPT-5.2/5.3/5.5 系列模型专门为 `apply_patch` 格式微调
2. **结构化解析** — 格式有明确的 BNF 语法，安全解析
3. **原子性操作** — 整个 patch 作为单一 shell 命令执行，全部成功或全部失败
4. **可审查性** — Patch 格式天然可读，适合 diff review
5. **减少往返次数** — 一个 patch 可以跨多个文件，减少 API 调用

### 4.4 工具选择机制：PEV 架构的隐式引导

Codex 不依赖模型"选工具"——**架构本身引导模型在正确时机调用正确工具**：

```
Plan 阶段   → update_plan（总是先规划）
Execute 阶段 → shell / apply_patch（执行计划中的步骤）
Verify 阶段  → shell（运行测试/构建验证）
```

每一步完成后用 `update_plan` 标记进度，模型有清晰的"下一步该干什么"的信号。

### 4.5 工具返回格式

- `shell`：返回 `stdout + stderr` 合并输出 + 退出码
- `apply_patch`：返回已应用的操作列表（`A/M/D`）+ 文件状态
- `update_plan`：返回更新后的 todo 列表

---

## 5. 对比总结与设计启示

### 5.1 工具数量策略对比

| 维度 | OpenCode | Claude Code | Aider | Codex CLI |
|------|----------|-------------|-------|-----------|
| **内置工具数** | 12 | 19-54 | 0 (编辑格式) | 3 |
| **扩展机制** | MCP + Plugin | MCP + Plugin/SDK | model-settings.yml | Plugin |
| **工具注册层级** | 全局 + Location | 5 步 Pool 组装 | 用户 /add 文件 | 配置 + 插件 |
| **多 vs 少的哲学** | 精确专用 | 分批加载 | 格式替代工具 | 通用 + 组合 |
| **token 效率** | 全量注入 context | ToolSearch 节省 85% | 无工具描述开销 | 极低开销 |

### 5.2 工具选择机制对比

| 维度 | OpenCode | Claude Code | Aider | Codex CLI |
|------|----------|-------------|-------|-----------|
| **选择方式** | 模型自主选择 | ToolSearch 渐进式 | 格式硬约束 | PEV 架构引导 |
| **渐进式披露** | ❌ 全量加载 | ✅ 按需搜索 | N/A | N/A |
| **权限过滤** | Wildcard + deny 前置 | 7 层纵深防御 | 用户 /add 文件 | 沙箱模式 |
| **防止误调用** | Schema 验证 + 错误反馈 | isReadOnly/isDestructive | 精确匹配 + "Did you mean" | 架构阶段约束 |
| **并发安全性** | implicit | isConcurrencySafe 标记 | N/A | N/A |

### 5.3 返回格式对比

| 维度 | OpenCode | Claude Code | Aider | Codex CLI |
|------|----------|-------------|-------|-----------|
| **截断策略** | 2000 行 / 50KB + head-tail | 50K chars + 2KB preview | N/A (编辑块) | 命令输出 |
| **持久化路径** | `tool-output/tool_<id>` | `/tmp/claude-<id>.txt` | Git commit | N/A |
| **结构化输出** | ✅ structured + content 双层 | content only | diff 格式 | 操作列表 |
| **空结果处理** | "(no output)" | "(toolName completed with no output)" | Diff 显示 | 退出码 |
| **媒体支持** | ✅ 图片 base64 内联 | 图片替换为 [image] | ❌ | ❌ |

### 5.4 描述设计对比

| 维度 | OpenCode | Claude Code | Aider | Codex CLI |
|------|----------|-------------|-------|-----------|
| **描述风格** | 完整自然段落 | searchHint 3-10 词 | 格式规则 + 示例 | BNF 语法 |
| **描述长度** | 25-150 词 | 精简 | 结构化指令 | 语法定义 |
| **动态内容** | ✅ 年份动态注入 | ❌ | ✅ fence 自适应 | ❌ |
| **包含默认值** | ✅ 内联在描述中 | ❌ | 在格式规则中 | ❌ |
| **包含约束** | ✅ 路径/权限说明 | ❌（走权限系统） | ✅ 硬约束规则 | ✅ 语法约束 |
| **包含示例** | ❌ | ❌ | ✅ few-shot examples | ✅ 格式示例 |

### 5.5 关键设计启示

#### 启示 1：渐进式披露是管理大量工具的必备模式

Claude Code 的 ToolSearch 实测节省 85% context token。当 MCP 工具超过 ~15 个时，全量加载所有 tool schema 会显著挤占模型的推理空间。**建议 Jeeves 在工具超过 12 个时自动启用类似机制**：

```python
# 伪代码
if total_tool_schema_tokens > context_window * 0.10:
    # 启用延迟加载
    for tool in tools:
        context.append({"name": tool.name, "search_hint": tool.search_hint})
else:
    # 全量加载
    for tool in tools:
        context.append(tool.full_schema())
```

#### 启示 2：三层输出结构是"模型友好"的最佳实践

OpenCode 的 `structured + content` 双层输出 + `toModelOutput` 格式化是当前最成熟的方案：
- `structured` 给程序消费（后续工具链、审批逻辑）
- `toModelOutput` 给模型消费（自然语言、格式化 diff）
- 截断时 `head + tail` 而非纯 head（保留尾部上下文）

#### 启示 3：硬约束 + 精确错误反馈 > 模糊匹配

Aider 的经验表明：
- **精确匹配 + "Did you mean" 错误提示**在第二轮能显著提高成功率
- **禁用模糊匹配**避免了"默默猜错"的隐蔽 bug
- **每轮注入 system_reminder** 是保证格式遵守的最低成本方案

#### 启示 4：工具安全标记驱动智能调度

Claude Code 的 `isConcurrencySafe` / `isReadOnly` / `isDestructive` 三元组让 harness 能做智能决策，而不是依赖 model 的"自觉"。Jeeves 应该为每个工具声明这些属性。

#### 启示 5：工具描述的设计原则

从四个项目的实践中提取的通用原则：

1. **完整的自然段落**优于结构化列表（OpenCode 验证）
2. **searchHint 作为快速索引**优于让模型通读全部描述（Claude Code 验证）
3. **包含默认值和参数约束**在描述中（OpenCode 验证）
4. **明确写清楚"什么时候用"**（question 工具的 "Use this tool when..." 模式）
5. **动态注入当前年份等信息**（OpenCode websearch 验证）
6. **格式规则 + few-shot examples 优于纯文本描述**（Aider 验证）
7. **BNF 语法定义约束**（Codex 验证）

---

## 附录：关键源码文件索引

### OpenCode
| 文件 | 功能 | 行数 |
|------|------|------|
| `packages/core/src/tool/tool.ts` | 工具定义框架 (Tool.make, Definition) | 162 |
| `packages/core/src/tool/registry.ts` | 工具注册、权限过滤、Materialization | 147 |
| `packages/core/src/tool/tools.ts` | 工具注册接口 (Tools.Service) | 13 |
| `packages/core/src/tool/builtins.ts` | 12 个内置工具列表 | 48 |
| `packages/core/src/tool/application-tools.ts` | 全局工具注册 | 57 |
| `packages/core/src/tool/bash.ts` | Bash 工具实现 | 207 |
| `packages/core/src/tool/edit.ts` | Edit 工具实现 | 223 |
| `packages/core/src/tool/read.ts` | Read 工具实现 | 117 |
| `packages/core/src/tool/read-filesystem.ts` | 分页读取 + 图片处理 | 366 |
| `packages/core/src/tool/write.ts` | Write 工具实现 | 101 |
| `packages/core/src/tool/grep.ts` | Grep 工具实现 | 137 |
| `packages/core/src/tool/glob.ts` | Glob 工具实现 | 105 |
| `packages/core/src/tool/apply-patch.ts` | V4A Patch 工具实现 | 219 |
| `packages/core/src/tool/question.ts` | Question 工具实现 | 94 |
| `packages/core/src/tool/todowrite.ts` | TodoWrite 工具实现 | 62 |
| `packages/core/src/tool/websearch.ts` | WebSearch 工具实现 | 260 |
| `packages/core/src/tool/webfetch.ts` | WebFetch 工具实现 | 218 |
| `packages/core/src/tool-output-store.ts` | 输出截断 + 持久化存储 | 211 |

### Aider
| 文件 | 功能 | 行数 |
|------|------|------|
| `aider/coders/editblock_prompts.py` | SEARCH/REPLACE 格式 prompt | 172 |
| `aider/coders/editblock_coder.py` | SEARCH/REPLACE 解析和应用 | 657 |
| `aider/coders/base_coder.py` | Coder 基类，agent loop 核心 | 2485 |
| `aider/coders/wholefile_prompts.py` | WholeFile 格式 prompt | 64 |
| `aider/coders/architect_coder.py` | Architect+Editor 双模型 | 48 |
| `aider/resources/model-settings.yml` | 模型-工具匹配配置 | 3128 |
| `aider/models.py` | 模型路由和配置 | 1338 |

### Claude Code（社区逆向 + 文档推断）
| 概念 | 关键文件（推断） |
|------|----------------|
| ToolSearch | `Tool.ts` (searchHint, toolMatchesName) |
| 工具池组装 | `assembleToolPool` |
| 安全标记 | `isConcurrencySafe`, `isReadOnly`, `isDestructive` |
| 输出截断 | `maybe_persist_large_result` |
| 权限分类器 | `yoloClassifier.ts` |

### Codex CLI
| 概念 | 关键文件 |
|------|---------|
| V4A Patch | `prompt_with_apply_patch_instructions.md` |
| PEV 架构 | `core/prompt.md` |
| 工具定义 | `tools/apply_patch/`, `tools/shell/`, `tools/update_plan/` |
