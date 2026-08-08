# Claude Code 驾驭工程深度研究

> **研究日期**：2026-08-08
> **数据来源**：Anthropic 官方文档、社区逆向分析（2026-03 npm source map 泄漏事件公开的 500K+ 行 TypeScript）、VILA-Lab 架构论文、WaveSpeed 架构拆解、社区深度博客
> **覆盖版本**：v2.1.88 ~ v2.1.200（2026 年 Q1-Q2）

---

## 目录

1. [架构总览](#1-架构总览)
2. [7 层权限体系](#2-7-层权限体系)
3. [Context 管理 5 层策略](#3-context-管理-5-层策略)
4. [MCP 集成](#4-mcp-集成)
5. [Hooks 系统](#5-hooks-系统)
6. [子 Agent 编排](#6-子-agent-编排)
7. [Claude Agent SDK](#7-claude-agent-sdk)
8. [工具系统](#8-工具系统)
9. [对 Jeeves 的启示](#9-对-jeeves-的启示)

---

## 1. 架构总览

### 1.1 核心哲学

> "The model is necessary but not sufficient. Claude Code proves that even the most capable frontier model needs 200,000 lines of infrastructure to be production-useful."

Claude Code 的代码库中，**仅 ~1.6% 是 AI 决策逻辑，其余 98.4% 是运营基础设施**——权限执行、上下文管理、安全层、可扩展性、会话持久化。核心 agent loop 其实极其简单，是一个 while 循环：

```
while (not done):
    response = model.call(context)
    for tool_call in response.tool_uses:
        result = execute(tool_call, after_permission_check)
        context.append(result)
```

真正复杂的系统全部包裹在这个循环外面。

### 1.2 7 组件架构

```
User → Interfaces → Agent Loop → Permission System → Tools → State & Persistence → Execution Environment
```

| 组件 | 职责 | 关键文件 |
|------|------|----------|
| Interfaces | CLI、headless (`-p`)、SDK、IDE/Desktop/Browser | React + Ink 终端 UI |
| Agent Loop | `queryLoop` async generator | `query.ts` |
| Permission | 7 层安全检查、6 种模式、ML 分类器 | `permissions.ts`, `yoloClassifier.ts` |
| Tools | 最多 54 个内置工具 + MCP 工具 | `assembleToolPool` |
| State | JSONL 会话转录、prompt history、subagent sidechain | `transcript.ts` |
| Execution | Shell（含沙箱）、文件系统、Web、MCP 连接 | 42 个工具子目录 |

### 1.3 5 层子系统分解

```
Surface 层  → CLI, headless, SDK, IDE
Core 层     → queryLoop, 5-stage compaction, subagent spawning
Safety 层   → 7 权限模式, auto-mode classifier, 27 hook events, tool pool
State 层    → JSONL transcripts, CLAUDE.md hierarchy, auto-memory
Backend 层  → Shell execution, MCP connections (7 transport types)
```

### 1.4 9 步 Turn 执行 Pipeline

每个 turn（用户发言到 agent 回复）经历严格的 9 步 pipeline：

1. Settings 解析
2. State 初始化
3. Context 组装（9 个来源，见 §3.3）
4. **5 个 pre-model context shaper**（最便宜的先执行）
5. Model 调用（API）
6. Tool 分发
7. 权限门控（7 层，见 §2）
8. Tool 执行
9. 停止条件检查

---

## 2. 7 层权限体系

> 这是 Claude Code 最突出的驾驭工程创新。任何请求都必须通过**所有**适用层——任一层都能阻断请求。

### 2.1 Layer 1: Tool Pre-filtering（工具预过滤）

在 model 看到工具列表之前，先做**粗粒度过滤**：被拒绝的工具完全从 model 的视野中移除。这是最外层的安全网。

```typescript
// 伪代码：assembleToolPool 中的第一步
function assembleToolPool(allTools, permissionConfig):
    pool = allTools
    // Step 1: 移除被 blanket-deny 的工具
    pool = pool.filter(t => !permissionConfig.deny.includes(t.name))
    // Step 2: 按权限模式过滤
    pool = applyModeFilter(pool, permissionConfig.mode)
    // Step 3: 按 deny rules 预过滤
    pool = applyDenyRules(pool, permissionConfig.rules)
    return pool
```

**设计优点**：不让 model 知道被禁用的工具存在，避免 "jailbreak via social engineering"——model 无法请求它不知道的工具。

### 2.2 Layer 2: Deny-first Rules（拒绝优先规则）

**deny 永远优先于 allow**。即使 allow 规则更具体，deny 也会生效。规则评估顺序：

```
1. deny  rules → 匹配即阻断
2. ask   rules → 匹配即弹窗确认
3. allow rules → 匹配即静默通过
4. 无匹配     → 弹窗确认（默认行为）
```

规则格式：`Tool` 或 `Tool(specifier)`，例如：

```json
{
  "permissions": {
    "deny": ["Bash(rm *)", "Bash(git push --force *)", "Edit(.env)"],
    "ask": ["Bash(curl *)", "WebFetch(*)"],
    "allow": ["Read", "Grep", "Glob", "Bash(git diff *)", "Bash(git log *)"]
  }
}
```

**关键实现细节**：
- `Bash(rm *)` 匹配任意 `rm` 命令（包括 `rm -rf`），不论参数
- 即使有 `allow: ["Bash(*)"]`，`deny: ["Bash(rm *)"]` 仍会阻断 `rm`
- "Strictest rule wins" 原则：deny > ask > allow

### 2.3 Layer 3: Permission Modes（权限模式）

**6 种模式**（外加内部 `bubble` 模式用于子 agent 向上级升级）：

| 模式 | 行为 | 信任级别 | 适用场景 |
|------|------|----------|----------|
| `plan` | 用户批准所有计划才能执行 | 最低 | 代码探索、敏感项目 |
| `default` | 标准交互式批准（读操作自动通过） | 低 | 新手、敏感工作 |
| `acceptEdits` | 文件编辑 + 文件系统命令自动批准 | 中 | 在审视中迭代代码 |
| `auto` | ML 分类器评估工具安全性 | 高 | 长时间任务、减少提示疲劳 |
| `dontAsk` | 不弹窗，仅允许 pre-approved 工具 | 更高 | CI/CD、锁定脚本 |
| `bypassPermissions` | 跳过几乎所有检查（关键安全保留） | 最高 | 隔离容器/VM only |

**Shift+Tab** 在 CLI 中循环切换 `default → acceptEdits → plan`。`auto` 和 `bypassPermissions` 需显式启用。

#### auto 模式的特殊行为

- `rm -rf /`、`rm -rf ~` 即使在 `bypassPermissions` 下也会被阻断
- 自动阻止：`curl | bash`、`git push --force`、`terraform destroy`、`.env` 文件读取
- **Protected paths**：不同模式对 `.git`、`.env`、`.bashrc` 等系统文件的写操作有不同处理

### 2.4 Layer 4: Auto-mode Classifier（自动模式分类器）

**这是 Claude Code 最具创新的安全设计**：用一个**独立的 Sonnet 4.6 实例**评估工具调用的安全性，而不是让主 model 自评。

```typescript
// 核心文件：yoloClassifier.ts
// 伪代码架构
class YoloClassifier {
    async evaluate(toolCall, context):
        // 1. 加载 classifier 的专用 system prompt
        //    （独立的 prompt，只有安全评估逻辑，不含 agent prose）
        prompt = loadClassifierPrompt()
        
        // 2. 两阶段评估：
        //    Phase 1: Fast-filter（模式匹配 + 启发式规则）
        if fastFilterPasses(toolCall):
            return "allow"
        
        //    Phase 2: Chain-of-thought（完整 LLM 推理）
        //    与预计算分类结果 race
        result = race(
            classifierModel.evaluate(toolCall, context),
            precomputedTimeout,
        )
        
        return result // "allow" | "block" | "escalate"
}
```

**关键设计决策**：
- Classifier **看不到 agent 的 prose/思考过程**——只看到工具调用参数
- 两阶段策略：快速过滤先跑（零成本），LLM 评估作为回退
- 与超时 race，确保不阻塞 agent loop
- 默认阻止：`git reset --hard`、`terraform destroy`、`pulumi destroy`、任何含 `--dangerously-skip-permissions` 的命令
- 对 `Bash(*)` 和 `PowerShell(*)` 的敏感子命令有硬编码阻止列表

**ArXiv 研究数据**（Sonnet 4.6，2026-04）：
- 在 restart-services 任务上提升 43.8% → 59.4% 安全率
- Classifier 更擅长检测宽泛的不安全操作（如批量 git 操作）而非精确的定向操作
- 偶尔会对合法操作过度阻止（artifact cleanup 下降 21.9% → 18.8%）

**这是 Claude Code 独有的创新** ⭐——据我所知没有其他 agent harness 有独立的 ML safety classifier。

### 2.5 Layer 5: Shell Sandboxing（Shell 沙盒）

- 文件系统隔离 + 网络隔离用于 shell 命令
- 通过 Git worktree 实现工作区隔离
- `acceptEdits` 模式支持 `additionalDirectories` 配置允许写入特定目录
- `bypassPermissions` 模式拒绝以 root/sudo 运行

### 2.6 Layer 6: Non-restoration on Resume（恢复时不自动恢复权限）

> "Trust is always re-established in the current session."

这是接受**用户摩擦**作为维护安全不变量的代价的设计选择：

- 会话恢复时，权限模式重置为 `default`（或配置的 `defaultMode`）
- 之前已批准的权限不会自动迁移
- 用户必须重新建立信任边界
- 防止 "权限漂移"：上个会话批准的某次 `curl` 不会在下个会话静默执行

**设计优点**：简单、可审计、无状态。代价是用户体验有些摩擦。

### 2.7 Layer 7: Hook Interception（Hook 拦截）

`PreToolUse` hooks 可以在权限系统之外**额外**修改或阻断工具调用。这是用户自定义的安全层，可以：

- 返回 `permissionDecision: "deny"` 阻断操作
- 返回 `updatedInput` 修改工具参数
- 注入 `additionalContext` 提供额外安全上下文

详见 [§5 Hooks 系统](#5-hooks-系统)。

### 2.8 权限体系总结

```
                    请求到达
                       │
        ┌──────────────┼──────────────┐
        │ Tool Pre-filtering          │ ← 移除 model 不可见的工具
        │ (甚至看不到被禁用的工具)        │
        └──────────────┼──────────────┘
                       │
        ┌──────────────┼──────────────┐
        │ Deny-first Rules            │ ← deny > ask > allow
        │ (严格优先规则)                │
        └──────────────┼──────────────┘
                       │
        ┌──────────────┼──────────────┐
        │ Permission Mode             │ ← 6 种模式决定基线行为
        │ (default/acceptEdits/auto等) │
        └──────────────┼──────────────┘
                       │
        ┌──────────────┼──────────────┐
        │ Auto-mode Classifier        │ ← 独立 Sonnet 实例
        │ (ML 安全评估，Auto 模式)      │
        └──────────────┼──────────────┘
                       │
        ┌──────────────┼──────────────┐
        │ Shell Sandboxing            │ ← 文件系统 + 网络隔离
        │ (命令级沙盒)                  │
        └──────────────┼──────────────┘
                       │
        ┌──────────────┼──────────────┐
        │ Non-restoration on Resume   │ ← 恢复时重置权限
        │ (会话边界安全)                │
        └──────────────┼──────────────┘
                       │
        ┌──────────────┼──────────────┐
        │ Hook Interception           │ ← 用户自定义 hook 拦截
        │ (PreToolUse 返回 deny)       │
        └──────────────┼──────────────┘
                       │
                    执行/拒绝
```

### 2.9 对 Jeeves 的启示

| Claude Code 设计 | Jeeves 可借鉴 | 优先级 |
|------------------|---------------|--------|
| Deny-first 原则 | 无论 allow 多具体，deny 总是赢——这是唯一不会出错的安全策略 | 🔴 高 |
| 独立 ML 分类器 | 用独立模型评估安全性而非让主模型自评——当前最创新的安全设计 | 🟡 中（成本高） |
| 恢复时不恢复权限 | 简单的无状态设计，安全可审计 | 🔴 高 |
| 7 层纵深防御 | 每层独立、任一层可阻断——纵深防御 > 单点防御 | 🔴 高 |
| Hook 拦截 | 在权限系统外提供用户自定义的安全拦截点 | 🟢 低 |

---

## 3. Context 管理 5 层策略

> Context window（200K/1M token）是 Claude Code 的**约束性资源**。5 种不同的 context-reduction 策略在**每次 model 调用前**顺序执行。

### 3.1 5 层 Compaction Pipeline

| 层 | 策略 | 触发条件 | 成本 | 实现文件 |
|---|------|----------|------|----------|
| **1** | Budget Reduction | 每条消息的 size cap，始终活跃 | ~$0 | query.ts |
| **2** | Snip | 裁剪旧历史（feature-gated） | ~$0 | `HISTORY_SNIP` 开关 |
| **3** | Microcompact | 缓存感知细粒度压缩 + 时间触发 | ~$0 | `microcompact.ts` |
| **4** | Context Collapse | 只读虚拟投射（非破坏性） | ~$0 | `CONTEXT_COLLAPSE` 开关 |
| **5** | Auto-Compact | 完整 LLM 摘要（最后手段） | $$$ | `compact.ts` |

#### Layer 0: 大结果磁盘持久化（预 compaction）

在 compaction 之前，先控制进入 context 的数据量：工具返回超过 50K chars（v2.1.51 起，原为 100K），完整结果写磁盘，context 中仅保留 ~2KB 预览 + 文件路径。

```python
def maybe_persist_large_result(tool_result, threshold=50000):
    if len(tool_result.content) <= threshold:
        return tool_result  # 直接保留在 context
    
    filepath = write_to_disk(tool_result.content, tool_result.id)
    preview = tool_result.content[:2000]
    return ToolResult(
        content=f"Output too large ({size}). Saved to: {filepath}\n"
                f"Preview:\n{preview}\n...\n"
    )
```

- 使用 `O_CREAT|O_EXCL` 模式写入，避免重复
- **Read 工具显式设置 threshold = Infinity**，豁免持久化（避免循环依赖：Read → 写文件 → 再去 Read 那个文件）
- 消息级别也有 200K chars 聚合 budget：N 个并行工具各返回 40K 组合成 400K 怪物会被限制

#### Layer 1: Cached Microcompact（缓存微压缩）

最优雅的一层。利用 Anthropic API 的 `cache_edits` 能力，**直接在服务端缓存中删除旧 tool result**，本地消息完全不动。

```
API Server Cache:
+------------------+
| system prompt    | <-- cached prefix (preserved)
| tools            |
| msg[0]: user     |
| msg[1]: asst     |
| msg[2]: user/tr  | <-- cache_edits: delete this tool_result
| msg[3]: asst     |
| msg[N]: user     | <-- new turn appended
+------------------+
```

- 只清理特定工具的结果：Bash, Read, Grep, Glob, WebFetch, WebSearch, FileEdit, FileWrite
- 保留最近 N 个结果，删除更早的
- **零 I/O、零内容修改**——只操作服务端缓存
- 仅在主线程运行，避免 fork agent 的 tool result 污染全局状态

#### Layer 2: Time-Based Microcompact（时间触发微压缩）

当用户离开超过 60 分钟（匹配 Anthropic 缓存 TTL = 1 小时）后回来，缓存已冷，是一个清理的好时机。

```python
def time_based_microcompact(messages):
    gap_minutes = (now() - last_assistant.timestamp) / 60
    if gap_minutes < 60:  # 缓存可能还热着
        return None
    
    # 缓存已冷，可以安全修改本地消息
    keep_set = compactable_ids[-keep_recent:]  # 保留最近
    clear_set = compactable_ids[:-keep_recent]  # 清理旧的
    
    for msg in messages:
        for block in msg.tool_results:
            if block.id in clear_set:
                block.content = "[Old tool result content cleared]"
```

与 Layer 1 互斥：时间触发先运行，如果触发则跳过 Cached MC。

#### Layer 3: Session Memory Compact（会话记忆压缩）

**这是整个系统中最有趣的设计**。Session Memory 是一个后台进程，在整个会话期间持续维护一个结构化的 markdown 笔记文件。当需要 compaction 时，直接使用这些笔记作为摘要——**零额外 LLM 调用**。

笔记文件模板：

```markdown
# Session Title
_A short and distinctive 5-10 word descriptive title..._

# Current State
_What is actively being worked on right now?..._

# Task specification
_What did the user ask to build?..._

# Files and Functions
_What are the important files?..._

# Errors & Corrections
_Errors encountered and how they were fixed..._

# Worklog
_Step by step, what was attempted, done?..._
```

- 后台提取作为 fork agent 运行（共享主 session 的 prompt cache）
- 仅允许使用 Edit 工具操作笔记文件
- 触发条件：token count + tool call count 双阈值，且最近回复不含 tool call
- 每节有 2000 token 上限，总计 12000 token
- 如果模板为空（session 太短没有提取机会），回退到 Full Compact（Layer 4）

**这是 Claude Code 独有的创新** ⭐——将昂贵的摘要操作平摊到整个 session 生命周期。

#### Layer 4: Full Compact（完整压缩）

当 Session Memory 不可用或 post-compaction token 仍超阈值时，回退到完整 LLM 摘要。

关键实现细节：

```markdown
CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.
Tool calls will be REJECTED and will waste your only turn — you will fail the task.
```

- 使用 **forked agent**（共享主 session 的 prompt cache prefix，节省输入 token 费用）
- 两阶段 prompt：`<analysis>`（思考草稿，提取后被剥离）+ `<summary>`（9 个章节：Primary Request, Key Technical Concepts, Files, Errors, Problem Solving, User Messages, Pending Tasks, Current Work, Optional Next Step）
- maxTurns: 1——如果 model 调用工具而非生成文本，则整个 compact 失败
- Sonnet 4.6 的失败率约 2.79%，Sonnet 4.5 仅 0.01%
- 图像替换为 `[image]` 文本标记，文档替换为 `[document]`
- 最多 3 次重试（prompt-too-long 时分组丢弃最旧轮次的消息）

### 3.2 5 层 Cascade 决策流程

```
autoCompactIfNeeded(messages, context):
    │
    ├── Circuit breaker: 3 consecutive failures? → SKIP
    │
    ├── Token count < threshold (~167K for 200K window)? → SKIP
    │       (threshold = context_window - max_output_tokens - 13K buffer)
    │
    ├── Try Session Memory Compact (Layer 3)  → 若成功，返回
    │
    └── Try Full Compact (Layer 4)  → 最终手段
```

**设计哲学**："defer as long as possible, keep it as cheap as possible, escalate in stages"

```
Cost     ^
         |                                      * Full Compact ($$$, LLM)
         |
         |                 * Session Memory ($0, incremental notes)
         |
         |     * Time-Based MC ($0, content clearing)
         |
         | * Cached MC (~$0, cache edit API)
         |
         +-------------------------------------------------------->
              Compression Quality
```

### 3.3 9 个 Context 来源（有序）

System prompt → Environment info → CLAUDE.md hierarchy → Path-scoped rules → Auto-memory → Tool metadata → Conversation history → Tool results → Compact summaries

### 3.4 CLAUDE.md 4 级层次（Lazy-loaded）

| 级别 | 路径 | 作用域 |
|------|------|--------|
| Managed | `/etc/claude-code/CLAUDE.md` | 全系统（企业） |
| User | `~/.claude/CLAUDE.md` | 每用户 |
| Project | `CLAUDE.md`, `.claude/CLAUDE.md`, `.claude/rules/*.md` | 每项目 |
| Local | `CLAUDE.local.md` | 个人（gitignored） |

**关键设计决策**：CLAUDE.md 是 **user context**（概率性合规），不是 system prompt（确定性执行）。Permission rules 提供确定性执行层。每 turn 重读以确保变更生效。

### 3.5 Deferred Tool Schemas（延迟工具 Schema）

当 MCP 工具数量多到描述超过 context 的 10% 时，启动 ToolSearch：
- 启动时只加载工具名称 + searchHint（3-10 词的能力短语）
- Model 通过关键词搜索发现工具
- 完整 schema 按需加载

```
// Tool 接口中的 searchHint 字段
searchHint?: string  // 3-10 words, no trailing period
                     // "Search code across repository"
                     // "Query PostgreSQL database"
```

Token 节省达 85%。

### 3.6 Summary-only Subagent Returns

子 agent 只返回**最终摘要**给父 agent——所有中间工具调用、文件读取、测试输出都留在子 agent 的独立 context 中。详见 [§6 子 Agent 编排](#6-子-agent-编排)。

### 3.7 对 Jeeves 的启示

| Claude Code 设计 | Jeeves 可借鉴 | 优先级 |
|------------------|---------------|--------|
| Session Memory 后台笔记 | 平摊摘要成本，避免一次性 LLM 调用——可能是最值得借鉴的设计 | 🔴 高 |
| Cached Microcompact | 利用 API 缓存编辑能力零成本清理——依赖 API 支持 | 🟡 中 |
| 大结果磁盘持久化 | 控制进入 context 的数据量，不依赖 LLM——简单但有奇效 | 🔴 高 |
| 5 层渐进升级 | 每层尽力避免调用下一层——成本敏感型设计 | 🔴 高 |
| Deferred Tool Schemas | 工具多时只加载名称，按需加载 schema——85% token 节省 | 🔴 高 |
| 每 turn 重读规则文件 | CLAUDE.md 变更立即生效，无需重启 | 🟢 低 |

---

## 4. MCP 集成

### 4.1 架构

Claude Code 作为 MCP Host，为每个 MCP Server 创建独立的 MCP Client 连接：

```
Claude Code (Host)
├── MCP Client 1 ←→ MCP Server A (stdio, 本地)
├── MCP Client 2 ←→ MCP Server B (HTTP, 远程)
├── MCP Client 3 ←→ MCP Server C (SSE, 旧版/远程)
└── ... 
```

### 4.2 三种传输模式

| 传输 | 连接方式 | 使用场景 | 状态 |
|------|----------|----------|------|
| **stdio** | 子进程 stdin/stdout | 本地 MCP server | ✅ 主流 |
| **Streamable HTTP** | HTTP POST + SSE streaming | 远程 server（新版） | ✅ 推荐 |
| **HTTP + SSE** (legacy) | GET SSE stream + POST | 远程 server（旧版） | ⚠️ 已弃用 |

实际支持 7 种传输类型：stdio, SSE, HTTP, WebSocket, SDK, IDE，以及企业内部传输。

VILA-Lab 分析指出 Claude Code 有 **42 个工具子目录**，其中相当一部分与 MCP 集成相关。

### 4.3 工具延迟发现机制（ToolSearch）⭐

这是 Claude Code 的 MCP 集成最创新的设计：

```
正常模式（ToolSearch 关闭）：
  启动时 → 所有 MCP 工具 schema 加载到 context → 可能消耗 67K+ tokens

ToolSearch 模式（当 MCP 描述超过 context 的 10% 时自动启用）：
  启动时 → 只加载工具名称 + searchHint → ~10K tokens
  Model 需要时 → 关键词搜索 → 按需加载完整 schema
```

实现细节：

```typescript
// 工具接口
interface Tool {
    name: string           // 工具名
    searchHint?: string    // 一行能力描述，用于关键词匹配
    description(input): Promise<string>  // 完整 schema，按需调用
    inputSchema: ZodType   // 参数 schema
}

// ToolSearch 工作流
function toolSearch(query: string, toolPool: Tool[]): Tool[] {
    return toolPool.filter(t => 
        keywordMatch(t.name, t.searchHint, query)
    ).map(t => loadFullSchema(t))
}
```

**已知问题**（截至 2026 Q2）：
- 首回合不可用（agent 在第一个 turn 看不到 deferred tools）
- 压缩后丢失引用（compaction 后可能忘记已加载的工具）
- PreToolUse hooks 与 deferred tool loading 冲突导致 hang

### 4.4 工具搜索工作原理

从社区逆向分析：

```typescript
// src/Tool.ts 中的工具匹配
export function toolMatchesName(
    tool: { name: string; aliases?: string[] },
    name: string,
): boolean {
    return tool.name === name || (tool.aliases?.includes(name) ?? false)
}
```

ToolSearch 基于 `searchHint` 进行关键词匹配，帮助 model 在大工具池中发现相关工具。

### 4.5 输出截断策略

| 参数 | 默认值 | 说明 |
|------|--------|------|
| Tool result cap | ~25K tokens / ~50K chars | 硬编码，不可用户配置 |
| Preview | ~2KB | 在 context 中保留的预览量 |
| 磁盘持久化 | 超过阈值即触发 | 完整结果写入文件 |
| MCP `maxResultSizeChars` | 可到 500K chars | MCP server 可通过 `_meta["anthropic/maxResultSizeChars"]` 覆盖 |
| 消息级聚合 budget | 200K chars | 防止 N 个并行工具返回过大 |

**重要**：25K token cap 是 harness 级别的决定，**不是 model 限制**。Model 有 200K+ context window，但 harness 为了 context 管理主动截断。

```
Bash tool 输出处理：
  1. 命令执行 → stdout + stderr 捕获
  2. 如果总输出 > 50K chars → 写入 /tmp/claude-{id}.txt，context 中保留 ~2KB 预览
  3. 如果总输出 ≤ 50K chars → 直接放入 context
```

### 4.6 对 Jeeves 的启示

| Claude Code 设计 | Jeeves 可借鉴 | 优先级 |
|------------------|---------------|--------|
| ToolSearch 延迟发现 | MCP 工具多时只注册名称，按需加载完整 schema——85% token 节省 | 🔴 高 |
| 输出截断 + 磁盘持久化 | tool output 兜底策略，防止 context 爆炸 | 🔴 高 |
| 多种传输模式支持 | 支持 stdio + HTTP + SSE，覆盖本地和远程场景 | 🟡 中 |
| 10% context 触发阈值 | 自适应启用 ToolSearch | 🟢 低 |

---

## 5. Hooks 系统

> "The model proposes; the harness, through your hooks, disposes — deterministically, at well-defined points in the lifecycle."

### 5.1 架构总览

Hooks 是 shell 脚本（或 HTTP endpoint / LLM prompt / agent），在会话生命周期的特定点自动执行。**零 context 消耗**（不占用 model 的 context window）。

四种执行类型：
| 类型 | 执行方式 | 适用场景 |
|------|----------|----------|
| **command** | Shell 命令 + JSON stdin | 本地脚本、安全检查 |
| **HTTP** | HTTP POST + JSON body | 远程服务、webhooks |
| **prompt** | LLM 评估 | 需要推理的决策 |
| **agent** | 子 agent 验证 | 复杂验证逻辑 |

### 5.2 完整 Hook 事件列表（27 个）

#### 工具授权事件（5 个）
| 事件 | 触发时机 | 可阻断？ | 可修改？ |
|------|----------|----------|----------|
| `PreToolUse` | 工具执行前 | ✅ 可阻断 | ✅ 可修改参数 |
| `PermissionRequest` | 需要权限决策时 | ✅ 可决定 | ✅ 可修改 |
| `PostToolUse` | 工具执行成功后 | ❌ | ✅ 可替换输出 |
| `PostToolUseFailure` | 工具执行失败后 | ❌ | ✅ 可注入建议 |
| `PermissionDenied` | auto-mode 拒绝后 | ❌ | ✅ 可建议重试 |
| `PostToolBatch` | 一批并行工具调用全部完成后 | ❌ | ✅ 可注入上下文 |

#### 会话生命周期事件（4 个）
| 事件 | 触发时机 |
|------|----------|
| `SessionStart` | 会话开始/恢复时（`startup`, `resume`, `clear`, `compact`） |
| `SessionEnd` | 会话终止时（`clear`, `resume`, `logout` 等） |
| `Setup` | `--init-only` 或 `--init`/`--maintenance` in `-p` mode |
| `Stop` | Claude 完成响应时——**可强制继续** |

#### 用户交互事件（3 个）
| 事件 | 触发时机 |
|------|----------|
| `UserPromptSubmit` | 用户提交 prompt 后，Claude 处理前——**可注入额外 context** |
| `UserPromptExpansion` | 斜杠命令展开为 prompt 时 |
| `MessageDisplay` | 助手消息文本显示时 |

#### 子 Agent 协调事件（5 个）
| 事件 | 触发时机 |
|------|----------|
| `SubagentStart` | 子 agent 被创建时 |
| `SubagentStop` | 子 agent 完成时——**子 agent 版本的 Stop** |
| `TaskCreated` | TaskCreate 创建任务时 |
| `TaskCompleted` | 任务被标记为完成时 |
| `TeammateIdle` | Agent team 成员即将空闲时 |

#### 上下文管理事件（5 个）
| 事件 | 触发时机 |
|------|----------|
| `PreCompact` | 上下文压缩前——**准备上下文或运行分析** |
| `PostCompact` | 上下文压缩完成后 |
| `InstructionsLoaded` | CLAUDE.md 或规则文件加载时 |
| `ConfigChange` | 配置文件变更时 |
| `Notification` | Claude Code 发送通知时 |

#### 工作区事件（4 个）
| 事件 | 触发时机 |
|------|----------|
| `CwdChanged` | 工作目录变更时（如 `cd` 命令） |
| `FileChanged` | 监控的文件变更时 |
| `WorktreeCreate` | Worktree 创建时 |
| `WorktreeRemove` | Worktree 移除时 |

#### MCP 事件（2 个）
| 事件 | 触发时机 |
|------|----------|
| `Elicitation` | MCP server 请求用户输入时 |
| `ElicitationResult` | 用户回复 MCP 请求后 |

### 5.3 Hook 配置格式

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "if": "Bash(rm *)",
            "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/block-rm.sh",
            "timeout": 5
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Write",
        "hooks": [
          {
            "type": "command",
            "command": "npx prettier --write ${CLAUDE_PROJECT_DIR}/src/**/*.ts"
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "prompt",
            "prompt": "Did the agent complete all requested tasks? Check test results and file changes.",
            "model": "haiku"
          }
        ]
      }
    ]
  }
}
```

**matcher 支持两种过滤**：
1. Event 级别的 `matcher`：匹配工具名（如 `"Bash"`、`"Write"`、`"mcp__*"`）
2. Handler 级别的 `if`：细化匹配（如 `"Bash(rm *)"`），只在两个过滤都匹配时才执行

**Hook 位置优先级**（从高到低）：
1. Managed settings（企业部署，最高优先级）
2. `--hooks` CLI flag
3. `.claude/settings.json`（项目级）
4. `~/.claude/settings.json`（用户级）
5. Plugin `hooks/` 目录

### 5.4 Hook 的决策控制（权限边界）

Hooks 通过 **exit code** 和 **JSON stdout** 控制 agent 行为：

| Exit Code | 含义 |
|-----------|------|
| `0` | 成功，无特殊处理 |
| `1` | 非阻塞错误，stderr 注入 context |
| `2` | **阻塞**——行为因事件而异 |

**Exit code 2 的行为**：

| 事件 | Exit 2 的含义 |
|------|---------------|
| `PreToolUse` | 阻断工具调用 |
| `PostToolUse` | 注入 stderr 作为反馈 |
| `UserPromptSubmit` | 阻断 prompt（不发送给 Claude） |
| `Stop` | **强制 Claude 继续工作** |
| `SubagentStop` | 强制子 agent 继续工作 |
| `PreCompact` | 阻断压缩 |
| `SessionStart` | 阻断会话启动 |
| `TaskCompleted` | 阻止任务标记为完成 |

**JSON 输出支持的决策控制**：

```json
// PreToolUse
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",        // deny | ask
    "permissionDecisionReason": "Blocked by security policy",
    "updatedInput": { "command": "safe-alternative" }  // 修改工具参数
  }
}

// PostToolUse
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "Test failed: expected 5, got 4",
    "updatedToolOutput": "Modified result..."
  }
}

// Stop
{
  "hookSpecificOutput": {
    "hookEventName": "Stop",
    "decision": "block",
    "additionalContext": "Tests are failing. Fix them before stopping."
  }
}
```

### 5.5 Hook 的权限边界

**Hook 可以做的事**：
- ✅ 阻断任何工具调用（`PreToolUse` exit 2）
- ✅ 修改工具参数（`PreToolUse` `updatedInput`）
- ✅ 替换工具输出（`PostToolUse` `updatedToolOutput`）
- ✅ 注入额外 context 到对话
- ✅ 强制 agent 继续工作（`Stop` exit 2）
- ✅ 读取/写入文件系统
- ✅ 调用外部 API
- ✅ 修改环境变量（`SessionStart`）

**Hook 不能做的事**：
- ❌ 绕过 deny 规则（Hook 的 `allow` 不会覆盖 deny 规则）
- ❌ 访问 model 的思考过程/内部状态
- ❌ 在异步模式下阻断工具调用（async hook 的执行结果在下个 turn 才生效）

### 5.6 四种 Hook 执行模式

1. **同步 command hook**：阻塞 agent loop 直到完成，支持决策控制
2. **异步 command hook**：不阻塞，适合测试运行、日志记录——不能返回决策
3. **HTTP hook**：调用外部服务，支持 `allowedEnvVars` 白名单
4. **Prompt hook**：用 LLM 评估，适合复杂多条件判断（如 Stop hook 检查多个条件）

### 5.7 对 Jeeves 的启示

| Claude Code 设计 | Jeeves 可借鉴 | 优先级 |
|------------------|---------------|--------|
| Exit code 协议 | 简单二进制协议：0=OK, 1=error, 2=block——极其简洁但强大 | 🔴 高 |
| Stop hook 强制继续 | agent 提前停止时 hook 可强制继续——解决 early stopping | 🔴 高 |
| PreToolUse 修改参数 | 不只是 approve/deny，还能修改——比二进制安全模型更灵活 | 🟡 中 |
| 异步 hook | 运行测试/格式化不阻塞 agent——实用性强 | 🔴 高 |
| `if` + `matcher` 双层过滤 | 减少不必要的 hook 执行 | 🟢 低 |

---

## 6. 子 Agent 编排

### 6.1 核心模型

子 agent 是**独立的 Claude 实例**，有自己的 context window、system prompt、工具集和权限模式。父 agent 只接收子 agent 的**最终消息**——所有中间工具调用、文件读取、测试输出完全隔离。

```
Parent Context Window:
  ├── 用户: "分析三个模块"
  ├── Claude: Agent(Explore, "分析 auth 模块")     ─┐
  ├── Claude: Agent(Explore, "分析 db 模块")       ─┤ 并行
  ├── Claude: Agent(Explore, "分析 api 模块")      ─┘
  ├── ✓ auth 结果（仅摘要）                          ← 只返回摘要
  ├── ✓ db 结果（仅摘要）
  ├── ✓ api 结果（仅摘要）
  └── Claude: "综合结论..."

Subagent Context (auth):
  ├── System prompt (Explore)
  ├── "分析 auth 模块"
  ├── Grep "auth*"... [结果]
  ├── Read auth.ts... [结果]
  ├── Read auth.test.ts... [结果]
  └── 最终: "auth 模块有 3 个端点，2 个未测试..."
      ↑ 只有这行回到父 context
```

### 6.2 Agent 工具（原名 Task）

子 agent 通过 `Agent` 工具（v2.1.63 前叫 `Task`）调用：

```markdown
Agent(subagent_type="Explore", description="Search auth module", 
      prompt="Find all authentication endpoints in src/auth/")
```

- `Agent` 和 `Task` 是同一机制的不同名称（旧名作 alias 仍可用）
- 父 agent 需要 `Agent` 在 `allowedTools` 中才能创建子 agent
- 子 agent 的 `tools` 字段控制其工具访问

### 6.3 内置子 Agent 类型

| 类型 | 模型 | 工具 | 用途 | 特殊行为 |
|------|------|------|------|----------|
| **Explore** | Haiku | 只读（被禁止 Write/Edit） | 快速代码搜索和分析 | 跳过 CLAUDE.md + git status |
| **Plan** | 继承父模型 | 只读（被禁止 Write/Edit） | plan mode 中收集上下文 | 跳过 CLAUDE.md + git status |
| **General-purpose** | 继承父模型 | 全部工具 | 复杂多步任务 | 完整上下文加载 |

还有 `statusline-setup`（Sonnet）和 `claude-code-guide`（Haiku）两个辅助 agent。

### 6.4 自定义子 Agent

定义在 `.claude/agents/*.md`（项目级）或 `~/.claude/agents/*.md`（用户级）：

```markdown
---
name: code-reviewer
description: Reviews code for quality and best practices. Use proactively.
tools: Read, Glob, Grep
model: sonnet
permissionMode: acceptEdits
maxTurns: 10
skills: [typescript-best-practices]
---
You are a code reviewer. When invoked, analyze the code and provide 
specific, actionable feedback on quality, security, and best practices.
```

**关键字段**：

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `name` | 唯一标识符（lowercase + hyphens） | 必填 |
| `description` | **何时委派给此 agent**——给 dispatcher 看的，不是给人看的 | 必填 |
| `tools` | 允许列表——**省略 = 继承全部工具**（陷阱！） | 继承全部 |
| `disallowedTools` | 拒绝列表（从 tools 中减去） | 无 |
| `model` | sonnet/opus/haiku/full ID/inherit | `inherit` |
| `permissionMode` | 子 agent 的权限模式 | 继承父 |
| `maxTurns` | 最大 agentic turns | 无限制 |
| `skills` | 预加载到子 agent context 的 skill | 无 |
| `mcpServers` | 子 agent 专用 MCP server | 继承父 |
| `hooks` | 子 agent 范围的生活周期 hook | 无 |
| `background` | 始终后台运行（auto-deny 权限） | `false` |
| `isolation` | `worktree` = 临时 git worktree 隔离 | 无 |

### 6.5 并行调度

```markdown
"Research the authentication, database, and API modules 
 in parallel using separate subagents."
```

- 多个独立 `Agent` 调用可以在**单个 model response 中**并行发出
- 父 agent 会并行创建子 agent，它们并发运行
- Claude 默认保守使用并行——需要**显式要求**
- 并行度上限：`CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` 环境变量
- 会话总量上限：`CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION`

### 6.6 前台 vs 后台执行

| 模式 | 行为 | 权限处理 |
|------|------|----------|
| **前台** | 阻塞主对话直到完成 | 权限弹窗**透传给用户** |
| **后台** | 并发运行，用户继续工作 | 权限请求**自动 deny**——这是最常见 failure mode |

**关键陷阱**：后台子 agent 无法请求用户批准！如果任务需要用户确认的操作，该操作会失败。解决方案：重新把同一任务作为前台子 agent 运行。

### 6.7 三种隔离模式

| 模式 | 机制 | 默认 |
|------|------|------|
| Worktree | Git worktree（文件系统隔离） | 否 |
| Remote | 远程执行（internal-only） | 否 |
| In-process | 共享文件系统，隔离对话 | **是** |

### 6.8 Sidechain Transcripts

- 每个子 agent 写自己的 `.jsonl` 文件
- 只有摘要返回父 agent
- 完整历史**永不进入**父 context
- 多实例协调使用 POSIX `flock()`——零外部依赖

### 6.9 子 Agent 消息 ≠ 用户批准

**重要设计决策**：子 agent 的指令（即使来自用户通过父 agent）**不等于**用户对每个操作的批准。权限系统仍然独立运行——这确保即使被恶意指令的子 agent 也无法绕过权限门控。

### 6.10 Fork vs Subagent

| 特性 | Subagent | Fork |
|------|----------|------|
| Context 起点 | 空白 + prompt | 继承完整父对话 |
| System prompt | 自定义 | 与父相同 |
| 工具集 | 可限制 | 与父相同 |
| Prompt cache | 独立 | 复用父的（第一个请求） |
| 使用场景 | 自包含的调研/执行任务 | 需要大量父 context 的分支任务 |

### 6.11 对 Jeeves 的启示

| Claude Code 设计 | Jeeves 可借鉴 | 优先级 |
|------------------|---------------|--------|
| Summary-only returns | 只返回摘要，中间结果不污染父 context——这是子 agent 的核心价值 | 🔴 高 |
| 独立 context window | 每个子 agent 完全隔离——实现真正的关注点分离 | 🔴 高 |
| tools 省略 = 全部工具 | 这个默认值很危险——但作为设计教训很有价值 | 🟡 中 |
| 后台 auto-deny 权限 | 明确的设计取舍：并发 vs 权限——需要显式告知用户 | 🔴 高 |
| Sidechain JSONL | 每个子 agent 独立转录文件——可审计、可回放 | 🟡 中 |
| Fork 复用 prompt cache | 同名 context 工作 fork 比 subagent 便宜——精细的性能优化 | 🟢 低 |

---

## 7. Claude Agent SDK

### 7.1 SDK 定位

> "The Claude Agent SDK gives you the same agent loop, built-in tools, and context management that power Claude Code, programmable in TypeScript and Python."

SDK 是 Claude Code 驾驭引擎的开放出口——将 CLI 中的能力封装为库，供开发者在自己的进程中构建 agent。

| 产品 | 定位 | 何时使用 |
|------|------|----------|
| **Agent SDK** | Python/TypeScript 库，在自己进程中运行 agent loop | 构建自定义 agent 应用 |
| **Claude Code CLI** | 终端交互式工具 | 日常开发、一次性任务 |
| **Client SDK** | 直接调 Anthropic API | 自己实现 tool loop |
| **Managed Agents** | 托管 REST API（Anthropic 运行 agent） | 不需要自己管理基础设施 |

### 7.2 SDK 暴露的配置面

SDK 通过 `options` 对象暴露所有能力：

```typescript
// TypeScript SDK 入口
import { query } from '@anthropic-ai/claude-agent-sdk'

const result = await query({
    prompt: "Build a REST API for todos",
    model: "claude-sonnet-4-6",
    
    // 工具
    tools: ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
    allowedTools: ["Read", "Bash(git diff *)", "Grep"],
    
    // MCP
    mcpServers: {
        postgres: { command: "npx", args: ["-y", "@anthropic/mcp-postgres"] }
    },
    
    // 权限
    permissionMode: "acceptEdits",
    permissions: {
        deny: ["Bash(rm *)", "Edit(.env)"],
        allow: ["Read", "Grep", "Glob"]
    },
    
    // Hooks
    hooks: {
        PreToolUse: [{
            matcher: "Bash",
            hooks: [{ type: "command", command: "./security-check.sh" }]
        }]
    },
    
    // 子 Agent
    agents: {
        "code-reviewer": {
            description: "Reviews code for quality",
            tools: ["Read", "Glob", "Grep"],
            model: "haiku"
        }
    },
    
    // Context
    settingSources: ["user", "project"],  // 加载 .claude/ 和 ~/.claude/
    systemPrompt: "You are a specialized Python developer...",
    
    // Session
    sessionId: "my-session",
    resume: "latest",
    forkSession: false
})
```

### 7.3 SDK 对 Jeeves 的启示

| Claude Code 设计 | Jeeves 可借鉴 | 优先级 |
|------------------|---------------|--------|
| SDK 即 Harness-as-a-Service | 开放的 SDK 将驾驭引擎能力产品化——Viv Trivedy 提出的 HaaS 概念 | 🔴 高 |
| TypeScript + Python 双语言 | 覆盖前端和后端开发者 | 🟡 中 |
| 统一 options 对象 | 所有配置通过一个结构体传递，简化 API | 🟢 低 |
| 共享 CLI 的 context 管理 | SDK 与 CLI 使用相同的底层引擎——避免重复实现 | 🔴 高 |

---

## 8. 工具系统

### 8.1 Bash 作为通用工具的设计哲学

> "Give the agent bash and let it build the tools it needs on the fly."

Claude Code 的哲学是不为每种可能操作预制工具，而是将 Bash 作为通用工具，agent 通过 shell 命令自行组合。其他专用工具（Read、Edit、Grep 等）提供更结构化、更安全的常用操作。

### 8.2 工具接口设计

```typescript
// src/Tool.ts — 完整工具接口
type Tool<Input, Output> = {
    readonly name: string               // 主标识符
    aliases?: string[]                  // 向后兼容
    searchHint?: string                 // 3-10 词能力描述，ToolSearch 使用
    
    call(args, context, canUseTool, parentMessage, onProgress?): Promise<ToolResult>
    description(input, options): Promise<string>  // 动态描述
    
    readonly inputSchema: ZodType       // 参数 schema（Zod）
    readonly inputJSONSchema?: object   // JSON Schema 格式
    
    isConcurrencySafe(input): boolean   // 能否与其他工具并发
    isReadOnly(input): boolean          // 是否只读
    isDestructive?(input): boolean      // 是否破坏性
    
    checkPermissions(input, context): Promise<PermissionResult>
    userFacingName(input): string       // 用户可读名称
    maxResultSizeChars: number          // 输出截断阈值
    
    renderToolUseMessage(...): ReactNode
    renderToolResultMessage(...): ReactNode
}
```

**关键字段**：

- **`isConcurrencySafe`**：告诉 harness 此工具能否与其他工具同时执行——实现智能并发调度
- **`isReadOnly`**：告诉权限系统是否需要审批——`isReadOnly=true` 的工具自动通过 `default` 模式
- **`isDestructive`**：标记破坏性操作——`auto` 模式分类器重点关注
- **`maxResultSizeChars`**：每个工具可以有自己的输出截断阈值
- **`call` 中的 `canUseTool`**：工具本身可以递归调用其他工具（如 SkillTool 内部调用 AgentTool）

### 8.3 工具分类（~19-40+ 个工具）

#### 核心文件操作
| 工具 | 只读 | 说明 |
|------|------|------|
| Read | ✅ | 读取文件（threshold = Infinity，永不被磁盘持久化） |
| Write | ❌ | 写入文件 |
| Edit | ❌ | 精确编辑（find-and-replace） |
| Glob | ✅ | 文件搜索（glob pattern） |
| Grep | ✅ | 内容搜索（regex） |
| LSP | ✅ | 语言服务器协议（代码导航） |

#### Shell 执行
| 工具 | 说明 |
|------|------|
| Bash | 通用 shell 命令执行（Linux/macOS） |
| PowerShell | Windows PowerShell 命令执行 |

#### 版本控制
| 工具 | 说明 |
|------|------|
| Git | Git 操作 |

#### Web
| 工具 | 说明 |
|------|------|
| WebFetch | HTTP 请求 |
| WebSearch | 网络搜索 |

#### Notebook
| 工具 | 说明 |
|------|------|
| NotebookEdit | Jupyter notebook 编辑 |

#### Agent 协调
| 工具 | 说明 |
|------|------|
| Agent（原 Task） | 创建子 agent |
| SkillTool | 加载并注入 skill 内容 |
| TaskCreate / TaskList / TaskGet / TaskUpdate | 任务管理（Dynamic Workflows） |
| SendMessage | 跨 agent 消息 |
| TeamCreate / TeamDelete | Agent team 管理 |

#### MCP
| 工具 | 说明 |
|------|------|
| `mcp__*` | 动态注册的 MCP 工具 |

### 8.4 工具池组装 5 步 Pipeline

```
Base enumeration (up to 54 tools)
    → Mode filtering (按权限模式过滤)
    → Deny rule pre-filtering (移除被 deny 的)
    → MCP integration (添加 MCP 工具)
    → Deduplication (去重)
```

### 8.5 输出截断和磁盘持久化

```
Tool.exec() → result
    │
    ├── result.size ≤ maxResultSizeChars → 直接放入 context
    │
    └── result.size > maxResultSizeChars →
         ├── 完整结果写磁盘 (O_CREAT|O_EXCL, 按 tool_use_id 唯一)
         ├── Context 中保留 ~2KB 预览 + 文件路径
         └── Model 可后续调用 Read 工具读取完整文件
```

**关键设计决策**：
- Read 工具 `threshold = Infinity`——自身豁免持久化，避免 Read→写文件→再 Read 的循环
- 消息级 200K chars 聚合 budget——N 个并行工具的结果总和不能超过此值
- 已替换的结果状态冻结——一旦 tool_result 被替换，其命运就不再改变，保证 prompt cache 一致性
- 空结果替换为 `(toolName completed with no output)`——防止 model 将空结果误认为对话边界

### 8.6 对 Jeeves 的启示

| Claude Code 设计 | Jeeves 可借鉴 | 优先级 |
|------------------|---------------|--------|
| Bash 作为通用工具 | 不预制所有工具，让 agent 通过 shell 自行组合——减少工具维护成本 | 🔴 高 |
| isConcurrencySafe + isReadOnly 标记 | 在工具接口上声明行为属性，让 harness 做智能调度 | 🔴 高 |
| 输出截断策略 | 防 context 爆炸的硬约束，每个工具可自定义阈值 | 🔴 高 |
| 5 步工具池组装 | 模式过滤 + deny 预过滤 + MCP 集成 + 去重——标准化 pipeline | 🟡 中 |
| `canUseTool` 递归 | 工具内部可调用其他工具——组合能力的原子化 | 🟢 低 |

---

## 9. 对 Jeeves 的启示——总结

### 9.1 Claude Code 独有的创新（⭐ 标记）

1. ⭐ **Auto-mode Classifier**（Layer 4）：独立 ML 模型评估工具安全性，而非让主 model 自评
2. ⭐ **Session Memory 后台笔记**：平摊摘要成本到整个 session，compaction 时零额外 LLM 调用
3. ⭐ **Cached Microcompact**：利用 API 缓存编辑能力删除旧 tool result，零 I/O 零内容修改
4. ⭐ **ToolSearch 延迟发现**：工具多时只加载名称 + searchHint，按需加载完整 schema
5. ⭐ **Summary-only subagent returns**：子 agent 完全隔离，只返回结论
6. ⭐ **Deny-first 原则**：deny > ask > allow，严格优先

### 9.2 Jeeves 应优先实现的能力（按优先级）

#### 🔴 高优先级（核心安全 + Context 管理）

1. **Deny-first 权限规则**：无论 allow 有多具体，deny 总是赢
2. **7 层纵深防御**：每层独立、任一层可阻断
3. **大结果磁盘持久化**：超过阈值自动写盘，context 中只留预览
4. **5 层渐进 compaction**：每次 model 调用前最便宜的 compaction 先跑
5. **子 Agent 只返回摘要**：中间结果不污染父 context
6. **Bash 作为通用工具**：不预制所有专用工具
7. **`isConcurrencySafe` + `isReadOnly` 标记**：工具接口上声明行为属性
8. **Exit code hook 协议**：0=OK, 1=error, 2=block——简单但强大

#### 🟡 中优先级（扩展能力）

9. **ToolSearch 延迟发现**：MCP 工具按需加载
10. **独立 ML 分类器**（远期）：独立模型评估安全
11. **5 步工具池组装 pipeline**：标准化工具注册
12. **导出 SDK**：驾驭引擎作为库而非仅 CLI

#### 🟢 低优先级（锦上添花）

13. **Fork 复用 prompt cache**：同名 context 工作 fork 更便宜
14. **每 turn 重读规则文件**：变更立即生效
15. **SkillTool vs AgentTool 分离**：注入 context vs 独立窗口

### 9.3 设计教训

1. **98.4% 是基础设施**：核心 loop 只有几行，真正复杂的都在外面
2. **模型是商品，驾驭是产品**：Harness 决定实际体验
3. **"成功静默，失败详细"**：Hook 输出只在失败时注入错误信息
4. **接受用户摩擦换取安全**：恢复时不恢复权限——安全 > 便利
5. **平摊成本而非一次性支付**：Session Memory 是最好的例子

---

## 参考资源

1. Anthropic 官方文档 — https://code.claude.com/docs
2. VILA-Lab Dive into Claude Code — https://github.com/VILA-Lab/Dive-into-Claude-Code
3. WaveSpeed AI Architecture Breakdown — https://wavespeed.ai/blog/posts/claude-code-agent-harness-architecture
4. Finisky Context Compaction Analysis — https://finisky.github.io/en/claude-code-context-compaction
5. Hidekazu Konishi Subagents Guide — https://hidekazu-konishi.com/entry/claude_code_subagents_and_orchestration_guide.html
6. Hidekazu Konishi Hooks Guide — https://hidekazu-konishi.com/entry/claude_code_hooks_complete_guide.html
7. Addy Osmani Harness Engineering — https://addyosmani.com/blog/agent-harness-engineering
8. Ken Huang Tool Architecture — https://kenhuangus.substack.com/p/claude-code-harness-pattern-2-tool
9. Blake Crosley Hooks Guide — https://blakecrosley.com/blog/claude-code-hooks
10. ArXiv: Design Space of Agent Systems — https://arxiv.org/html/2604.14228v1
11. ArXiv: Auto Mode Stress-Test — https://arxiv.org/html/2604.04978v2
