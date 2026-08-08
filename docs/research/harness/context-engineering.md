# 上下文工程调研：记忆分层、信息选择、结构化记忆、压缩策略

> 基于对 Pi (earendil-works/pi)、OpenCode (anomalyco/opencode)、Claude Code 三个项目源码的深入分析。
> 调研日期：2026-08-08

---

## 一、Pi (earendil-works/pi)

### 1.1 核心压缩算法

**文件**: `packages/agent/src/harness/compaction/compaction.ts` (848行)

#### 1.1.1 Token 估算策略

Pi 采用**混合估算**：优先使用 provider 返回的实际 usage，无 usage 时回退到字符启发式。

```typescript
// compaction.ts:216-244 — 双路径 token 估算
export function estimateContextTokens(messages: AgentMessage[]): ContextUsageEstimate {
  const usageInfo = getLastAssistantUsageInfo(messages);
  if (!usageInfo) {
    // 路径 1：纯字符估算（chars/4 换算）
    let estimated = 0;
    for (const message of messages) {
      estimated += estimateTokens(message);
    }
    return { tokens: estimated, usageTokens: 0, trailingTokens: estimated, lastUsageIndex: null };
  }
  // 路径 2：provider usage + trailing 估算
  const usageTokens = calculateContextTokens(usageInfo.usage);
  let trailingTokens = 0;
  for (let i = usageInfo.index + 1; i < messages.length; i++) {
    trailingTokens += estimateTokens(messages[i]);
  }
  return { tokens: usageTokens + trailingTokens, usageTokens, trailingTokens, lastUsageIndex: usageInfo.index };
}
```

字符估算具体规则（`estimateTokens`, L271-311）：
- **user 消息**: `Math.ceil(chars / 4)`，图片按 `ESTIMATED_IMAGE_CHARS = 4800` 字符计
- **assistant 消息**: 遍历所有 content block，分别计算 text/thinking/toolCall 的字符数
- **toolResult/bashExecution**: 输出被截断至 2000 字符后估算（`serializeConversation`, L91-131）

#### 1.1.2 触发阈值

```typescript
// compaction.ts:157-162 — 默认配置
export const DEFAULT_COMPACTION_SETTINGS: CompactionSettings = {
  enabled: true,
  reserveTokens: 16384,    // 为 summary prompt + 输出预留
  keepRecentTokens: 20000, // 压缩后保留的近期 token 预算
};

// compaction.ts:247-250 — 触发条件
export function shouldCompact(contextTokens: number, contextWindow: number, settings: CompactionSettings): boolean {
  if (!settings.enabled) return false;
  return contextTokens > contextWindow - settings.reserveTokens;
}
```

**关键设计**：不是按绝对 token 数触发，而是按"距 context window 上限的差值"触发 —— 确保总是有余量生成 summary。

#### 1.1.3 切点选择算法（信息选择核心）

`findCutPoint()` (L373-422) 实现了**工具调用原子性保护**的切点选择：

```typescript
// compaction.ts:312-344 — 合法切点的选择规则
function findValidCutPoints(entries: Entry[], startIndex: number, endIndex: number): number[] {
  const cutPoints: number[] = [];
  for (let i = startIndex; i < endIndex; i++) {
    const entry = entries[i];
    switch (entry.type) {
      case "message": {
        const role = entry.message.role;
        switch (role) {
          case "bashExecution": case "custom": case "branchSummary":
          case "compactionSummary": case "user": case "assistant":
            cutPoints.push(i);   // ✅ 可以作为切点
            break;
          case "toolResult":
            break;               // ❌ toolResult 不能作为切点！
        }
        break;
      }
      // thinking_level_change, model_change, compaction, branch_summary: 都不作为切点
    }
  }
  return cutPoints;
}
```

**决策树**（`findCutPoint` 完整流程）：
1. 从后往前累加 token，到达 `keepRecentTokens` 阈值时停止
2. 在停止位置附近寻找最近的**合法切点**（`findValidCutPoints`）
3. 向前回退，跳过非 message 类型的 entry（但遇到上一个 compaction 就停止）
4. 判断是否为**分割 turn**：如果切点不是 user 消息，则向上找 turn 起始点

```typescript
// compaction.ts:346-361 — Turn 起始点查找
export function findTurnStartIndex(entries: Entry[], entryIndex: number, startIndex: number): number {
  for (let i = entryIndex; i >= startIndex; i--) {
    const entry = entries[i];
    if (entry.type === "branch_summary") return i;
    if (entry.type === "message") {
      const role = entry.message.role;
      if (role === "user" || role === "bashExecution") return i;
    }
  }
  return -1;
}
```

#### 1.1.4 Split Turn 处理（记忆分层体现）

当切点落在 turn 中间时，Pi 将前缀单独总结：

```typescript
// compaction.ts:689-702 — Turn 前缀的专用总结模板
const TURN_PREFIX_SUMMARIZATION_PROMPT = `This is the PREFIX of a turn that was too large to keep...
## Original Request
## Early Progress
## Context for Suffix
Be concise. Focus on what's needed to understand the kept suffix.`;
```

`compact()` 函数（L706-793）在 split turn 时调用两次 LLM：
1. 先对历史消息生成 `generateSummaryWithUsage`（含 previousSummary 迭代更新）
2. 再对 turn 前缀生成 `generateTurnPrefixSummary`
3. 拼接：`"{historyText}\n\n---\n\n**Turn Context (split turn):**\n\n{turnPrefixText}"`

注意：turn 前缀用的 `maxTokens = floor(0.5 * reserveTokens)`，比正常 summary 的 `floor(0.8 * reserveTokens)` 更保守。

#### 1.1.5 结构化摘要模板（6-section）

```typescript
// compaction.ts:428-459 — 初始总结模板
const SUMMARIZATION_PROMPT = `
## Goal
[What is the user trying to accomplish?]

## Constraints & Preferences
- [Any constraints, preferences, or requirements]

## Progress
### Done
- [x] [Completed tasks/changes]
### In Progress
- [ ] [Current work]
### Blocked
- [Issues preventing progress]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list]

## Critical Context
- [Any data, examples, or references needed to continue]
`;
```

**迭代更新**（`UPDATE_SUMMARIZATION_PROMPT`, L461-498）：
- **PRESERVE** 所有已有信息
- **ADD** 新的 progress/decisions/context
- **UPDATE** Progress 状态（In Progress → Done）
- 可以**删除**不再相关的内容（唯一允许丢失信息的地方）

**重要设计**：这是**增量更新**而非重新总结 —— 保留上一轮 compaction summary，只追加新信息。

#### 1.1.6 文件追踪（跨 compaction 边界的持久记忆）

```typescript
// compaction.ts:30-35 — Compaction 附带文件操作记录
export interface CompactionDetails {
  readFiles: string[];     // 被压缩历史中读取的文件
  modifiedFiles: string[]; // 被压缩历史中修改的文件
}
```

提取逻辑（`extractFileOperations`, L44-67）：
1. 从上一个 `CompactionEntry.details` 继承文件列表（跨压缩边界传递）
2. 遍历新消息的 assistant/toolCall，提取 read/write/edit 操作的 path
3. 摘要末尾追加 XML 标签 `<read-files>` 和 `<modified-files>`

### 1.2 Session 持久化格式

**文件**: `packages/agent/src/harness/session/types.ts` (393行)

Entry 类型体系（L67-74）：
```
MessageEntry | ModelChangeEntry | ThinkingLevelEntry | ActiveToolsEntry 
| CompactionEntry | BranchSummaryEntry | CustomEntry
```

`CompactionEntry`（L44-51）：
```typescript
{
  type: "compaction";
  summary: string;          // 结构化摘要文本
  retainedTail: AgentMessage[]; // 保留的尾部消息
  tokensBefore: number;     // 压缩前 token 数
  details?: unknown;         // CompactionDetails (readFiles, modifiedFiles)
  usage?: Usage;            // 生成 summary 的 LLM cost
}
```

**上下文重建**（`session/context.ts`, L45-57）：
```typescript
export function defaultContextEntryTransform(pathEntries: readonly Entry[]): Entry[] {
  // 找到最近的 compaction entry
  // 返回 [compaction, ...compaction 之后的 entries]
  // 即：压缩过的历史只保留 summary，未压缩的保留原始消息
}
```

`sessionEntryToContextMessages`（L65-88）中，`compaction` entry 展开为：
1. 一条 `createCompactionSummaryMessage(summary, tokensBefore, timestamp)` —— 自由文本摘要
2. 展开 `retainedTail` —— 压缩时保留的尾部消息（完整保留，不压缩）

### 1.3 Skills 作为"持久记忆"的加载机制

**文件**: `packages/coding-agent/src/core/skills.ts` (487行)

**懒加载（Lazy-Load via XML Index）**：

```typescript
// skills.ts:335-361 — Skills 注入 system prompt 的格式
export function formatSkillsForPrompt(skills: Skill[]): string {
  // 仅注入 name + description + location（XML 格式）
  // 不注入完整内容！
  // 模型需要自己调用 read 工具加载 SKILL.md
  for (const skill of visibleSkills) {
    lines.push("  <skill>");
    lines.push(`    <name>${escapeXml(skill.name)}</name>`);
    lines.push(`    <description>${escapeXml(skill.description)}</description>`);
    lines.push(`    <location>${escapeXml(skill.filePath)}</location>`);
    lines.push("  </skill>");
  }
}
```

**关键设计决策**：
- Skills 的完整内容**不占用 context token**，只是一个 XML 索引
- 模型通过 `read` 工具按需加载 —— 类似 Claude Code 的 `memory.md` 指针模式
- 与 Hermes Agent 形成对比：Hermes 直接将完整 SKILL.md 注入 system prompt

**Skill 发现**（`loadSkillsFromDirInternal`, L173-275）：
- 递归扫描目录，遇到 `SKILL.md` 即停止深入（子目录作为 skill 的 references/templates）
- 支持 `.gitignore` 风格的忽略规则
- 通过 `realPathSet` 去重（symlink 检测）
- 同名 skill 冲突使用 winner/loser 机制：先发现的 win

### 1.4 Pi 上下文工程总结

| 维度 | Pi 的设计 |
|------|-----------|
| **信息选择** | 切点算法：禁止在 toolResult 处切分，保护工具调用原子性；从后往前累加 token 直到阈值 |
| **记忆分层** | CompactionEntry (summary) + retainedTail (原始消息)；split turn 时前缀单独总结 |
| **结构化记忆** | 6-section 固定模板（Goal/Constraints/Progress/KeyDecisions/NextSteps/CriticalContext）；XML 标签 `<read-files>` `<modified-files>` |
| **重要性评判** | 保留"最近的"（token budget 驱动）; 保护"工具调用原子性"; 文件路径始终保留 |
| **压缩策略** | 增量更新（非重新总结）; turn 前缀使用独立模板；maxTokens=0.5×reserveTokens（前缀）vs 0.8×reserveTokens（正常） |
| **持久记忆桥梁** | CompactionDetails 跨边界传递文件列表；Skills 作为懒加载的持久知识 |

---

## 二、OpenCode (anomalyco/opencode)

### 2.1 3 阶段压缩架构

**文件**: `packages/core/src/session/compaction.ts` (241行)

**简要架构**：Selection → Generation → Replacement（在消息链中插入 Compaction 消息）

#### 2.1.1 Selection（信息选择）

```typescript
// compaction.ts:128-159 — select() 函数：从后往前扫描
const select = (entries, tokens) => {
  const conversation = entries
    .filter(entry => entry.message.type !== "compaction")  // ← 跳过已有 compaction
    .map(entry => serialize(entry.message))
    .filter(Boolean);
  
  let total = 0, split = conversation.length;
  // 从后往前累加，超出预算时切分
  for (let index = conversation.length - 1; index >= 0; index--) {
    const next = total + Token.estimate(conversation[index]);
    if (next > tokens) {
      // 字符级精确切分：只保留最后 remaining 个字符
      const remaining = Math.max(0, tokens - total) * 4;
      splitPrefix = conversation[index].slice(0, -remaining);
      splitSuffix = conversation[index].slice(-remaining);
      split = index + 1;
      break;
    }
    total = next; split = index;
  }
  return { head: ..., recent: ... };
}
```

**关键差异**：OpenCode 使用**字符级切分** —— 当单条消息超出剩余预算时，将消息从中间切开（Pi 不允许消息级切分）。

消息序列化（`serialize`, L86-111）：
- User 消息：`[User]: text` + `[Attached file]`
- Assistant 消息：`[Assistant]: text` + `[Assistant tool call]: name(args)` + `[Tool result]: truncated`
- Tool 输出截断至 `TOOL_OUTPUT_MAX_CHARS = 2000`
- System 消息：`[System update]: text`
- Shell 消息：`[Shell]: command\n{truncated output}`

#### 2.1.2 Anchored Summary（增量更新）

```typescript
// compaction.ts:161-168 — buildPrompt：锚定摘要模式
export const buildPrompt = (input) => [
  input.previousSummary
    ? `Update the anchored summary below using the conversation history above.
Preserve still-true details, remove stale details, and merge in the new facts.
<previous-summary>\n${input.previousSummary}\n</previous-summary>`
    : "Create a new anchored summary from the conversation history.",
  SUMMARY_TEMPLATE,
  ...input.context,
].join("\n\n");
```

**Anchored Summary** 的设计理念：
- 有 previousSummary → 更新（preserve + merge + remove stale）
- 无 previousSummary → 新建
- 对比 Pi：Pi 的 UPDATE_SUMMARIZATION_PROMPT 更显式地规定了"PRESERVE/ADD/UPDATE/REMOVE"规则；OpenCode 用自然语言描述

#### 2.1.3 结构化摘要模板

```typescript
// compaction.ts:16-46 — SUMMARY_TEMPLATE
const SUMMARY_TEMPLATE = `Output exactly the Markdown structure shown inside <template>:
<template>
## Objective
- [one or two brief sentences]

## Important Details
- [constraints/preferences, decisions and why, important facts/assumptions, exact context]

## Work State
### Completed
- [finished work, verified facts, or "(none)"]
### Active
- [current work, partial changes, or "(none)"]
### Blocked
- [blockers, failing commands, or "(none)"]

## Next Move
1. [immediate concrete action, or "(none)"]
2. [next action if known, or "(none)"]

## Relevant Files
- [file or directory path: why it matters, or "(none)"]
</template>

Rules:
- Keep every section, even when empty.
- Use terse bullets, not prose paragraphs.
- Preserve exact file paths, symbols, commands, error strings, URLs, and identifiers.
- Do not mention the summary process or that context was compacted.`
```

**与 Pi 的模板对比**：
| Pi | OpenCode |
|---|---|
| 6 sections（Goal/Constraints/Progress/KeyDecisions/NextSteps/CriticalContext） | 5 sections（Objective/ImportantDetails/WorkState/NextMove/RelevantFiles） |
| Progress 分 Done/InProgress/Blocked | Work State 分 Completed/Active/Blocked |
| Key Decisions 单独一节 | Decisions 合并到 Important Details |
| Critical Context 单独一节 | 合并到 Important Details |
| 文件路径在末尾 XML 标签 | 文件路径作为独立 section |

#### 2.1.4 触发条件

```typescript
// compaction.ts:225-236 — compactIfNeeded
const compactIfNeeded = function* (input) {
  if (!config.auto) return false;
  const context = input.model.route.defaults.limits?.context;
  if (context === undefined || context <= 0) return false;
  const output = input.request.generation?.maxTokens ?? input.model.route.defaults.limits?.output ?? 0;
  if (estimate(input.request) <= context - Math.max(output, config.buffer))
    return false;  // ← 不触发：当前请求 + 最大输出 + buffer < context
  return yield* compactAfterOverflow(input);
};
```

**关键常量**（L12-16）：
```typescript
const DEFAULT_BUFFER = 20_000     // context 缓冲区
const DEFAULT_KEEP_TOKENS = 8_000 // 压缩后保留的近期 token
const TOOL_OUTPUT_MAX_CHARS = 2_000
const SUMMARY_OUTPUT_TOKENS = 4_096
```

触发条件比 Pi 更保守：除了 `context - reserveTokens` 的阈值外，还要检查 summary prompt 本身是否过大（`Token.estimate(summaryPrompt) > context - summaryOutput`）。

### 2.2 消息存储格式

**文件**: `packages/schema/src/session-message.ts` (213行)

消息类型体系（L200-212）：
```
AgentSwitched | ModelSwitched | User | Synthetic | System | Shell 
| Assistant | Compaction
```

`Compaction` 消息（L191-198）：
```typescript
{
  type: "compaction",
  reason: "auto" | "manual",
  summary: string,   // 摘要文本
  recent: string,    // 保留的近期消息（序列化后的文本）
  id, metadata, time: { created }
}
```

**关键差异**：与 Pi 不同，OpenCode 的 Compaction 消息中 `recent` 是**序列化后的文本**（序列化时通过 `serialize()` 转为 `[User]:`/`[Assistant]:` 格式），而非原始 AgentMessage 对象。压缩后的上下文变成纯文本，丢失了原始消息结构。

### 2.3 Context Epoch 机制

**文件**: `packages/core/src/session/context-epoch.ts` (174行)

OpenCode 引入了一个独立于消息链的**上下文快照层**：

```typescript
// context-epoch.ts:23-78 — initialize/prepare 逻辑
// 每个 session 有一个 ContextEpoch 行，存储：
// - baseline: 当前 system prompt 的文本快照
// - snapshot: SystemContext 的快照（结构化）
// - baseline_seq: 快照时的 latest sequence
```

**三种状态变更**：
1. **Unchanged / ReplacementBlocked**：使用已有 baseline（无变化）
2. **ReplacementReady**：直接替换（如 compaction 之后较大变更）
3. **需要 reconcile**：生成 `ContextUpdated` 事件，产生新的 System 消息（增量更新）

这个设计回答了"system prompt 变更（新文件、新配置）时，如何在不重读全部历史的情况下更新上下文"。

### 2.4 OpenCode 上下文工程总结

| 维度 | OpenCode 的设计 |
|------|----------------|
| **信息选择** | 从后往前 token 累加；字符级精确切分（消息可从中间切开）；filter 掉已有 compaction |
| **记忆分层** | Compaction 消息嵌入消息链；Summary + Recent（序列化文本）; ContextEpoch 独立快照层 |
| **结构化记忆** | 5-section 模板（Objective/ImportantDetails/WorkState/NextMove/RelevantFiles）；每个 section 即使为空也保留 |
| **重要性评判** | 保留最近的（DEFAULT_KEEP_TOKENS=8000）；自动触发（buffer=20000）; 任何信息都可被压缩 |
| **压缩策略** | Anchored Summary 增量更新；字符级切分；先检查 summary prompt 是否过大再决定是否压缩 |
| **持久记忆桥梁** | ContextEpoch 存储 baseline/snapshot；System 消息可注入更新 |

---

## 三、Claude Code（基于社区分析）

> 来源：[Dive into Claude Code](https://github.com/VILA-Lab/Dive-into-Claude-Code)、[WaveSpeed 架构分析](https://wavespeed.ai/blog/posts/claude-code-agent-harness-architecture)、[MindStudio 内存架构](https://www.mindstudio.ai/blog/claude-code-source-leak-memory-architecture)、[Harness Design for Long-Running Apps](https://anthropic.com/engineering/harness-design-long-running-apps)

### 3.1 三层记忆架构

这是 Claude Code 最独特的设计 —— 不依赖向量数据库，而是使用**纯文件系统**实现分层记忆：

#### Layer 1: In-Context Memory（工作记忆）
- 当前 context window 中的一切：对话历史、tool 输出、正在编辑的文件
- 快速但易失 —— 压缩或 session 结束即丢失
- 类似 Pi 的 `retainedTail` + OpenCode 的 `recent`

#### Layer 2: External File Memory（`memory.md` 指针索引）
- **`memory.md`** 不存储实际信息，只存储**指针**（对其他 memory 文件的引用）
- 域名文件：`memory/project-context.md`、`memory/decisions.md`、`memory/code-patterns.md`、`memory/user-preferences.md`
- **LLM-based 检索**：扫描 memory 文件头部，选择最多 **5 个**最相关的文件加载
- **自愈（Self-healing）**：agent 自动更新 memory 文件（Read-before-write），无需人工维护
- **关键设计理念**：指针索引保持小而快，实际内容可以任意增长

#### Layer 3: Structural Project Memory（`CLAUDE.md` 层次结构）
```
/etc/claude/CLAUDE.md          ← Managed（组织级别，管理员控制）
~/.claude/CLAUDE.md            ← User（全局偏好）
<project>/CLAUDE.md            ← Project（项目级指令，版本控制）
<project>/.claude/rules/       ← Project Rules（按目录/文件匹配的条件规则）
<project>/CLAUDE.local.md      ← Local（gitignored，个人本地覆盖）
```

**CLAUDE.md 的"锚点"作用**：
- **每 turn 重读** —— 阻止关键规则被压缩丢失
- 以 **user context** 传递（非 system prompt），意味着 model 有概率性遵循而非确定性执行
- 社区经验：将关键指令放在 CLAUDE.md 是防止 compaction 丢失信息的**唯一可靠方式**

### 3.2 5 层压缩管线

在**每次 model 调用前**，按从便宜到贵的顺序执行：

```
Budget Reduction → Snip → Microcompact → Context Collapse → Auto-Compact
(最便宜)                                                      (最贵，最后手段)
```

#### Layer 1: Budget Reduction
- 减小 `maxTokens` 输出预算
- 零 LLM 调用，纯参数调整

#### Layer 2: Snip
- 截断过大的 tool 输出
- 零 LLM 调用

#### Layer 3: Microcompact（缓存感知）
- **利用 Anthropic API 的 prompt caching**：直接丢弃旧消息，依赖缓存重建
- 缓存命中时成本接近零（cache reads 按原价 10% 计费，cache writes 按 125%）
- 但缓存 TTL 仅 5 分钟 —— 这个约束驱动了整个压缩策略的时间窗口设计

#### Layer 4: Context Collapse
- **读时投影（Read-time projection）**，非破坏性编辑
- 合并冗余消息、简化 tool 输出
- 不修改磁盘数据，仅在构建 context 时应用

#### Layer 5: Auto-Compact（最后手段）
- 完整 LLM summary，类似 Pi 和 OpenCode 的方案
- 但首选 **Session Memory** 路径（后台笔记，零 LLM 调用）
- 仅在前 4 层都失败时触发

**触发时机**：token 使用达到 context window 的 ~98% 时

**"非破坏性 patch"机制**（很重要）：
- 压缩边界记录 `headUuid`/`anchorUuid`/`tailUuid`
- session 加载器在**读时**修补消息链
- 磁盘上的数据**不会被破坏性编辑**

### 3.3 Session Memory（后台笔记）

这是一个独立的轻量级记忆通道：

- **零 LLM 调用**的 compact 方式
- 后台自动记录关键决策、文件变更、错误信息
- 优先于 Auto-Compact（完整的 LLM summary）使用
- 类似 Pi 的 `CompactionDetails`（文件追踪），但更主动

### 3.4 Claude Code 上下文工程总结

| 维度 | Claude Code 的设计 |
|------|-------------------|
| **信息选择** | LLM 扫描 memory 文件头部选择最多 5 个文件加载；5 层压缩管线按成本递增排序 |
| **记忆分层** | 三层：In-Context / File Memory (memory.md) / Structural Config (CLAUDE.md)；Session Memory 独立通道 |
| **结构化记忆** | 文件系统即数据库；`memory.md` 作指针索引；CLAUDE.md 作持久锚点；无向量数据库 |
| **重要性评判** | CLAUDE.md 的内容永不压缩（每 turn 重读）；5 层压缩从便宜到贵依次尝试；缓存感知的 Microcompact 优先 |
| **压缩策略** | Budget Reduction → Snip → Microcompact → Context Collapse → Auto-Compact；非破坏性读时投影；缓存 TTL 5分钟驱动策略 |
| **持久记忆桥梁** | CLAUDE.md 层次结构（Managed→User→Project→Local）；memory.md 指针系统；append-only JSONL transcripts |

---

## 四、跨项目对比矩阵

### 4.1 压缩触发条件

| 项目 | 触发条件 | 阈值常量 |
|------|---------|---------|
| **Pi** | `contextTokens > contextWindow - reserveTokens` | reserveTokens=16384, keepRecentTokens=20000 |
| **OpenCode** | `estimate(request) > context - max(output, buffer)` + 检查 summary prompt 是否过大 | buffer=20000, keep=8000, summaryOutput=4096 |
| **Claude Code** | 达到 context window ~98% 时，5 层管线依次尝试 | 各层独立阈值 |

### 4.2 摘要模板结构

| 模板字段 | Pi | OpenCode | Claude Code |
|---------|-----|---------|-------------|
| 目标/任务 | Goal | Objective | Objective (推断) |
| 约束/偏好 | Constraints & Preferences | Important Details (合并) | Important Details (合并) |
| 进度 | Progress (Done/InProgress/Blocked) | Work State (Completed/Active/Blocked) | 各层不同 |
| 决策 | Key Decisions (单独) | Important Details (合并) | 各层不同 |
| 下一步 | Next Steps | Next Move | 各层不同 |
| 关键上下文 | Critical Context (单独) | Important Details (合并) | 各层不同 |
| 文件 | `<read-files>` `<modified-files>` XML 标签 | Relevant Files (section) | 各层不同 |

### 4.3 关键设计决策对比

| 设计维度 | Pi | OpenCode | Claude Code |
|---------|-----|---------|-------------|
| **消息切分粒度** | Turn 级（不允许在 toolResult 处切） | 字符级（可从消息中间切开） | 多层级（预算→截断→缓存→投影→LLM） |
| **增量更新** | ✅ UPDATE_SUMMARIZATION_PROMPT（显式规则） | ✅ Anchored Summary（自然语言描述） | ✅ Session Memory（零 LLM 方案优先） |
| **持久记忆** | CompactionDetails (文件列表) + Skills (懒加载) | ContextEpoch (baseline/snapshot) | memory.md 指针系统 + CLAUDE.md 层次 |
| **缓存利用** | ❌ 无 | ❌ 无 | ✅ Microcompact 利用 API 缓存 |
| **非破坏性** | CompactionEntry 嵌入消息链（保留 original） | Compaction 消息嵌入链 + Recent 序列化文本 | 读时 chain patching（磁盘不修改） |
| **外部记忆检索** | Skills XML 索引（模型 read） | 无 | LLM 扫描 memory 文件头（最多 5 个） |

---

## 五、对 Jeeves 的启示

### 5.1 可采用的设计模式

1. **Pi 的 6-section 结构化模板**：适合作为 Jeeves 压缩摘要的基线格式；Progress 的 Done/InProgress/Blocked 三态模型特别适合任务跟踪场景。

2. **OpenCode 的 Anchored Summary**：增量更新 + "preserve still-true, remove stale, merge new" 的理念是处理长对话的正确方式。

3. **Claude Code 的文件系统记忆**：`memory.md` 指针索引 + 域名文件 的模式不依赖任何外部基础设施，最适合本地优先的 agent 系统。

4. **Pi 的切点原子性保护**：禁止在 toolResult 处切分的设计避免了压缩后的上下文包含无来源的 tool 输出 —— 这是容易被忽视但至关重要的一点。

5. **Claude Code 的"锚点"概念**：让关键规则（如 Jeeves 的项目配置、用户偏好）在每次 turn 时重读，防止被压缩丢失。可类比 Pi 的 Skills 懒加载机制。

### 5.2 应避免的设计

1. **OpenCode 的字符级切分**：从消息中间切开会破坏语义完整性，仅在极端场景可用。
2. **纯自由文本压缩**：没有结构化模板的摘要容易丢失关键信息（文件路径、错误消息、决策理由）。
3. **单层压缩**：Pi/OpenCode 只有一种压缩方式，没有 Claude Code 的"从便宜到贵"分级策略。
4. **忽略缓存**：如果 Jeeves 使用的 LLM provider 支持 prompt caching，应考虑缓存感知的压缩策略。

### 5.3 推荐架构

```
Jeeves 上下文工程 = 
  Pi 的切点算法（工具原子性保护）
+ OpenCode 的 Anchored Summary 增量更新
+ Pi 的 6-section 结构化模板
+ Claude Code 的分层压缩策略（预算→截断→LLM summary）
+ Claude Code 的 CLAUDE.md 锚点模式（关键配置每次重读）
+ Pi 的 CompactionDetails 文件追踪
+ 文件系统记忆（memory.md 指针 + 域名文件，不依赖向量数据库）
```

---

## 附录：已读取的源文件清单

### Pi (earendil-works/pi)
| 文件 | 行数 | 角色 |
|------|------|------|
| `packages/agent/src/harness/compaction/compaction.ts` | 848 | 压缩算法核心 |
| `packages/agent/src/harness/compaction/utils.ts` | 132 | 序列化/文件追踪工具 |
| `packages/agent/src/harness/session/types.ts` | 393 | Session 持久化类型定义 |
| `packages/agent/src/harness/session/state.ts` | 344 | Session 内存状态管理 |
| `packages/agent/src/harness/session/context.ts` | 100 | 上下文重建 |
| `packages/agent/src/harness/session/memory.ts` | 192 | 内存存储实现 |
| `packages/coding-agent/src/core/skills.ts` | 487 | Skills 加载与 system prompt 注入 |

### OpenCode (anomalyco/opencode)
| 文件 | 行数 | 角色 |
|------|------|------|
| `packages/core/src/session/compaction.ts` | 241 | 核心压缩（3 阶段） |
| `packages/opencode/src/session/compaction.ts` | 601 | V1 压缩（turn-based + prune） |
| `packages/schema/src/session-message.ts` | 213 | 消息 schema（含 Compaction 类型） |
| `packages/core/src/session/context-epoch.ts` | 174 | Context 快照管理 |
| `packages/opencode/src/session/prompt/default.txt` | 95 | Agent system prompt |
| `packages/core/src/agent.ts` | 111 | Agent 定义 |

### Claude Code 社区分析
| 来源 | 角色 |
|------|------|
| [Dive into Claude Code (VILA-Lab)](https://github.com/VILA-Lab/Dive-into-Claude-Code) | 512K 行源码系统分析 |
| [WaveSpeed 架构分析](https://wavespeed.ai/blog/posts/claude-code-agent-harness-architecture) | 架构拆解 |
| [MindStudio 内存架构](https://www.mindstudio.ai/blog/claude-code-source-leak-memory-architecture) | 三层记忆系统 |
