# 执行编排工程技术深度调研

> 深入研究 OpenCode、Claude Code、Aider、Goose 四个项目在**结构化流程、防止一步到位、子智能体委派、错误恢复、防跑偏**五个维度的源码实现。

---

## 1. 结构化流程：如何约束模型按步骤做事

### 1.1 OpenCode — 双 Agent 模式 + 步数硬限制

OpenCode 通过**内置两个预配置 Agent**（`build` 和 `plan`）来区分执行阶段，再加上 `steps` 字段限制每个 Agent 的最大步数。

**Agent 定义**（`packages/opencode/src/agent/agent.ts:140-195`）：

```typescript
// build agent: 默认执行者，允许编辑和提问
build: {
  name: "build",
  description: "The default agent. Executes tools based on configured permissions.",
  permission: Permission.merge(defaults, Permission.fromConfig({
    question: "allow",
    plan_enter: "allow",
  }), user),
  mode: "primary",
},

// plan agent: 只读模式，禁止所有编辑工具
plan: {
  name: "plan",
  description: "Plan mode. Disallows all edit tools.",
  permission: Permission.merge(defaults, Permission.fromConfig({
    question: "allow",
    plan_exit: "allow",
    task: { general: "deny" },
    edit: { "*": "deny", [path.join(".opencode", "plans", "*.md")]: "allow" },
  }), user),
  mode: "primary",
},
```

**关键设计**：
- `plan` Agent **禁止所有写操作**，只能读文件和写 `.opencode/plans/*.md`
- `plan` Agent **禁止子智能体委派**（`task: { general: "deny" }`），防止计划阶段就执行
- `plan_enter` / `plan_exit` 权限分别控制进入和退出 plan 模式

**步数硬限制**（`packages/core/src/session/runner/max-steps.ts:1-16`）：

```
CRITICAL - MAXIMUM STEPS REACHED

The maximum number of steps allowed for this task has been reached.
Tools are disabled until next user input. Respond with text only.

STRICT REQUIREMENTS:
1. Do NOT make any tool calls
2. MUST provide a text response summarizing work done so far
3. This constraint overrides ALL other instructions

Response must include:
- Statement that maximum steps reached
- Summary of accomplishments
- List of remaining tasks
- Recommendations for next steps
```

**主循环中的集成**（`packages/core/src/session/runner/llm.ts:202-213`）：

```typescript
const isLastStep = agent.info?.steps !== undefined && currentStep >= agent.info.steps
const toolMaterialization = isLastStep ? undefined : yield* tools.materialize(...)
// ...
const request = LLM.request({
  messages: [...toLLMMessages(context, model),
    ...(isLastStep ? [Message.assistant(MAX_STEPS_PROMPT)] : [])
  ],
  tools: toolMaterialization?.definitions ?? [],
  toolChoice: isLastStep ? "none" : undefined,  // 强制工具选择为 none
})
```

**关键机制**：最后一步时，(1) tools 数组为空，(2) tool_choice = "none"，(3) 注入 MAX_STEPS_PROMPT 作为 assistant 消息强制模型转文本输出。

**主循环**（`llm.ts:383-406`）：

```typescript
const run = Effect.fn("SessionRunner.run")(function* (input) {
  // ...
  while (shouldRun) {
    let needsContinuation = true
    let step = 1
    while (needsContinuation) {
      const result = yield* runTurn(input.sessionID, promotion, step)
      needsContinuation = result.needsContinuation
      step = result.step + 1
      promotion = "steer"  // 后续轮次优先检查 steer
      // ...
    }
    shouldRun = yield* SessionInput.hasPending(db, input.sessionID, "queue")
  }
})
```

步数递增由框架控制，模型无法跳过。

---

### 1.2 Aider — system_reminder 机制 + SEARCH/REPLACE 格式强制

Aider 通过**两步注入** `system_reminder` 来确保模型遵循格式约束：

**System prompt 尾部注入**（`base_coder.py:1261-1262`）：

```python
if self.gpt_prompts.system_reminder:
    main_sys += "\n" + self.fmt_system_prompt(self.gpt_prompts.system_reminder)
```

**每次用户消息末尾/前再次注入**（`base_coder.py:1285-1329`）：

```python
if self.gpt_prompts.system_reminder:
    reminder_message = [
        dict(role="system", content=self.fmt_system_prompt(self.gpt_prompts.system_reminder)),
    ]

# 如果 token 预算允许，注入 system_reminder
if (
    not max_input_tokens
    or total_tokens < max_input_tokens
    and self.gpt_prompts.system_reminder
):
    if self.main_model.reminder == "sys":
        chunks.reminder = reminder_message
    elif self.main_model.reminder == "user" and final and final["role"] == "user":
        # 塞进用户消息的末尾
        new_content = (
            final["content"] + "\n\n"
            + self.fmt_system_prompt(self.gpt_prompts.system_reminder)
        )
        chunks.cur[-1] = dict(role=final["role"], content=new_content)
```

**system_reminder 内容**（`editblock_prompts.py:120-159`）：

```python
system_reminder = """# *SEARCH/REPLACE block* Rules:

Every *SEARCH/REPLACE block* must use this format:
1. The *FULL* file path alone on a line, verbatim.
2. The opening fence and code language, eg: ```python
3. The start of search block: <<<<<<< SEARCH
4. A contiguous chunk of lines to search for in the existing source code
5. The dividing line: =======
6. The lines to replace into the source code
7. The end of the replace block: >>>>>>> REPLACE
8. The closing fence: ```

Every *SEARCH* section must *EXACTLY MATCH* the existing file content.
...
ONLY EVER RETURN CODE IN A *SEARCH/REPLACE BLOCK*!
"""
```

**设计哲学**：
- `system_reminder` 被**同时注入 system prompt 和每条用户消息附近**，形成双重保险
- "ONLY EVER RETURN CODE IN A SEARCH/REPLACE BLOCK!" 是 Aider 的核心约束口号
- 这实际上是一种**输出格式门控**（output format gating）

---

### 1.3 Goose — State Machine Pipeline

Goose 使用**有序操作流水线**（ordered operation pipeline），每个操作（Operation）是一个独立的检查点。

**操作模块列表**（`crates/goose/src/agents/state_machine/mod.rs:8-27`）：

```rust
mod ops_bang_shell;     // ! 开头的 shell 命令
mod ops_compaction;     // 上下文压缩
mod ops_doctor;         // 诊断
mod ops_exit_on_error;  // 出错时退出
mod ops_llm;            // LLM 推理
mod ops_maxturns;       // 最大轮次限制
mod ops_project;        // 项目信息
mod ops_recipe;         // 配方/工作流
mod ops_retry;          // 重试逻辑
mod ops_skills;         // 技能加载
mod ops_slash_command;  // 斜杠命令
mod ops_steer;          // 用户中途介入
mod ops_stop_hook;      // 停止钩子
mod ops_tool_approval;  // 工具批准
mod ops_tool_pair_compaction; // 工具对压缩
mod ops_toolcalling;    // 工具执行
mod ops_unknown_tool;   // 未知工具处理
```

**Step 枚举**（`machine.rs:17-20`）：

```rust
pub enum Step<'a> {
    Operation(Arc<dyn Operation + 'a>),
    Inference(Arc<dyn Inference + 'a>),
}
```

**Step 执行**（`machine.rs:98-156`）：按顺序遍历所有 Step，Inference Step 会**聚合所有 Operation 的 tools/prompts/moim_parts**，然后调用 LLM。

**主循环**（`machine.rs:260-327`）：

```rust
pub async fn run(&self, session_manager, session_id, emit) -> Result<Session> {
    loop {
        let session = session_manager.get_session(session_id, true).await?;
        let Some(mut result) = self.step(&session, emit).await? else {
            break;  // 没有更多步骤，退出
        };
        self.apply(session_manager, &session, &mut result, emit).await?;
        if result.yield_to_client {
            break;  // 需要交还给用户
        }
    }
    // ...
}
```

**关键**：每个 Operation 都有 `name()` 返回唯一标识，且返回类型为 `OperationResult`（NotApplicable / Applied / Yielded），框架根据返回值决定是继续、退出还是等待用户输入。

---

### 1.4 Claude Code — Stop Hook + SubagentStart/Stop 钩子体系

Claude Code 通过完整的**生命周期钩子**控制每一步：

**钩子事件**（按执行顺序）：
```
SessionStart → UserPromptSubmit → [PreToolUse → PostToolUse]* → Stop → SessionEnd
```

**Stop Hook 的决策控制**（来自 Claude Code 文档）：

```
Stop hook 可以返回:
{
  "decision": "block",
  "reason": "Must be provided when Claude is blocked from stopping"
}
```

或通过 `additionalContext` 注入反馈：
```json
{
  "hookSpecificOutput": {
    "hookEventName": "Stop",
    "additionalContext": "Please run the test suite before finishing"
  }
}
```

**内置保护**：
- `stop_hook_active` 字段：当 Stop hook 已经触发一次继续后设为 `true`
- **8 次连续阻塞上限**：超过后强制结束，防止死循环

---

## 2. 防止一步到位：如何让模型小步快跑

### 2.1 OpenCode — 步数限制 + 工具禁用

```typescript
// agent.ts:54
steps: Schema.optional(Schema.Finite),

// llm.ts:202
const isLastStep = agent.info?.steps !== undefined && currentStep >= agent.info.steps
const toolMaterialization = isLastStep ? undefined : yield* tools.materialize(...)
```

- `steps` 是 Agent 配置的可选字段，默认无限制
- 达到 `steps` 后，工具被**物理禁用**（不注册，tool_choice="none"）
- 模型**只能输出文本摘要**，无法继续工作

### 2.2 Aider — SEARCH/REPLACE 格式本身就是"小步"约束

Aider 的核心设计就是强迫模型**每次只输出 SEARCH/REPLACE 块**：

- 一个 SEARCH/REPLACE 块**只能替换一处匹配**
- 需要多处修改时，模型必须**输出多个小 SEARCH/REPLACE 块**
- system_reminder 强调："Break large *SEARCH/REPLACE* blocks into a series of smaller blocks that each change a small portion of the file."

### 2.3 Goose — turn_budget_part XML 注入 + MaxTurns

**XML turn_budget_part**（`ops_maxturns.rs:19-27`）：

```rust
fn turn_budget_part(turns_taken: u32, max_turns: u32) -> Option<String> {
    if max_turns == 0 || turns_taken.saturating_mul(2) < max_turns {
        return None;  // 前半段不显示，避免误导
    }
    Some(format!(
        "<turn-budget>{turns_taken}/{max_turns} used</turn-budget>"
    ))
}
```

- 只有超过一半预算时才注入 `<turn-budget>` 标签
- **过半才告知**避免在早期就制造紧迫感导致模型焦虑
- 预算用尽时输出 `MAX_TURNS_MESSAGE`：`"I've reached the maximum number of actions I can do without user input."`

**MAX_TURNS 硬约束**（`ops_maxturns.rs:59-62`）：

```rust
if assistant_turn_count(messages) < self.max_turns {
    return not_applicable();
}
// 达到上限，yield to client
let message = Message::assistant().with_text(MAX_TURNS_MESSAGE);
yielded_with([message.into()])
```

默认 MAX_TURNS=1000，子智能体可以通过 TaskConfig 设置更低的 `max_turns`。

### 2.4 Claude Code — maxTurns + effort 分级

Claude Code 子智能体配置：
- `maxTurns`: 子智能体的最大自主轮次
- `effort`: 努力级别（low/medium/high/xhigh/max），控制模型思考深度
- 主流程通过 Stop hook 的 8 次阻塞上限防止无限循环

---

## 3. 子智能体委派

### 3.1 OpenCode — Task 工具 + 深度限制 + 权限派生

**Task 工具描述**（`task.txt:1-19`）：

```
Launch a new agent to handle complex, multistep tasks autonomously.

Usage notes:
1. Launch multiple agents concurrently whenever possible
2. Once delegated, do not duplicate that work
3. When agent is done, it returns a single message
4. Each agent starts with fresh context unless task_id is provided
5. Agent's outputs should generally be trusted
6. Clearly tell the agent whether to write code or do research
```

**深度限制**（`task.ts:106-117`）：

```typescript
const parent = yield* sessions.get(ctx.sessionID)
let current = parent
let depth = 0
while (current.parentID) {
  depth++
  current = yield* sessions.get(current.parentID)
}
if (depth >= (cfg.subagent_depth ?? 1)) {
  return yield* Effect.fail(
    new Error(`Subagent depth limit reached...`)
  )
}
```

- 默认 `subagent_depth = 1`，即**只允许一层子智能体嵌套**
- 防止无限递归创建子智能体

**子智能体权限派生**（`task.ts:139-155`）：

```typescript
const childPermission = deriveSubagentSessionPermission({
  parentSessionPermission: parent.permission ?? [],
  subagent: next,
})
const childToolDenies = [
  // 禁止子智能体再次调用 task 工具（防止无限委派）
  ...(next.permission.some(r => r.permission === "todowrite") ? [] :
    [{ permission: "todowrite", pattern: "*", action: "deny" }]),
  ...(next.permission.some(r => r.permission === id) ? [] :
    [{ permission: id, pattern: "*", action: "deny" }]),
  // 父级 primary_tools 在子智能体中自动 deny
  ...(cfg.experimental?.primary_tools?.map(p =>
    ({ permission: p, pattern: "*", action: "deny" })) ?? []),
]
```

**关键安全设计**：
- 子智能体默认**禁止再次调用 task**（防止递归）
- 子智能体权限从**父 session 继承并收紧**
- `primary_tools`（仅主 Agent 可用的工具）在子智能体中自动 deny

**后台模式**（`task.ts:25-41`）：

```typescript
// 后台任务启动后的提示词
const BACKGROUND_STARTED = [
  "The task is working in the background. You will be notified automatically.",
  "DO NOT sleep, poll for progress, ask the task for status, or duplicate this task's work",
  "Work on non-overlapping tasks, or briefly tell the user what you launched."
].join("\n")
```

**结果回收**（`task.ts:216-243`）：

子智能体完成后通过 `inject` 函数将结果**以 synthetic 用户消息**注入父 session：

```typescript
yield* ops.prompt({
  sessionID: ctx.sessionID,
  parts: [{
    type: "text",
    synthetic: true,
    text: renderOutput({
      sessionID: nextSession.id,
      state,  // "completed" | "error"
      summary: `Background task completed: ${params.description}`,
      text,
    }),
  }],
})
```

输出格式：
```xml
<task id="session_id" state="completed">
<summary>Background task completed: ...</summary>
<task_result>...result text...</task_result>
</task>
```

---

### 3.2 Goose — Subagent 独立 Session + max_turns 传递

**子智能体 prompt 构建**（`subagent_handler.rs:241-270`）：

```rust
async fn build_subagent_prompt(agent, task_config, session_id, system_instructions) -> Result<String> {
    let tools = agent.list_tools(session_id, None).await;
    render_template("subagent_system.md", &SubagentPromptContext {
        max_turns: task_config.max_turns.expect("TaskConfig always sets max_turns"),
        subagent_id: session_id.to_string(),
        task_instructions: system_instructions,
        tool_count: tools.len(),
        available_tools: tools.iter().map(|t| t.name.to_string()).join(", "),
    })
}
```

**子智能体执行**（`subagent_handler.rs:121-239`）：

```rust
let session_config = SessionConfig {
    id: session_id.clone(),
    schedule_id: None,
    max_turns: task_config.max_turns.map(|v| v as u32),
    retry_config: recipe.retry,
};

let mut stream = agent.reply(user_message, session_config, cancellation_token).await;

while let Some(message_result) = stream.next().await {
    match message_result {
        Ok(AgentEvent::Message(msg)) => { conversation.push(msg); }
        Ok(AgentEvent::HistoryReplaced(updated)) => { conversation = updated; }
        // ...
    }
}
```

- 子智能体是**完整独立的 Agent 实例**
- 通过独立 `SessionConfig` 传递限制（max_turns、retry_config）
- 父 Agent 获取**完整的 conversation 流**，可从中提取文本

---

### 3.3 Claude Code — Agent Teams + Parallel Dispatch

**子智能体配置**（来自 Claude Code 文档）：

```yaml
# .claude/agents/code-improver.md
---
name: code-improver
description: Scans files and suggests improvements
tools: Read, Grep, Glob
model: sonnet
maxTurns: 10
---
You are a code improvement specialist...
```

**关键特性**：
- `model`: 可为每个子智能体指定不同模型（sonnet/opus/haiku）
- `maxTurns`: 子智能体最大轮次
- `background: true`: 始终后台运行
- `isolation: worktree`: 在隔离的 git worktree 中运行
- **并行调度**：单个消息包含多个 Agent tool call 即可并发
- **Opus 4.7** 是默认的子智能体协调模型

**Agent 工具输入**（来自 Hook 文档 PreToolUse → Agent）：

```json
{
  "tool_name": "Agent",
  "tool_input": {
    "prompt": "Find all API endpoints",
    "description": "Find API endpoints",
    "subagent_type": "Explore",
    "model": "sonnet"
  }
}
```

**TeammateIdle hook**：当 agent team 成员即将空闲时触发，可通过 exit code 2 阻止停止。

---

### 3.4 Aider — Architect/Editor 双模型分工

Aider 的 Architect/Editor 模式不是真正的"子智能体委派"，而是一种**顺序双阶段工作流**：

```python
# architect_coder.py:6-48
class ArchitectCoder(AskCoder):
    edit_format = "architect"

    def reply_completed(self):
        content = self.partial_response_content
        if not self.auto_accept_architect and not self.io.confirm_ask("Edit the files?"):
            return

        editor_model = self.main_model.editor_model or self.main_model
        kwargs["main_model"] = editor_model
        kwargs["edit_format"] = self.main_model.editor_edit_format
        # ... 配置 Editor Coder

        editor_coder = Coder.create(**new_kwargs)
        editor_coder.cur_messages = []  # 空消息历史
        editor_coder.run(with_message=content, preproc=False)

        self.move_back_cur_messages("I made those changes to the files.")
```

**流程**：
1. **Architect**（通常是强模型）：接收需求，输出修改方案（纯文本描述）
2. 用户确认后，**Editor**（可能用弱模型）：接收 Architect 的方案文本，执行实际代码修改
3. Editor 的 `cur_messages = []` 避免被历史困惑
4. 完成后通过 `move_back_cur_messages` 将结果同步回主 session

Architect prompt 核心指令（`architect_prompts.py:7-17`）：

```
Act as an expert architect engineer and provide direction to your editor engineer.
Study the change request and the current code.
Describe how to modify the code to complete the request.
The editor engineer will rely solely on your instructions, so make them unambiguous.
DO NOT show the entire updated function/file/etc!
```

---

## 4. 错误恢复

### 4.1 Goose — 最完善的错误恢复体系

#### 4.1.1 RetryOperation：Goal/Grind + 成功检查 + 失败回调

**核心流程**（`ops_retry.rs:177-283`）：

```rust
async fn run(&self, session, conversation, emit) -> Result<OperationResult> {
    let messages = messages_since_kickoff(conversation)?;
    if !ends_turn(messages) {
        return not_applicable();  // 还没结束，继续执行
    }

    // 1. Goal 检查：先确认目标是否达成
    if !self.goal_was_nudged(messages) {
        if let Some(goal) = self.goal.lock().await.clone() {
            let nudge = format!(
                "Before finishing, check whether the following goal has been met:\n\n
                 **Goal:** {goal}\n\n
                 If not, continue working toward it."
            );
            // 注入 goal nudge 作为 user 消息
            let mut message = Message::user().with_text(&nudge)
                .with_visibility(false, true);  // agent-only visible
            self.set_message_meta(&mut message, NUDGED, serde_json::json!(true));
            return applied([message.into()]);
        }
    }

    // 2. Grind 检查：持续工作直到 max_turns
    if let Some(grind) = self.grind.lock().await.clone() {
        let nudge = format!("Keep working. The grind goal is not yet complete...");
        return applied([message.into()]);
    }

    // 3. 成功检查脚本（用户定义）
    let retry_config = Self::retry_config(session)?;
    let success = execute_success_checks_with_timeout(&retry_config.checks, retry_timeout).await;

    if success {
        return not_applicable();  // 通过，正常结束
    }

    // 4. 超过最大重试次数
    if attempts >= retry_config.max_retries {
        let message = Message::assistant().with_error(
            MessageErrorKind::Other,
            format!("Maximum retry attempts ({}) exceeded.", retry_config.max_retries)
        );
        return applied([message.into()]);
    }

    // 5. 执行失败回调
    if let Some(command) = &retry_config.on_failure {
        execute_on_failure_command_with_timeout(command, timeout).await;
    }

    // 6. 重置 conversation（保留 kickoff 消息，计数增加）
    let mut reset = Self::reset_conversation(conversation)?;
    if let Some(kickoff) = reset.messages_mut().last_mut() {
        self.set_message_meta(kickoff, ATTEMPTS, json!(attempts + 1));
    }
    applied([reset.into()])  // 触发重新推理
}
```

**关键设计点**：

| 机制 | 实现 | 用途 |
|------|------|------|
| Goal Nudge | 注入不可见 user 消息，标记 NUDGED | 避免重复 nudge |
| Grind | 无条件继续，直到 max_turns | 持续工作模式 |
| Success Checks | 用户定义的 shell 命令 | 验证任务是否完成 |
| On Failure | 用户定义的 shell 命令 | 失败时清理/通知 |
| Max Retries | `retry_config.max_retries` | 防止无限重试 |
| Conversation Reset | 回滚到 kickoff 消息 | 给模型全新上下文 |

#### 4.1.2 ExitOnErrorOperation

```rust
// ops_exit_on_error.rs:20-31
async fn run(&self, _session, conversation, _emit) -> Result<OperationResult> {
    if trailing_error(conversation).is_none() {
        return not_applicable();
    }
    yielded()  // 有尾随错误 → 交还控制权给用户
}
```

- 检查 conversation 末尾是否有错误消息
- 有错误立即 yield to client，不给模型"瞎试"的机会

#### 4.1.3 StopHook Block Cap

```rust
// ops_stop_hook.rs:88-105
let blocks = messages.iter()
    .filter(|message| self.message_meta(message, DENIED).is_some())
    .count() as u32 + 1;

if blocks > self.block_cap {  // 默认 8
    let warning = block_cap_warning(&plugin, self.block_cap);
    yielded_with([warning.into()])  // 强制放行
} else {
    // 注入 denial 消息继续循环
    applied([denial.into()])
}
```

- `STOP_HOOK_BLOCK_CAP=8`：Stop hook 连续阻塞 8 次后，**强制结束**
- 每次 denial 注入不可见的上下文消息告知模型原因
- 超限时输出警告 notification

---

### 4.2 Aider — max_reflections + malformed_response 计数

```python
# base_coder.py:101
num_reflections = 0
max_reflections = 3  # 最多反射/重试 3 次

# base_coder.py:936-944
while message:
    self.reflected_message = None
    list(self.send_message(message))

    if not self.reflected_message:
        break  # 没有需要反思的内容

    if self.num_reflections >= self.max_reflections:
        self.io.tool_warning(f"Only {self.max_reflections} reflections allowed, stopping.")
        return

    self.num_reflections += 1
    message = self.reflected_message
```

**反射机制**：模型输出了不符合 SEARCH/REPLACE 格式的内容 → 解析失败 → 构造反思消息重新发送 → 最多 3 次。

---

### 4.3 OpenCode — Context Overflow Recovery

```typescript
// llm.ts:231-239
const providerStream = llm.stream(request).pipe(
  Stream.runForEach((event) =>
    Effect.gen(function* () {
      if (LLMEvent.is.providerError(event)) {
        if (isContextOverflowFailure(event) && !publisher.hasAssistantStarted()) {
          overflowFailure = event  // 标记而非立即失败
          return
        }
      }
      // ...
    })
  )
)

// llm.ts:282-288
if (recoverOverflow && !publisher.hasAssistantStarted()
    && isContextOverflowFailure(overflowFailure ?? failure)
    && (yield* restore(recoverOverflow({ sessionID, entries, model, request }))))
  return yield* Effect.die(continueAfterOverflowCompaction(currentStep))
```

- 检测到 context overflow（未启动 assistant 回复时）
- 触发 compaction 后重试
- overflow compaction 只能做**一次**：`"Post-compaction provider attempt cannot recover another overflow"`

---

## 5. 防止跑偏

### 5.1 Aider — SEARCH/REPLACE 格式是最强的防跑偏机制

Aider 防止跑偏的核心不是"提醒目标"，而是**彻底约束输出格式**：

- **只允许输出 SEARCH/REPLACE 块**，任何其他格式都被视为错误
- system_reminder 在**每次都注入**："ONLY EVER RETURN CODE IN A *SEARCH/REPLACE BLOCK*!"
- 模型如果开始解释/聊天/写 markdown，解析器会报错并触发 reflection

这是一种**格式级硬约束**，比"提醒目标"更强制。

---

### 5.2 Goose — Steer 队列 + Goal 注入

**Steer 队列**（`ops_steer.rs:42-76`）：

```rust
async fn run(&self, session, conversation, emit) -> Result<OperationResult> {
    let messages = messages_since_kickoff(conversation)?;
    let between_turns = ends_turn(messages) || last_effective_role(messages)? == EffectiveRole::Tool;
    if !between_turns {
        return not_applicable();  // 只在轮次间注入
    }

    let pending: Vec<_> = self.queue.lock().await.drain(..).collect();
    if pending.is_empty() {
        return not_applicable();
    }

    for message in pending {
        let message = emit.message(message).await;
        effects.push(message.into());
    }
    applied(effects)  // 注入后继续
}
```

- 用户可以在 agent 运行中通过 `/steer` 命令注入指引
- Steer 消息在**轮次之间**注入（不打断正在进行的工具调用）
- 每个 steer 消息触发 `UserPromptSubmit` hook

**Goal 注入**（`ops_retry.rs:188-206`）：

```rust
// 在每次 turn 结束前检查 goal 是否达成
let nudge = format!(
    "Before finishing, check whether the following goal has been fully met:\n\n
     **Goal:** {goal}\n\n
     If not, continue working toward it."
);
let mut message = Message::user().with_text(&nudge)
    .with_visibility(false, true);  // agent-only
```

Goal 在每轮结束前注入，作为"目标提醒"，确保模型没有偏离。

---

### 5.3 Claude Code — Stop hook 质量门控

Stop hook 是 Claude Code 防跑偏的核心机制：

```
Stop hook 在模型声称"完成"时触发
→ 外部脚本检查输出是否符合预期
→ 不符合 → 返回 {"decision": "block", "reason": "..."}
→ 模型收到 reason，继续工作
→ 最多 8 次连续阻塞
```

这本质上是**外部验证循环**：模型说"我完成了" → hook 检查 → 没完成就踢回去。

配合 `additionalContext` 可以注入"你应该检查 X"之类的指引。

---

### 5.4 OpenCode — 步数限制 + 权限收窄

OpenCode 通过以下组合防止跑偏：

1. **步数硬限制**：`steps` 字段，超限后禁用工具
2. **权限收窄**：子智能体权限只能从父级**继承并收紧**，不能扩大
3. **doom_loop 检测**：权限配置中 `doom_loop: "ask"`，检测重复工具调用模式
4. **Steer 注入**：用户可中途 `/steer` 消息，在轮次间注入

---

## 6. 跨项目模式对比

### 6.1 结构化流程对比

| 项目 | 结构化机制 | 强制程度 | 实现方式 |
|------|-----------|---------|---------|
| OpenCode | plan/build 双 Agent + steps | **硬**（代码禁用工具） | 权限配置 + 步数计数 |
| Aider | system_reminder + SEARCH/REPLACE | **硬**（解析器拒绝） | Prompt 重复注入 |
| Goose | State Machine Pipeline | **硬**（代码顺序） | Operation trait |
| Claude Code | Hook 生命周期 | **软**（外部脚本） | 钩子配置 + 8 次上限 |

### 6.2 小步快跑对比

| 项目 | 机制 | 细节 |
|------|------|------|
| OpenCode | steps 限制 | 达到上限后 tools 设为 undefined + tool_choice="none" |
| Aider | SEARCH/REPLACE 单次替换 | 一个块只能改一处，大改需要多个小块 |
| Goose | turn-budget XML 注入 + MaxTurns | 过半才显示预算，yield 时输出友好消息 |
| Claude Code | maxTurns + effort 分级 | 可配模型努力程度，限步数 |

### 6.3 子智能体委派对比

| 项目 | 子 Agent 机制 | 深度限制 | 并发 | 结果回收 |
|------|-------------|---------|------|---------|
| OpenCode | task 工具 + Agent 类型 | subagent_depth 默认 1 | 支持后台模式 | XML task 格式 + synthetic 消息 |
| Goose | 独立 Agent 实例 + Session | 无硬限制 | 不支持内置并发 | 完整 conversation 流 |
| Claude Code | Agent(model:xxx) | 不限制 | **原生并行** | summary 文本 |
| Aider | Architect→Editor 串行 | 仅一层 | 不支持 | 文本方案 → Edit Coder 执行 |

### 6.4 错误恢复对比

| 项目 | 重试机制 | 最大次数 | 成功后检查 | 失败回调 |
|------|---------|---------|-----------|---------|
| Goose | RetryOperation | max_retries | success_checks | on_failure |
| Aider | reflection | 3 次 | 格式解析 | - |
| OpenCode | Compaction recovery | 1 次 | overflow check | - |
| Claude Code | Stop hook block | 8 次 | 外部脚本 | - |

### 6.5 防跑偏对比

| 项目 | 核心机制 | 特色 |
|------|---------|------|
| Aider | 格式硬约束 | **最强**：物理限制输出只能是 SEARCH/REPLACE |
| Goose | Goal 注入 + Steer | Goal 自动注入每轮结束前；Steer 队列支持中途指引 |
| Claude Code | Stop hook 质量门 | 外部验证循环，不通过就踢回去 |
| OpenCode | 权限收窄 + 步数限制 | doom_loop 检测 + subagent_depth 限制 |

---

## 7. 关键洞察与设计建议

### 7.1 "小步快跑"最有效的三个机制

1. **输出格式门控**（Aider 模式）：物理限制模型只能输出特定格式，比任何 prompt 都有效
2. **步数 + 工具禁用**（OpenCode 模式）：达到上限后停用工具、强制 tool_choice="none"
3. **过半提醒**（Goose 模式）：只有过半预算才显示 `<turn-budget>`，避免早期焦虑

### 7.2 "防跑偏"最可靠的模式

**外部验证循环**（Claude Code Stop hook + Goose Retry goal）比内部 prompt 更可靠：
- 模型说"完成" → 外部脚本验证 → 不通过就踢回去
- Goose 的 goal nudge 本质上是简化版的"外部验证循环"，但验证方是模型本身

### 7.3 子智能体安全的"铁三角"

OpenCode 的子智能体设计是安全标准：
1. **深度限制**（默认 1）：防止递归委派
2. **权限收窄**（只能继承并收紧）：防止越权
3. **禁止再委派**（deny task tool）：防止失控

### 7.4 State Machine 的架构优势

Goose 的 Operation Pipeline 是最清晰的可扩展架构：
- 每个关注点（重试、限制、批准、错误）都是独立的 Operation
- 按顺序执行，每个 Operation 返回 NotApplicable / Applied / Yielded
- 新功能只需添加新 Operation，不修改现有代码

---

## 附录：源码文件索引

| 文件 | 行数 | 角色 |
|------|------|------|
| opencode: `packages/opencode/src/agent/agent.ts` | 453 | Agent 定义（build/plan/general/explore） |
| opencode: `packages/core/src/session/runner/llm.ts` | 432 | 主执行循环 + 步数限制 + compaction |
| opencode: `packages/core/src/session/runner/max-steps.ts` | 16 | MAX_STEPS_PROMPT |
| opencode: `packages/opencode/src/tool/task.ts` | 360 | Task 工具（子智能体委派） |
| opencode: `packages/opencode/src/tool/task.txt` | 19 | Task 工具描述 |
| aider: `aider/coders/editblock_prompts.py` | 172 | system_reminder + SEARCH/REPLACE 格式 |
| aider: `aider/coders/architect_coder.py` | 48 | Architect/Editor 双模型 |
| aider: `aider/coders/architect_prompts.py` | 40 | Architect system prompt |
| aider: `aider/coders/base_coder.py` | 2485 | system_reminder 注入逻辑 + reflection |
| goose: `crates/goose/src/agents/state_machine/mod.rs` | 60 | State Machine 模块声明 |
| goose: `crates/goose/src/agents/state_machine/machine.rs` | 327 | StateMachine 实现 + Step 枚举 |
| goose: `crates/goose/src/agents/state_machine/ops_maxturns.rs` | 67 | MaxTurns + turn_budget_part |
| goose: `crates/goose/src/agents/state_machine/ops_retry.rs` | 284 | Retry + Goal/Grind + Success Checks |
| goose: `crates/goose/src/agents/state_machine/ops_stop_hook.rs` | 107 | StopHook + BLOCK_CAP |
| goose: `crates/goose/src/agents/state_machine/ops_steer.rs` | 77 | Steer 队列 |
| goose: `crates/goose/src/agents/state_machine/ops_exit_on_error.rs` | 32 | 尾随错误自动退出 |
| goose: `crates/goose/src/agents/subagent_handler.rs` | 361 | 子智能体 prompt 构建 + 执行 |
| Claude Code: `docs/en/hooks` | - | Hook 生命周期 + Stop/PreToolUse/PostToolUse |
| Claude Code: `docs/en/sub-agents` | - | 子智能体配置 + Agent Teams |
