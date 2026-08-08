# Pi (earendil-works/pi) 驾驭工程深度分析

> **项目**: https://github.com/earendil-works/pi | **Stars**: 82K+
> **语言**: TypeScript | **架构**: Monorepo (npm workspaces)
> **核心包**: `packages/agent/` (agent loop, harness) | `packages/coding-agent/` (系统提示词, skills)

---

## 目录

1. [System Prompt 设计](#1-system-prompt-设计)
2. [Agent Loop（双层循环）](#2-agent-loop双层循环)
3. [硬编码安全约束](#3-硬编码安全约束)
4. [Compaction 算法](#4-compaction-算法)
5. [Hooks 系统](#5-hooks-系统)
6. [Skills 机制](#6-skills-机制)
7. [与 Hermes Agent 对比](#7-与-hermes-agent-对比)

---

## 1. System Prompt 设计

### 1.1 架构概览

Pi 有两层 System Prompt 构建：

| 层级 | 文件 | 职责 |
|------|------|------|
| 旧层 (coding-agent) | `packages/coding-agent/src/core/system-prompt.ts` | 默认 coding assistant prompt + context files + skills |
| 新层 (harness) | `packages/agent/src/harness/system-prompt.ts` | 纯 skills XML 注入（新架构用） |

### 1.2 默认 System Prompt 模板

```typescript
// packages/coding-agent/src/core/system-prompt.ts:121-138

let prompt = `You are an expert coding assistant operating inside pi, a coding agent harness. You help users by reading files, executing commands, editing code, and writing new files.

Available tools:
${toolsList}

In addition to the tools above, you may have access to other custom tools depending on the project.

Guidelines:
${guidelines}

Pi documentation (read only when the user asks about pi itself, its SDK, extensions, themes, skills, or TUI):
- Main documentation: ${readmePath}
- Additional docs: ${docsPath}
- Examples: ${examplesPath} (extensions, custom tools, SDK)
- When reading pi docs or examples, resolve docs/... under Additional docs and examples/... under Examples, not the current working directory
...`;
```

**关键设计点**：
- 身份定义为 "coding agent harness"，不是通用助手
- 工具列表动态注入（一行摘要），由 `selectedTools` + `toolSnippets` 控制
- Guidelines 根据可用工具动态构建：`hasBash` 决定是否引导使用 bash 工具

### 1.3 Guidelines 动态构造

```typescript
// packages/coding-agent/src/core/system-prompt.ts:87-119

const guidelinesList: string[] = [];
const guidelinesSet = new Set<string>();       // 去重
const addGuideline = (guideline: string): void => {
  if (guidelinesSet.has(guideline)) return;    // 幂等安全
  guidelinesSet.add(guideline);
  guidelinesList.push(guideline);
};

// 条件注入：没有独立 grep/find/ls 工具时引导用 bash
if (hasBash && !hasGrep && !hasFind && !hasLs) {
  addGuideline("Use bash for file operations like ls, rg, find");
}

// 额外 guidelines 从外部注入
for (const guideline of promptGuidelines ?? []) {
  const normalized = guideline.trim();
  if (normalized.length > 0) addGuideline(normalized);
}

// 始终包含
addGuideline("Be concise in your responses");
addGuideline("Show file paths clearly when working with files");
```

**设计优点**：
- 用 `Set` 去重，防止外部注入的 guideline 与默认重复
- 按工具能力条件注入 guidelines：不是死的文本模板，而是活的"如果有什么工具就给什么建议"

### 1.4 Context Files 注入（AGENTS.md 等）

```typescript
// packages/coding-agent/src/core/system-prompt.ts:144-152

// Append project context files
if (contextFiles.length > 0) {
  prompt += "\n\n<project_context>\n\n";
  prompt += "Project-specific instructions and guidelines:\n\n";
  for (const { path: filePath, content } of contextFiles) {
    prompt += `<project_instructions path="${filePath}">\n${content}\n</project_instructions>\n\n`;
  }
  prompt += "</project_context>\n";
}
```

**设计优点**：
- 使用 `<project_context>` / `<project_instructions>` XML 标签结构隔离
- 每个文件保留 `path` 属性，方便模型知道来源
- 全部展开注入（不是引用），避免额外的 read 工具调用

### 1.5 Skills 注入：XML 格式（只给索引，不给内容）

```typescript
// packages/agent/src/harness/system-prompt.ts:3-25

export function formatSkillsForSystemPrompt(skills: Skill[]): string {
  const visibleSkills = skills.filter((skill) => !skill.disableModelInvocation);
  if (visibleSkills.length === 0) return "";

  const lines = [
    "The following skills provide specialized instructions for specific tasks.",
    "Read the full skill file when the task matches its description.",
    "When a skill file references a relative path, resolve it against the skill directory...",
    "",
    "<available_skills>",
  ];

  for (const skill of visibleSkills) {
    lines.push("  <skill>");
    lines.push(`    <name>${escapeXml(skill.name)}</name>`);
    lines.push(`    <description>${escapeXml(skill.description)}</description>`);
    lines.push(`    <location>${escapeXml(skill.filePath)}</location>`);
    lines.push("  </skill>");
  }

  lines.push("</available_skills>");
  return lines.join("\n");
}
```

**关键区别（vs Hermes）**：
- **Pi**: 只注入了 name + description + filePath，**模型自己用 read 工具读取 skill 内容**
- **Hermes**: 把 skill 的完整 SKILL.md 内容直接注入 system prompt

Pi 的指令明确说 "Read the full skill file when the task matches its description."——这是一个懒惰加载策略。

### 1.6 Skill 内容注入时机

当模型触发显式 skill 调用时，才注入完整内容：

```typescript
// packages/agent/src/harness/skills.ts:38-41

export function formatSkillInvocation(skill: Skill, additionalInstructions?: string): string {
  const skillBlock = `<skill name="${skill.name}" location="${skill.filePath}">\nReferences are relative to ${dirnameEnvPath(skill.filePath)}.\n\n${skill.content}\n</skill>`;
  return additionalInstructions ? `${skillBlock}\n\n${additionalInstructions}` : skillBlock;
}
```

### 设计优点总结

| 优点 | 说明 |
|------|------|
| **XML 结构隔离** | `<available_skills>`, `<project_context>`, `<skill>` 等标签清晰分隔不同 prompt 段 |
| **条件化 guidelines** | 根据实际可用工具动态构建，非死模板 |
| **Skills 懒惰加载** | 只注入索引不注入内容，节省 context window |
| **escapeXml** | 对所有用户内容做 XML 转义，防止注入破坏结构 |
| **disableModelInvocation** | 支持隐藏 skill，只允许显式调用 |

### 对 Jeeves 的启示

1. **动态 guidelines 构建**：根据实际注册的工具列表生成对应的使用提示，而不是写死
2. **Skills 懒惰加载**：对 token 敏感时，只注入 skill 索引而非全部内容，让模型按需 read
3. **XML 结构隔离 prompt 段**：便于解析、替换、调试

---

## 2. Agent Loop（双层循环）

### 2.1 核心架构

Pi 的 agent loop 是 **双层循环**：

```
外层循环 (follow-ups):
  └── 内层循环 (tool calls + steering):
        ├── 1. 注入 pendingMessages（steering 消息）
        ├── 2. 流式获取 assistant 回复
        ├── 3. 如果有 tool calls → 执行工具
        ├── 4. prepareNextTurn（可切换 model/thinkingLevel）
        ├── 5. shouldStopAfterTurn? → YES: 结束
        ├── 6. 轮询 steering 消息 → 回到步骤 1
        └── 无更多 tool calls 且无 steering → 退出内层
  └── 轮询 followUp 消息 → 有: 注入并回到内层 | 无: 真正结束
```

### 2.2 完整源码

```typescript
// packages/agent/src/agent-loop.ts:163-275

async function runLoop(
  initialContext: AgentContext,
  newMessages: AgentMessage[],
  initialConfig: AgentLoopConfig,
  signal: AbortSignal | undefined,
  emit: AgentEventSink,
  streamFunction: StreamFn,
): Promise<void> {
  let currentContext = initialContext;
  let config = initialConfig;
  let firstTurn = true;
  // Check for steering messages at start (user may have typed while waiting)
  let pendingMessages: AgentMessage[] = (await config.getSteeringMessages?.()) || [];

  // Outer loop: continues when queued follow-up messages arrive after agent would stop
  while (true) {
    let hasMoreToolCalls = true;

    // Inner loop: process tool calls and steering messages
    while (hasMoreToolCalls || pendingMessages.length > 0) {
      if (!firstTurn) {
        await emit({ type: "turn_start" });
      } else {
        firstTurn = false;
      }

      // Process pending messages (inject before next assistant response)
      if (pendingMessages.length > 0) {
        for (const message of pendingMessages) {
          await emit({ type: "message_start", message });
          await emit({ type: "message_end", message });
          currentContext.messages.push(message);
          newMessages.push(message);
        }
        pendingMessages = [];
      }

      // Stream assistant response
      const message = await streamAssistantResponse(currentContext, config, signal, emit, streamFunction);
      newMessages.push(message);

      // 硬编码终止检查
      if (message.stopReason === "error" || message.stopReason === "aborted") {
        await emit({ type: "turn_end", message, toolResults: [] });
        await emit({ type: "agent_end", messages: newMessages });
        return;
      }

      // Check for tool calls
      const toolCalls = message.content.filter((c) => c.type === "toolCall");
      const toolResults: ToolResultMessage[] = [];
      hasMoreToolCalls = false;
      if (toolCalls.length > 0) {
        // 硬编码保护: stopReason === "length" 时拒绝执行所有工具调用
        const executedToolBatch =
          message.stopReason === "length"
            ? await failToolCallsFromTruncatedMessage(toolCalls, emit)
            : await executeToolCalls(currentContext, message, config, signal, emit);
        toolResults.push(...executedToolBatch.messages);
        hasMoreToolCalls = !executedToolBatch.terminate;

        for (const result of toolResults) {
          currentContext.messages.push(result);
          newMessages.push(result);
        }
      }

      await emit({ type: "turn_end", message, toolResults });

      // prepareNextTurn: 可在下一轮切换 model/thinkingLevel
      const nextTurnSnapshot = await config.prepareNextTurn?.({ message, toolResults, context: currentContext, newMessages });
      if (nextTurnSnapshot) {
        currentContext = nextTurnSnapshot.context ?? currentContext;
        config = { ...config, model: nextTurnSnapshot.model ?? config.model, reasoning: ... };
      }

      // shouldStopAfterTurn: 提前终止
      if (await config.shouldStopAfterTurn?.({ message, toolResults, context: currentContext, newMessages })) {
        await emit({ type: "agent_end", messages: newMessages });
        return;
      }

      // 每次内层循环结束都检查 steering
      pendingMessages = (await config.getSteeringMessages?.()) || [];
    }

    // Agent would stop here. Check for follow-up messages.
    const followUpMessages = (await config.getFollowUpMessages?.()) || [];
    if (followUpMessages.length > 0) {
      pendingMessages = followUpMessages;
      continue;   // 回到外层循环 → 重新进入内层
    }

    break;   // 真正结束
  }

  await emit({ type: "agent_end", messages: newMessages });
}
```

### 2.3 Steering vs Follow-Up 语义

| 类型 | 注入时机 | 语义 |
|------|---------|------|
| `steering` | **每次内层循环结束**后立即注入 | "转向指令"——在 agent 工作中途插入新指令 |
| `followUp` | 仅当 agent **自己决定停止**后才注入 | "后续任务"——等 agent 完成当前工作再给新任务 |

这是一个精妙的设计：steering 让外部可以在 agent 工作期间纠正方向；followUp 确保积压任务不会干扰当前工作流。

### 2.4 Queue Drain Mode

```typescript
// packages/agent/src/agent.ts:125-159

class PendingMessageQueue {
  private messages: AgentMessage[] = [];
  public mode: QueueMode;

  drain(): AgentMessage[] {
    if (this.mode === "all") {
      const drained = this.messages.slice();
      this.messages = [];
      return drained;
    }
    // "one-at-a-time": 每次只取一条
    const first = this.messages[0];
    if (!first) return [];
    this.messages = this.messages.slice(1);
    return [first];
  }
}
```

**QueueMode**: `"all"` 一次性清空队列 | `"one-at-a-time"` 每次只取一条。默认 `one-at-a-time`，确保每次注入一条后给模型机会消化。

### 2.5 事件系统

```typescript
// packages/agent/src/types.ts:428-443

export type AgentEvent =
  | { type: "agent_start" }
  | { type: "agent_end"; messages: AgentMessage[] }
  // Turn lifecycle
  | { type: "turn_start" }
  | { type: "turn_end"; message: AgentMessage; toolResults: ToolResultMessage[] }
  // Message lifecycle
  | { type: "message_start"; message: AgentMessage }
  | { type: "message_update"; message: AgentMessage; assistantMessageEvent: AssistantMessageEvent }
  | { type: "message_end"; message: AgentMessage }
  // Tool execution lifecycle
  | { type: "tool_execution_start"; toolCallId: string; toolName: string; args: any }
  | { type: "tool_execution_update"; toolCallId: string; ... }
  | { type: "tool_execution_end"; ... };
```

事件覆盖三个生命周期：Agent → Turn → Message + Tool。每个事件都被 Agent 类的 `processEvents` 方法和外部监听器处理。

### 设计优点总结

| 优点 | 说明 |
|------|------|
| **双层循环分离关注点** | 内层处理工具调用链，外层处理 follow-up |
| **Steering ≠ FollowUp** | 语义化的消息队列区分中途转向 vs 完成后的续接 |
| **事件驱动架构** | 6 类事件覆盖完整生命周期，支持 UI 实时更新 |
| **prepareNextTurn 钩子** | 允许在 turn 间动态切换模型、thinking level |
| **Queue Mode 可配置** | `one-at-a-time` 给模型消化空间，`all` 批量加速 |
| **流式 partial 消息处理** | 在 `streamAssistantResponse` 中通过事件流动态更新 `context.messages` |

### 对 Jeeves 的启示

1. **双层循环**：Jeeves 可借鉴内层(工具链) + 外层(后续任务)的分离设计
2. **Steering vs FollowUp**：区分"途中转向"和"完成后追加"两种消息注入语义
3. **事件驱动的 UI 更新**：每个 turn/message/tool 都有对应的生命周期事件
4. **Queue drain mode**：在自动模式和交互模式间切换队列行为

---

## 3. 硬编码安全约束

### 3.1 Token 截断保护

```typescript
// packages/agent/src/agent-loop.ts:208-213

if (toolCalls.length > 0) {
  // A "length" stop means the output was cut off by the token limit, so
  // every tool call in the message may carry truncated arguments. Fail
  // them all instead of executing potentially borked calls.
  const executedToolBatch =
    message.stopReason === "length"
      ? await failToolCallsFromTruncatedMessage(toolCalls, emit)
      : await executeToolCalls(currentContext, message, config, signal, emit);
```

当 LLM 返回 `stopReason === "length"`（达到 output token 上限被截断），**拒绝执行所有工具调用**，而不是冒险执行可能参数不完整的调用：

```typescript
// packages/agent/src/agent-loop.ts:381-406

async function failToolCallsFromTruncatedMessage(
  toolCalls: AgentToolCall[],
  emit: AgentEventSink,
): Promise<ExecutedToolCallBatch> {
  const messages: ToolResultMessage[] = [];
  for (const toolCall of toolCalls) {
    const finalized: FinalizedToolCallOutcome = {
      toolCall,
      result: createErrorToolResult(
        `Tool call "${toolCall.name}" was not executed: the response hit the output token limit, so its arguments may be truncated. Re-issue the tool call with complete arguments.`,
      ),
      isError: true,
    };
    messages.push(createToolResultMessage(finalized));
  }
  return { messages, terminate: false };  // terminate=false → 继续循环让模型重试
}
```

**关键点**：`terminate: false`——不终止 agent，而是返回错误让模型重新发起正确的调用。

### 3.2 角色交替硬校验

```typescript
// packages/agent/src/agent-loop.ts:74-77

export function agentLoopContinue(...) {
  // ...
  if (context.messages[context.messages.length - 1].role === "assistant") {
    throw new Error("Cannot continue from message role: assistant");
  }
}
```

```typescript
// packages/agent/src/agent-loop.ts:131-133

export async function runAgentLoopContinue(...) {
  if (context.messages[context.messages.length - 1].role === "assistant") {
    throw new Error("Cannot continue from message role: assistant");
  }
}
```

**两处校验**：进入 continue 模式前硬性检查最后一条消息必须是 user 或 toolResult，防止违反 LLM API 的 user/assistant 交替规则。

### 3.3 beforeToolCall: Block + Terminate 机制

```typescript
// packages/agent/src/agent-loop.ts:600-647

async function prepareToolCall(...) {
  const tool = currentContext.tools?.find((t) => t.name === toolCall.name);
  if (!tool) {
    return {
      kind: "immediate",
      result: createErrorToolResult(`Tool ${toolCall.name} not found`),
      isError: true,
    };
  }

  // 参数验证
  const validatedArgs = validateToolArguments(tool, preparedToolCall);

  if (config.beforeToolCall) {
    const beforeResult = await config.beforeToolCall({...}, signal);
    if (signal?.aborted) {
      return { kind: "immediate", result: createErrorToolResult("Operation aborted"), isError: true };
    }
    if (beforeResult?.block) {
      const result = createErrorToolResult(beforeResult.reason || "Tool execution was blocked");
      if (beforeResult.terminate === true) {
        result.terminate = true;   // 传递 terminate 标志
      }
      return { kind: "immediate", result, isError: true };
    }
  }

  return { kind: "prepared", toolCall, tool, args: validatedArgs };
}
```

```typescript
// packages/agent/src/types.ts:61-69

export interface BeforeToolCallResult {
  block?: boolean;        // 阻止执行
  reason?: string;        // 阻止原因（作为错误消息返回给模型）
  terminate?: boolean;    // 阻止后是否终止整个工具批次
}
```

### 3.4 批次终止规则：全票通过

```typescript
// packages/agent/src/agent-loop.ts:582-584

function shouldTerminateToolBatch(finalizedCalls: FinalizedToolCallOutcome[]): boolean {
  return finalizedCalls.length > 0 && finalizedCalls.every((finalized) => finalized.result.terminate === true);
}
```

**语义**：只有**所有**工具结果都设置了 `terminate === true` 时才终止 agent loop。单个工具的 terminate 不会影响其他并行工具。

### 3.5 afterToolCall 错误安全

```typescript
// packages/agent/src/agent-loop.ts:724-750

if (config.afterToolCall) {
  try {
    const afterResult = await config.afterToolCall({...}, signal);
    if (afterResult) {
      result = {
        ...result,
        content: afterResult.content ?? result.content,
        details: afterResult.details ?? result.details,
        usage: afterResult.usage ?? result.usage,
        terminate: afterResult.terminate ?? result.terminate,
      };
      isError = afterResult.isError ?? isError;
    }
  } catch (error) {
    // 如果 afterToolCall 抛出异常，兜底为 error tool result
    result = createErrorToolResult(error instanceof Error ? error.message : String(error));
    isError = true;
  }
}
```

**关键**：afterToolCall 的所有异常都被捕获并转换为 error tool result，**绝不会**因为 hook 异常导致 agent loop 崩溃。

### 3.6 convertToLlm 默认过滤

```typescript
// packages/agent/src/agent.ts:33-37

function defaultConvertToLlm(messages: AgentMessage[]): Message[] {
  return messages.filter(
    (message) => message.role === "user" || message.role === "assistant" || message.role === "toolResult",
  );
}
```

默认只传递标准 LLM 角色，自定义消息类型（如 `bashExecution`, `compactionSummary`, `branchSummary`）自动被过滤。

### 设计优点总结

| 保护机制 | 说明 |
|---------|------|
| **length 截断拒绝** | 拒绝执行可能参数不完整的工具调用，返回错误让模型重试 |
| **角色交替硬校验** | 两处检查，防止违反 API 协议 |
| **beforeToolCall block** | 在参数验证后、执行前阻止，可附带原因 |
| **terminate 全票规则** | 所有工具都同意才终止，防止误终止 |
| **afterToolCall 异常兜底** | hook 异常不会崩溃 loop，转为 error result |
| **defaultConvertToLlm 过滤** | 非标准角色自动过滤，不会泄漏到 LLM |

### 对 Jeeves 的启示

1. **stopReason 处理**：Jeeves 应检查 LLM 返回的 finish_reason，length 时拒绝执行工具
2. **Hook 异常兜底**：所有钩子应 try-catch 包裹，异常转为 error tool result 而非崩溃
3. **Terminate 语义**：采用"全票通过"规则比单票否决更安全

---

## 4. Compaction 算法

### 4.1 触发条件

```typescript
// packages/agent/src/harness/compaction/compaction.ts:246-250

export function shouldCompact(contextTokens: number, contextWindow: number, settings: CompactionSettings): boolean {
  if (!settings.enabled) return false;
  return contextTokens > contextWindow - settings.reserveTokens;
}

// 默认阈值
export const DEFAULT_COMPACTION_SETTINGS: CompactionSettings = {
  enabled: true,
  reserveTokens: 16384,     // 预留 16K tokens 给压缩 prompt + 输出
  keepRecentTokens: 20000,  // 保留最近 20K tokens
};
```

**触发公式**：`contextTokens > contextWindow - 16384`

### 4.2 Token 估算：Chars/4 启发式

```typescript
// packages/agent/src/harness/compaction/compaction.ts:271-311

export function estimateTokens(message: AgentMessage): number {
  let chars = 0;
  switch (message.role) {
    case "user":
      chars = estimateTextAndImageContentChars(message.content);
      return Math.ceil(chars / 4);
    case "assistant":
      for (const block of assistant.content) {
        if (block.type === "text") chars += block.text.length;
        else if (block.type === "thinking") chars += block.thinking.length;
        else if (block.type === "toolCall")
          chars += block.name.length + safeJsonStringify(block.arguments).length;
      }
      return Math.ceil(chars / 4);
    case "toolResult":
      chars = estimateTextAndImageContentChars(message.content);
      return Math.ceil(chars / 4);
    // ...
  }
}
```

**双重估算策略**：
- 优先使用 provider 返回的 usage 数据（精确）
- 无 usage 时回退到 `chars/4` 启发式估算

```typescript
// packages/agent/src/harness/compaction/compaction.ts:215-244

export function estimateContextTokens(messages: AgentMessage[]): ContextUsageEstimate {
  const usageInfo = getLastAssistantUsageInfo(messages);

  if (!usageInfo) {
    // 无 usage 数据 → 全部用 chars/4 估算
    let estimated = 0;
    for (const message of messages) estimated += estimateTokens(message);
    return { tokens: estimated, usageTokens: 0, trailingTokens: estimated, lastUsageIndex: null };
  }

  // 有 usage → usage 精确值 + trailing 部分估算
  const usageTokens = calculateContextTokens(usageInfo.usage);
  let trailingTokens = 0;
  for (let i = usageInfo.index + 1; i < messages.length; i++) {
    trailingTokens += estimateTokens(messages[i]);
  }
  return { tokens: usageTokens + trailingTokens, usageTokens, trailingTokens, lastUsageIndex: usageInfo.index };
}
```

### 4.3 切点选择：不切 toolResult

```typescript
// packages/agent/src/harness/compaction/compaction.ts:312-343

function findValidCutPoints(entries: Entry[], startIndex: number, endIndex: number): number[] {
  const cutPoints: number[] = [];
  for (let i = startIndex; i < endIndex; i++) {
    const entry = entries[i];
    switch (entry.type) {
      case "message": {
        const role = entry.message.role;
        switch (role) {
          case "bashExecution":
          case "custom":
          case "branchSummary":
          case "compactionSummary":
          case "user":
          case "assistant":
            cutPoints.push(i);
            break;
          case "toolResult":
            break;   // ← 不在这里切！必须保留 tool call + result 配对
        }
        break;
      }
      // compaction/branch_summary 等控制条目也不作为切点
    }
  }
  return cutPoints;
}
```

**核心规则**：
- ✅ 可以在 user / assistant 消息之间切
- ❌ 不能在 toolResult 消息处切（保证 tool call → result 原子性）
- ❌ 不能在 compaction / branch_summary 等控制条目处切

### 4.4 Split Turn 处理

当切点落在 assistant 消息后的 tool result 序列中（即切在 turn 中间），需要 split turn 处理：

```typescript
// packages/agent/src/harness/compaction/compaction.ts:373-421

export function findCutPoint(
  entries: Entry[],
  startIndex: number,
  endIndex: number,
  keepRecentTokens: number,
): CutPointResult {
  const cutPoints = findValidCutPoints(entries, startIndex, endIndex);

  // 从后往前累加 token，找到 keepRecentTokens 的分界点
  let accumulatedTokens = 0;
  let cutIndex = cutPoints[0];
  for (let i = endIndex - 1; i >= startIndex; i--) {
    const entry = entries[i];
    if (entry.type !== "message") continue;
    const messageTokens = estimateTokens(entry.message as AgentMessage);
    accumulatedTokens += messageTokens;
    if (accumulatedTokens >= keepRecentTokens) {
      // 找到最近的有效切点
      for (let c = 0; c < cutPoints.length; c++) {
        if (cutPoints[c] >= i) { cutIndex = cutPoints[c]; break; }
      }
      break;
    }
  }

  const cutEntry = entries[cutIndex];
  const isUserMessage = cutEntry.type === "message" && cutEntry.message.role === "user";
  const turnStartIndex = isUserMessage ? -1 : findTurnStartIndex(entries, cutIndex, startIndex);

  return {
    firstKeptEntryIndex: cutIndex,
    turnStartIndex,
    isSplitTurn: !isUserMessage && turnStartIndex !== -1,   // ← split turn 标志
  };
}
```

Split turn 发生后，前缀部分单独用 `TURN_PREFIX_SUMMARIZATION_PROMPT` 做摘要，与历史摘要拼接：

```typescript
// packages/agent/src/harness/compaction/compaction.ts:731-765

if (isSplitTurn && turnPrefixMessages.length > 0) {
  // 分别生成历史摘要和 turn 前缀摘要
  const historyResult = await generateSummaryWithUsage(messagesToSummarize, ...);
  const turnPrefixResult = await generateTurnPrefixSummary(turnPrefixMessages, ...);
  summary = `${historyText}\n\n---\n\n**Turn Context (split turn):**\n\n${turnPrefixResult.value.text}`;
}
```

### 4.5 摘要模板：结构化

```typescript
// packages/agent/src/harness/compaction/compaction.ts:428-458

const SUMMARIZATION_PROMPT = `...Use this EXACT format:

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
1. [Ordered list of what should happen next]

## Critical Context
- [Any data, examples, or references needed to continue]

Keep each section concise. Preserve exact file paths, function names, and error messages.`;
```

### 4.6 迭代摘要

当已有 compaction 时，使用 `UPDATE_SUMMARIZATION_PROMPT` 更新而非重新生成：

```typescript
// packages/agent/src/harness/compaction/compaction.ts:461-498

const UPDATE_SUMMARIZATION_PROMPT = `...Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context from the new messages
- UPDATE the Progress section: move items from "In Progress" to "Done" when completed
- UPDATE "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, and error messages
- If something is no longer relevant, you may remove it`;
```

### 4.7 文件操作追踪

```typescript
// packages/agent/src/harness/compaction/compaction.ts:30-34

export interface CompactionDetails {
  readFiles: string[];
  modifiedFiles: string[];
}
```

每次 compaction 记录被压缩历史中读/改的文件列表，后续 compaction 会继承这些信息，追加到摘要末尾：

```typescript
// packages/agent/src/harness/compaction/compaction.ts:784-785

const { readFiles, modifiedFiles } = computeFileLists(fileOps);
summary += formatFileOperations(readFiles, modifiedFiles);
```

### 设计优点总结

| 优点 | 说明 |
|------|------|
| **双重估算策略** | provider usage > chars/4 启发式，精确优先 |
| **不切 toolResult** | 保证 tool call → result 原子性，不会出现孤儿 result |
| **Split Turn** | 切在 turn 中间时，前缀独立摘要 + 历史摘要拼接 |
| **结构化模板** | 6 个固定章节（Goal/Progress/Decisions/Next Steps...），非自由文本 |
| **迭代摘要** | 增量更新而非全量重新生成，节省 token |
| **文件追踪** | 记录并继承 readFiles/modifiedFiles |

### 对 Jeeves 的启示

1. **双重估算**：优先用 provider usage，回退到 chars/N 估算
2. **切点不能切 toolResult**：这是最容易被忽视但最关键的设计约束
3. **Split Turn 处理**：不应简单放弃切点，而应单独摘要前缀
4. **结构化摘要模板**：强制模型按固定格式输出，比自由文本更可靠
5. **迭代摘要**：增量更新比全量重新生成更节省 token 和保证一致性

---

## 5. Hooks 系统

### 5.1 架构分层

Pi 的 hooks 系统分为三个层次：

| 层次 | 文件 | 钩子 |
|------|------|------|
| **AgentLoopConfig** | `types.ts` | beforeToolCall, afterToolCall, shouldStopAfterTurn, prepareNextTurn, transformContext, getSteeringMessages, getFollowUpMessages, convertToLlm, getApiKey |
| **Agent 类** | `agent.ts` | 同上 + AgentState 管理 + Queue 系统 |
| **AgentHarness** | `agent-harness.ts` | 命名 Hook 系统（17 个 hook 点） + Session 管理 |

### 5.2 AgentLoopConfig Hook 接口

```typescript
// packages/agent/src/types.ts:149-293

export interface AgentLoopConfig extends SimpleStreamOptions {
  model: Model<any>;

  /** AgentMessage[] → LLM Message[] */
  convertToLlm: (messages: AgentMessage[]) => Message[] | Promise<Message[]>;

  /** Context 变换（compaction 等） */
  transformContext?: (messages: AgentMessage[], signal?: AbortSignal) => Promise<AgentMessage[]>;

  /** 动态 API key（短期 token） */
  getApiKey?: (provider: string) => Promise<string | undefined> | string | undefined;

  /** 每 turn 完成后检查是否应停止 */
  shouldStopAfterTurn?: (context: ShouldStopAfterTurnContext) => boolean | Promise<boolean>;

  /** 下一 turn 前准备（切换 model/thinking） */
  prepareNextTurn?: (context: PrepareNextTurnContext) => AgentLoopTurnUpdate | undefined | Promise<...>;

  /** Steering 消息（途中转向） */
  getSteeringMessages?: () => Promise<AgentMessage[]>;

  /** Follow-up 消息（agent 停止后追加） */
  getFollowUpMessages?: () => Promise<AgentMessage[]>;

  /** 工具执行模式 */
  toolExecution?: ToolExecutionMode;

  /** 工具调用前 */
  beforeToolCall?: (context: BeforeToolCallContext, signal?: AbortSignal) => Promise<BeforeToolCallResult | undefined>;

  /** 工具调用后 */
  afterToolCall?: (context: AfterToolCallContext, signal?: AbortSignal) => Promise<AfterToolCallResult | undefined>;
}
```

### 5.3 Agent 类的 Hook 桥接

```typescript
// packages/agent/src/agent.ts:445-484

private createLoopConfig(options: { skipInitialSteeringPoll?: boolean } = {}): AgentLoopConfig {
  let skipInitialSteeringPoll = options.skipInitialSteeringPoll === true;
  const shouldStopAfterTurn = this.shouldStopAfterTurn;
  return {
    // ...基本配置...
    beforeToolCall: this.beforeToolCall,
    afterToolCall: this.afterToolCall,
    shouldStopAfterTurn: shouldStopAfterTurn
      ? async (context) => await shouldStopAfterTurn(context, this.signal)
      : undefined,
    prepareNextTurn:
      this.prepareNextTurnWithContext || this.prepareNextTurn
        ? async (context) => {
            if (this.prepareNextTurnWithContext) {
              return await this.prepareNextTurnWithContext(context, this.signal);
            }
            return await this.prepareNextTurn?.(this.signal);
          }
        : undefined,
    getSteeringMessages: async () => {
      if (skipInitialSteeringPoll) {
        skipInitialSteeringPoll = false;
        return [];    // 首次跳过 steering poll，避免重复
      }
      return this.steeringQueue.drain();
    },
    getFollowUpMessages: async () => this.followUpQueue.drain(),
  };
}
```

**关键设计**：`skipInitialSteeringPoll` 防止 agent.continue() 时从 steering queue 重复取出刚被消费的消息。

### 5.4 AgentHarness 级 Hook 系统

```typescript
// packages/agent/src/harness/agent-harness.ts:198-213

export type HookName =
  | "before_run" | "before_run_end"
  | "transform_context" | "before_request" | "before_payload"
  | "after_response" | "before_tool" | "after_tool"
  | "before_compaction" | "before_navigation";

export interface Hooks {
  on(name: HookName, handler: (event: unknown) => unknown | Promise<unknown>, options?: { id?: string }): () => void;
}
```

17 个命名的 hook 点覆盖 Session 级别的操作生命周期，比 Agent 类的 loop 级 hook 更高层。

### 设计优点总结

| 优点 | 说明 |
|------|------|
| **分层设计** | Config 层(loop)、Agent 层(state)、Harness 层(session) 三层 hooks |
| **所有 hook 都是 async** | 支持异步操作，不阻塞事件循环 |
| **signal 传递** | 所有 hook 都接收 AbortSignal，支持取消 |
| **Hook 异常兜底** | before/after tool hook 异常被捕获为 error result |
| **prepareNextTurn 动态切换** | 可在 turn 间切换 model、thinking level、context |
| **skipInitialSteeringPoll** | 防止 continue 时重复消费队列消息 |

### 对 Jeeves 的启示

1. **分层 hooks**：loop 级（工具生命周期）+ session 级（操作生命周期）分离
2. **Hook 必须接收 signal**：所有长操作 hook 都应支持取消
3. **动态 model 切换**：prepareNextTurn 的 model 切换提供了"渐进式降级"的可能性

---

## 6. Skills 机制

### 6.1 Skill 定义

```typescript
// packages/agent/src/harness/types.ts:46-57

export interface Skill {
  name: string;                    // 稳定唯一标识
  description: string;             // 简短描述（注入 system prompt）
  content: string;                 // 完整指令（按需加载）
  filePath: string;                // 绝对路径（模型 read 用）
  disableModelInvocation?: boolean; // 仅允许显式调用
}
```

### 6.2 SKILL.md 格式

```yaml
---
name: my-skill                    # 可选，默认用父目录名
description: Does something        # 必需
disable-model-invocation: false    # 可选
---
# Content
具体指令内容...
```

```typescript
// packages/agent/src/harness/skills.ts:312-326

function parseFrontmatter<T>(content: string): Result<{ frontmatter: T; body: string }, Error> {
  const normalized = content.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  if (!normalized.startsWith("---")) return { ok: true, value: { frontmatter: {} as T, body: normalized } };
  const endIndex = normalized.indexOf("\n---", 3);
  if (endIndex === -1) return { ok: true, value: { frontmatter: {} as T, body: normalized } };
  const yamlString = normalized.slice(4, endIndex);
  const body = normalized.slice(endIndex + 4).trim();
  return { ok: true, value: { frontmatter: (parse(yamlString) ?? {}) as T, body } };
}
```

### 6.3 Skills 加载策略

```typescript
// packages/coding-agent/src/core/skills.ts (旧层)

export function loadSkills(options: LoadSkillsOptions): LoadSkillsResult {
  if (includeDefaults) {
    // 用户级: ~/.pi/skills/
    addSkills(loadSkillsFromDirInternal(join(resolvedAgentDir, "skills"), "user", true));
    // 项目级: <cwd>/.pi/skills/
    addSkills(loadSkillsFromDirInternal(resolve(resolvedCwd, CONFIG_DIR_NAME, "skills"), "project", true));
  }
  // 显式路径
  for (const rawPath of skillPaths) { ... }

  // 去重：按 name 去重，第一个赢；按 realPath（解析 symlink）去重
  ...
}
```

```typescript
// packages/agent/src/harness/skills.ts (新层)

export async function loadSkills(env: ExecutionEnv, dirs: string | string[]): Promise<{ skills: Skill[]; diagnostics: SkillDiagnostic[] }> {
  // 遍历目录 → 找到 SKILL.md → parse frontmatter → validate → 返回 Skill
  // 支持 ignore 文件: .gitignore, .ignore, .fdignore
}
```

**加载规则**：
1. 目录包含 `SKILL.md` → 作为 skill 根，不继续递归
2. 否则加载根目录直接 `.md` 文件 + 递归子目录
3. 忽略规则：`.gitignore`, `.ignore`, `.fdignore`
4. 跳过 `.` 开头目录和 `node_modules`

### 6.4 Validation 约束

```typescript
// packages/agent/src/harness/skills.ts:290-300

function validateName(name: string, parentDirName: string): string[] {
  const errors: string[] = [];
  if (name !== parentDirName) errors.push(`name "${name}" does not match parent directory "${parentDirName}"`);
  if (name.length > MAX_NAME_LENGTH) errors.push(`name exceeds ${MAX_NAME_LENGTH} characters`);
  if (!/^[a-z0-9-]+$/.test(name)) errors.push("name contains invalid characters");
  if (name.startsWith("-") || name.endsWith("-")) errors.push("name must not start or end with a hyphen");
  if (name.includes("--")) errors.push("name must not contain consecutive hyphens");
  return errors;
}
```

- name: 小写字母、数字、连字符 only；不超过 64 字符；必须匹配父目录名
- description: 不超过 1024 字符；不能为空
- 验证失败时**仍然加载**，只产生 warning diagnostic

### 6.5 System Prompt 注入 vs 按需加载

| 阶段 | 注入内容 | 位置 |
|------|---------|------|
| System Prompt 构建 | name + description + filePath (XML) | `formatSkillsForSystemPrompt()` |
| 模型决定调用 | 模型用 read 工具读取 `<location>` 指向的文件 | — |
| 显式调用 (`/skill:name`) | 完整 content 通过 `formatSkillInvocation()` 注入 | 用户消息中 |

### 设计优点总结

| 优点 | 说明 |
|------|------|
| **懒惰加载** | System prompt 只注入索引，模型按需读取 skill 内容，节省 tokens |
| **XML 标准化** | 遵循 agentskills.io 标准格式 |
| **ignore 文件支持** | `.gitignore`/`.ignore`/`.fdignore` 多层忽略 |
| **Validation 非阻塞** | 验证失败产生 warning 而非中止加载 |
| **disableModelInvocation** | 隐藏式 skill，只能显式调用 |
| **Source-tagged** | `loadSourcedSkills` 支持带来源标记的批量加载 |

### 对 Jeeves 的启示

1. **Skills 懒惰加载**：对 token 敏感场景，只注入索引而非全部内容
2. **标准格式**：YAML frontmatter + Markdown body，可被多个 agent 框架共用
3. **多层加载策略**：用户级 + 项目级 + 显式路径，优先级明确

---

## 7. 与 Hermes Agent 对比

### 7.1 架构对比

| 维度 | Pi | Hermes Agent |
|------|-----|-------------|
| **语言** | TypeScript (Node.js) | Python |
| **Agent Loop** | 双层循环 (内层 tool calls + 外层 follow-ups) | 单层循环 |
| **消息队列** | Steering + FollowUp 双队列，支持 drain mode | 无内置队列系统 |
| **Compaction** | 结构摘要模板 + split turn + 迭代更新 | 简单摘要（markdown 自由文本） |
| **Skills** | XML 索引注入（懒惰加载） | 完整内容注入（通过 skill_view） |
| **Hooks** | 三层（Config/Agent/Harness），17 个 harness hook 点 | 插件系统 |
| **Session** | 树形 session (lanes) + JSONL 持久化 | 会话级 memory |
| **事件系统** | 完整事件流 (Agent/Turn/Message/Tool) | 无内置事件系统 |
| **类型安全** | 完整 TypeScript 类型系统 + TypeBox schema | Python 类型注解 |

### 7.2 Pi 优于 Hermes 的设计

1. **双层循环**：Steering vs FollowUp 的语义分离，避免了"在工作途中插入后续任务"的问题
2. **Compaction**：结构化摘要模板比自由文本更可靠；split turn 处理避免丢弃正在进行的工作
3. **硬编码保护**：stopReason==="length" 拒绝执行工具调用，Hermes 没有这个检查
4. **Queue drain mode**：`one-at-a-time` vs `all` 的可配置行为
5. **prepareNextTurn**：支持在 turn 之间动态切换模型/thinking level
6. **Session 树**：支持 lane（分支）、导航、挂起/恢复，比 Hermes 的线性对话更强大

### 7.3 Hermes 已有/优于 Pi 的设计

1. **Skills 按需加载**：Hermes 的 `skill_view()` 本质是"按需加载"，只是触发方是 agent（主动加载）而非模型（自己 read）；Hermes 的方式更确定、更可靠
2. **插件系统**：Hermes 有完整的插件体系（providers/tools/skills 分离）；Pi 的 extension 机制相对较新
3. **Managed Desktop**：Hermes 有完整的桌面运行时管理；Pi 是 CLI-first
4. **Profile 分离**：Hermes 的 profile 系统隔离不同场景的配置

### 7.4 对 Jeeves 的最终建议

| 采纳 Pi 的设计 | 保留 Hermes 的做法 |
|----------------|-------------------|
| ✅ 双层循环 (steering vs followUp) | ✅ Skills 主动加载（非模型 read） |
| ✅ 结构化 compaction 模板 | ✅ Profile 隔离 |
| ✅ stopReason==="length" 安全保护 | ✅ 插件系统 |
| ✅ Queue drain mode 可配置 | |
| ✅ 硬编码角色交替校验 | |
| ✅ XML 结构隔离 prompt 段 | |
| ✅ afterToolCall 异常兜底 | |

---

## 附录：关键文件索引

| 文件 | 行数 | 职责 |
|------|------|------|
| `packages/agent/src/agent-loop.ts` | 796 | 核心双层循环 + 工具执行 |
| `packages/agent/src/agent.ts` | 592 | Agent 类：state/hooks/queue |
| `packages/agent/src/types.ts` | 443 | 所有类型定义 |
| `packages/agent/src/harness/compaction/compaction.ts` | 848 | 压缩算法（估算+切点+摘要） |
| `packages/agent/src/harness/system-prompt.ts` | 34 | Skills XML 格式化 |
| `packages/agent/src/harness/skills.ts` | 375 | Skills 加载+解析+验证 |
| `packages/agent/src/harness/agent-harness.ts` | 508 | Harness 层接口+AgentHarness |
| `packages/agent/src/harness/types.ts` | 315 | Harness 层类型定义 |
| `packages/coding-agent/src/core/system-prompt.ts` | 162 | 默认 System Prompt 构建 |
| `packages/coding-agent/src/core/skills.ts` | 487 | 旧层 Skills 加载 |
