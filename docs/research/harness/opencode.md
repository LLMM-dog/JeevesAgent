# OpenCode 驾驭工程深度分析

> **项目**: anomalyco/opencode | **Stars**: 194K+ | **语言**: TypeScript | **架构**: Monorepo (Bun/Turborepo)
> **核心包**: `packages/core/`（Agent 编排）、`packages/opencode-ai/`（模型抽象）、`packages/schema/`（类型系统）、`packages/plugin/`（插件 SDK）
> **技术栈**: Effect-TS（代数效应）、Drizzle ORM（SQLite）、Zod/Effect Schema（验证）

---

## 目录

1. [Agent 定义系统](#1-agent-定义系统)
2. [工具执行流水线](#2-工具执行流水线)
3. [Plugin 系统](#3-plugin-系统)
4. [Permission 权限模型](#4-permission-权限模型)
5. [ProviderTransform — Provider 差异隔离](#5-providertransform--provider-差异隔离)
6. [Session 管理](#6-session-管理)
7. [Context 管理](#7-context-管理)
8. [整体架构评估与对 Jeeves 的启示](#8-整体架构评估与对-jeeves-的启示)

---

## 1. Agent 定义系统

### 1.1 双轨制 Agent 定义

OpenCode 采用**两层 Agent 定义**：Markdown 配置 + TypeScript 硬编码。

#### 第一层：`.opencode/agent/*.md`（YAML Frontmatter + Markdown Body）

```markdown
---
mode: primary
hidden: true
model: opencode/gpt-5.4-mini
color: "#44BA81"
tools:
  "*": false
  "github-triage": true
---

You are a triage agent responsible for triaging github issues.
Use your github-triage tool to triage issues.
```

**Frontmatter 字段解析**（`packages/core/src/v1/config/agent.ts`）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `mode` | `subagent \| primary \| all` | 是否出现在 @ 菜单、是否可被作为子代理调用 |
| `hidden` | `boolean` | 隐藏 Agent（不显示在 UI，仅内部使用） |
| `model` | `string` | 绑定的模型 |
| `color` | `#RRGGBB \| 主题色名` | UI 显示颜色 |
| `tools` | `Record<string, boolean>` | 已弃用的工具开关，正向等价于 `permission` |
| `permission` | `Permission.Ruleset` | 细粒度权限规则（V2 使用） |
| `steps` | `number` | 最大代理步数，超限后强制纯文本回复 |
| `system` | `string` | 覆盖系统 prompt |

**关键设计**：
- Unknown keys 自动归入 `options` 字段，不丢失额外配置
- `tools: { "*": false, "github-triage": true }` → 自动转换为 `permission` 规则
- `steps` 和已弃用的 `maxSteps` 兼容处理

#### 第二层：TypeScript 硬编码 Agent（`packages/core/src/plugin/agent.ts`）

OpenCode 内置 7 个 Agent，全部通过 `AgentPlugin` 在代码中硬编码注册：

```typescript
// packages/core/src/plugin/agent.ts
export const Plugin = define({
  id: "agent",
  effect: Effect.fn(function* (ctx) {
    const worktree = location.directory

    // 全局默认权限规则（所有 Agent 共享）
    const defaults: PermissionV2.Ruleset = [
      { action: "*", resource: "*", effect: "allow" },      // 全放通
      ...readonlyExternalDirectory,                           // 外部目录只读
      { action: "question", resource: "*", effect: "deny" }, // 禁止反问
      { action: "plan_enter", resource: "*", effect: "deny" },
      { action: "plan_exit", resource: "*", effect: "deny" },
      { action: "read", resource: "*.env", effect: "ask" },  // .env 需确认
      { action: "read", resource: "*.env.*", effect: "ask" },
      { action: "read", resource: "*.env.example", effect: "allow" },
    ]

    // Build Agent（默认）
    draft.update(AgentV2.defaultID, (item) => {
      item.system ??= BUILD_SYSTEM  // "You are an AI coding agent..."
      item.mode = "primary"
      item.permissions.push(...PermissionV2.merge(defaults, [
        { action: "question", resource: "*", effect: "allow" },
        { action: "plan_enter", resource: "*", effect: "allow" },
      ]))
    })

    // Plan Agent —— 只读保护硬编码
    draft.update(AgentV2.ID.make("plan"), (item) => {
      item.mode = "primary"
      item.permissions.push(...PermissionV2.merge(defaults, [
        { action: "edit", resource: "*", effect: "deny" },   // 拒绝所有编辑
        { action: "edit", resource: ".opencode/plans/*.md", effect: "allow" }, // 仅允许 plan 目录
        { action: "plan_exit", resource: "*", effect: "allow" },
      ]))
    })

    // Explore Agent —— 只读搜索代理
    draft.update(AgentV2.ID.make("explore"), (item) => {
      item.mode = "subagent"
      item.system = PROMPT_EXPLORE  // "You are a file search specialist..."
      item.permissions.push(...PermissionV2.merge(defaults, [
        { action: "*", resource: "*", effect: "deny" },  // 先全部拒绝
        { action: "grep", resource: "*", effect: "allow" },
        { action: "glob", resource: "*", effect: "allow" },
        { action: "read", resource: "*", effect: "allow" },
        { action: "webfetch", resource: "*", effect: "allow" },
        { action: "websearch", resource: "*", effect: "allow" },
      ]))
    })

    // General Agent —— 通用子代理
    draft.update(AgentV2.ID.make("general"), (item) => {
      item.mode = "subagent"
      item.permissions.push(...PermissionV2.merge(defaults, [
        { action: "todowrite", resource: "*", effect: "deny" }  // 不允许写 TODO
      ]))
    })

    // 内部 Agent（compaction, title, summary）—— 全部禁止工具
    for (const id of ["compaction", "title", "summary"]) {
      draft.update(AgentV2.ID.make(id), (item) => {
        item.mode = "primary"
        item.hidden = true
        item.permissions.push(...PermissionV2.merge(defaults, [
          { action: "*", resource: "*", effect: "deny" }
        ]))
      })
    }
  }),
})
```

### 1.2 Agent 选择与解析

```typescript
// packages/core/src/agent.ts
export interface Interface {
  readonly resolve: (id?: ID | string) => Effect.Effect<Info | undefined>
  readonly select: (id?: ID | string) => Effect.Effect<Selection>
  readonly default: () => Effect.Effect<Info | undefined>
}
```

**选择优先级**：
1. 用户显式指定的 Agent ID
2. 配置的默认 Agent（存储在 Session 中）
3. `build` Agent（`AgentV2.defaultID`）
4. 第一个 `mode !== "subagent" && !hidden` 的 Agent

**过滤规则**：`hidden: true` 或 `mode === "subagent"` 的 Agent 不出现在 UI @ 菜单中，但可通过 API 直接调用。

### 1.3 设计优点

| 优点 | 说明 |
|------|------|
| **Markdown 原生** | 用户无需懂 TypeScript，手写 .md 即可定义 Agent |
| **权限即配置** | Agent 的能力边界完全由 Permission Ruleset 定义，无需修改核心代码 |
| **白名单优先** | `"*": false` → 逐个开放，安全默认（deny-by-default） |
| **Plan Agent 的硬编码只读** | 不是"建议"，而是代码层 `{ action: "edit", resource: "*", effect: "deny" }` 强制保证 |
| **隐藏 Agent 机制** | `hidden: true` 的 Agent 不出现在 UI，但可通过系统调度（如自动 compaction） |

### 1.4 对 Jeeves 的启示

```
Jeeves 当前: 单一 Agent，无 Agent 概念
建议:
  1. Agent = name + system_prompt + permission_ruleset + model
  2. 支持 YAML/Markdown 配置 + Python 注册双轨制
  3. "只读模式" 通过 deny edit/batch/write 规则实现，而非特殊模式
  4. 隐藏 Agent 用于内部任务（如自动摘要、标题生成）
```

---

## 2. 工具执行流水线

### 2.1 核心抽象：`Tool.make()`

```typescript
// packages/core/src/tool/tool.ts
export function make<Input, Output, Structured = Output>(
  config: Config<Input, Output, Structured>
): Definition<Input, Structured> {
  const tool = Object.freeze({}) as Definition<Input, Structured>
  runtimes.set(tool, {
    // 延迟生成 ToolDefinition（带缓存）
    definition: (name) => {
      const cached = definitions.get(name)
      if (cached) return cached
      const definition = new ToolDefinition({
        name,
        description: config.description,
        inputSchema: toJsonSchema(config.input),   // Effect Schema → JSON Schema
        outputSchema: toJsonSchema(config.structured ?? config.output),
      })
      definitions.set(name, definition)
      return definition
    },
    // settle: 验证输入 → 执行 → 验证输出 → 结构化输出
    settle: (call, context) =>
      Schema.decodeUnknownEffect(config.input)(call.input).pipe(
        Effect.mapError(/* InvalidInput */),
        Effect.flatMap((input) =>
          config.execute(input, context).pipe(
            Effect.flatMap((output) =>
              Schema.encodeEffect(config.output)(output).pipe(
                Effect.flatMap((output) => {
                  // 可选的 structured output（如函数调用的 JSON schema 返回）
                  if (!config.structured) return Effect.succeed({ output, structured: output })
                  return Schema.encodeEffect(config.structured)(
                    config.toStructuredOutput!({ input, output })
                  ).pipe(Effect.map((structured) => ({ output, structured })))
                }),
              ),
            ),
          ),
        ),
      ),
  })
  return tool
}
```

**Tool.Config 接口**：

```typescript
type Config<Input, Output, Structured = Output> = {
  description: string                          // LLM 可见描述
  input: Input                                 // Effect Schema（自动转 JSON Schema）
  output: Output                               // 输出 Schema
  structured?: Structured                      // 可选的 LLM 结构化输出格式
  toStructuredOutput?: (input: { input; output }) => Structured
  execute: (input, context: Context) => Effect<Output, ToolFailure>
  toModelOutput?: (input: { input; output }) => Content[]  // 自定义模型输出格式
}
```

### 2.2 工具注册与解析

```typescript
// packages/core/src/tool/registry.ts
// 注册：addFinalizer 保证作用域退出时自动清理
register: Effect.fn("ToolRegistry.register")(function* (tools) {
  yield* Effect.uninterruptible(
    Effect.gen(function* () {
      const token = {}
      for (const [name, tool] of entries)
        local.set(name, [...(local.get(name) ?? []), { token, registration: { identity: {}, tool } }])
      yield* Effect.addFinalizer(() =>
        Effect.sync(() => {
          // 移除所有属于此 token 的注册
          for (const [name] of entries) {
            const registrations = local.get(name)?.filter(r => r.token !== token) ?? []
            if (registrations.length > 0) local.set(name, registrations)
            else local.delete(name)
          }
        }),
      )
    }),
  )
}),

// 实例化：局部注册优先于应用级注册（后注册覆盖前注册）
materialize: Effect.fn("ToolRegistry.materialize")(function* (permissions = []) {
  const registrations = new Map(applications.entries())
  for (const [name, entries] of local)
    registrations.set(name, entries.at(-1)!.registration)  // 取最后一个
  // 权限过滤：完全禁用的工具从定义列表中移除
  for (const [name, registration] of registrations)
    if (whollyDisabled(permission(registration.tool, name), permissions))
      registrations.delete(name)
  return {
    definitions: Array.from(registrations, ([name, reg]) => definition(name, reg.tool)),
    settle: (input) => { /* ... */ }
  }
})
```

### 2.3 工具执行调度（SessionRunner）

工具执行的核心在 `SessionRunner.runTurn()` → `toolMaterialization.settle()`：

```typescript
// packages/core/src/session/runner/llm.ts (简化)
const runTurnAttempt = Effect.fn(function* (sessionID, promotion, step, recoverOverflow) {
  // 1. 准备系统上下文和对话历史
  const system = yield* SessionContextEpoch.prepare(db, events, loadSystemContext(agent), session.id)
  const model = yield* models.resolve(session)
  const entries = yield* SessionHistory.entriesForRunner(db, session.id, system.baselineSeq)

  // 2. 检查是否达到最大步数 → 禁用工具
  const isLastStep = agent.info?.steps !== undefined && currentStep >= agent.info.steps
  const toolMaterialization = isLastStep
    ? undefined
    : yield* tools.materialize(agent.info?.permissions)

  // 3. 构建 LLM 请求
  const request = LLM.request({
    model,
    system: [agent.info?.system, system.baseline].map(SystemPart.make),
    messages: [...toLLMMessages(context, model)],
    tools: toolMaterialization?.definitions ?? [],
    toolChoice: isLastStep ? "none" : undefined,  // 强制纯文本
  })

  // 4. 流式处理 LLM 响应
  const providerStream = llm.stream(request).pipe(
    Stream.runForEach((event) =>
      Effect.gen(function* () {
        yield* publish(event)  // 持久化每个 SSE 事件
        if (event.type !== "tool-call" || event.providerExecuted) return

        // 5. 本地工具调用 → 并行执行
        if (!toolMaterialization) {
          yield* publisher.failUnsettledTools("Tools disabled after max steps")
          return
        }
        needsContinuation = true
        const assistantMessageID = yield* publisher.assistantMessageID(event.id)
        yield* toolMaterialization.settle({
          sessionID, agent, assistantMessageID, call: event,
        }).pipe(
          Effect.flatMap((settlement) =>
            publish(LLMEvent.toolResult({
              id: event.id, name: event.name,
              result: settlement.result, output: settlement.output,
            }), settlement.outputPaths ?? [])
          ),
        ).pipe(FiberSet.run(toolFibers))  // 并行执行所有工具
      }),
    ),
  )

  // 6. 等待所有工具 fiber 完成 → 决定是否继续
  const settled = yield* restore(awaitToolFibers(toolFibers))
  return { needsContinuation: !publisher.hasProviderError() && needsContinuation, step: currentStep }
})
```

### 2.4 工具状态机

```typescript
// packages/schema/src/session-message.ts
// 4 种工具状态：
export const ToolStatePending = Schema.Struct({
  status: Schema.Literal("pending"),
  input: Schema.String,  // 原始 JSON 字符串（流式传输中）
})

export const ToolStateRunning = Schema.Struct({
  status: Schema.Literal("running"),
  input: Schema.Record(Schema.String, Schema.Unknown),  // 解析后
  structured: Schema.Record(Schema.String, Schema.Unknown),
  content: ToolContent.pipe(Schema.Array),
})

export const ToolStateCompleted = Schema.Struct({
  status: Schema.Literal("completed"),
  input: Schema.Record(Schema.String, Schema.Unknown),
  content: ToolContent.pipe(Schema.Array),
  outputPaths: Schema.Array(Schema.String).pipe(optional),
  structured: Schema.Record(Schema.String, Schema.Unknown),
  result: Schema.Unknown.pipe(optional),
})

export const ToolStateError = Schema.Struct({
  status: Schema.Literal("error"),
  input: Schema.Record(Schema.String, Schema.Unknown),
  error: UnknownError,
})
```

**状态流转**：`pending → running → completed | error`

对应的 SSE 事件序列：

```
Tool.Input.Started → Tool.Input.Delta* → Tool.Input.Ended
  → Tool.Called → Tool.Success | Tool.Failed
```

### 2.5 工具输出截断与持久化

```typescript
// packages/core/src/tool-output-store.ts
const bound = Effect.fn("ToolOutputStore.bound")(function* (input) {
  const outputLimits = yield* limits()  // 默认: maxLines=2000, maxBytes=50KB
  const contextual = text.map(item => item.text).join("")

  if (lineCount(contextual) <= outputLimits.maxLines &&
      Buffer.byteLength(contextual) <= outputLimits.maxBytes)
    return { output: input.output, outputPaths: [] }  // 直接返回

  // 超限 → 写入文件 + 返回 head/tail 预览
  const outputPath = yield* write(contextual)
  return {
    output: { content: [{ type: "text", text: boundedPreview(contextual, marker, ...) }], ... },
    outputPaths: [outputPath],
  }
})
```

**截断策略**：head (`⌈maxLines/2⌉` 行) + marker + tail (`⌊maxLines/2⌋` 行)，按 UTF-8 字节精确裁剪。

### 2.6 最大步数保护

```typescript
// packages/core/src/session/runner/max-steps.ts
export const MAX_STEPS_PROMPT = `CRITICAL - MAXIMUM STEPS REACHED
The maximum number of steps allowed for this task has been reached.
Tools are disabled until next user input. Respond with text only.
...`
```

当 `currentStep >= agent.info.steps` 时：
- `toolMaterialization` 设为 `undefined`
- `toolChoice` 强制为 `"none"`
- 在消息末尾追加 `MAX_STEPS_PROMPT` 文本

### 2.7 设计优点

| 优点 | 说明 |
|------|------|
| **Effect Schema 作为唯一类型源** | 输入/输出 Schema 自动转为 JSON Schema（给 LLM）和运行时验证（给引擎），无重复定义 |
| **作用域绑定注册** | Effect Scope + addFinalizer 保证工具注册在 Agent 退出时自动清理 |
| **并行工具执行** | `FiberSet.run(toolFibers)` 使多个 tool-call 并行执行，最后 `FiberSet.join` 等待全部完成 |
| **流式工具输入** | `pending` 状态支持增量接收 LLM 的流式 tool-call input |
| **自动截断 + 持久化** | 超长工具输出自动写入磁盘文件并返回头尾预览，7 天自动清理 |
| **maxSteps 安全阀** | 软限制（纯文本提示）+ 硬限制（tools=[]，toolChoice="none"）双重保障 |

### 2.8 对 Jeeves 的启示

```
Jeeves 当前: 工具执行是同步回调，无流式输入，无状态机
建议:
  1. Tool = Pydantic input_schema + output_schema + async execute(ctx)
  2. 工具注册按 Agent 作用域隔离（context manager 模式）
  3. 实现 pending → running → completed/error 状态机
  4. 大输出自动截断 + 写文件 + 返回路径引用
  5. 支持并行工具执行（asyncio.gather + 超时控制）
```

---

## 3. Plugin 系统

### 3.1 Hooks 接口（20+ 钩子点）

```typescript
// packages/plugin/src/index.ts
export interface Hooks {
  dispose?: () => Promise<void>

  // ===== 生命周期 =====
  event?: (input: { event: Event }) => Promise<void>           // 全局事件监听
  config?: (input: Config) => Promise<void>                     // 配置修改

  // ===== 工具 =====
  tool?: { [key: string]: ToolDefinition }                      // 注册自定义工具
  "tool.definition"?: (input: { toolID: string }, output: { description, parameters }) => Promise<void>
  "tool.execute.before"?: (input: { tool, sessionID, callID }, output: { args }) => Promise<void>
  "tool.execute.after"?: (input: { tool, sessionID, callID, args }, output: { title, output, metadata }) => Promise<void>

  // ===== Chat 管道 =====
  "chat.message"?: (input, output: { message, parts }) => Promise<void>
  "chat.params"?: (input, output: { temperature, topP, topK, maxOutputTokens, options }) => Promise<void>
  "chat.headers"?: (input, output: { headers }) => Promise<void>

  // ===== 权限 =====
  "permission.ask"?: (input: Permission, output: { status: "ask" | "deny" | "allow" }) => Promise<void>

  // ===== Shell =====
  "shell.env"?: (input: { cwd, sessionID, callID }, output: { env }) => Promise<void>

  // ===== 命令 =====
  "command.execute.before"?: (input: { command, sessionID, arguments }, output: { parts }) => Promise<void>

  // ===== Provider =====
  auth?: AuthHook                     // OAuth/API Key 认证
  provider?: ProviderHook             // 自定义 Provider（模型列表）

  // ===== 实验性 =====
  "experimental.chat.messages.transform"?: (input, output: { messages }) => Promise<void>
  "experimental.chat.system.transform"?: (input, output: { system: string[] }) => Promise<void>
  "experimental.session.compacting"?: (input, output: { context, prompt? }) => Promise<void>
  "experimental.compaction.autocontinue"?: (input, output: { enabled }) => Promise<void>
  "experimental.text.complete"?: (input, output: { text }) => Promise<void>
}

export type Plugin = (input: PluginInput, options?: PluginOptions) => Promise<Hooks>
```

### 3.2 Plugin Context（插件可用的能力）

```typescript
export type PluginInput = {
  client: ReturnType<typeof createOpencodeClient>   // 完整 SDK 客户端
  project: Project                                    // 当前项目信息
  directory: string                                   // 工作目录
  worktree: string                                    // Git worktree 根
  experimental_workspace: { register(type, adapter) } // Workspace 适配器
  serverUrl: URL                                      // 服务器 URL
  $: BunShell                                         // Shell API
}
```

### 3.3 Plugin 加载来源

```typescript
// packages/core/src/config/plugin.ts
export const Plugin = Schema.Union([
  Schema.String,                     // npm 包名 或 文件路径
  Schema.Struct({                     // { package, options }
    package: Schema.String,
    options: Schema.Record(Schema.String, Schema.Unknown),
  }),
])
```

**3 个加载来源**（优先级由高到低）：
1. **npm 包**：`@scope/my-plugin` → 通过 npm 安装后自动发现
2. **项目本地**：`.opencode/plugins/` → 相对于项目根目录
3. **全局配置**：`~/.config/opencode/plugins/` → 用户级插件

### 3.4 流水线模式

Plugin Hooks 采用**流水线（Pipeline）模式**：每个 hook 接收上一个 plugin 的输出作为自己的输入，可以修改输出对象：

```
Plugin A                     Plugin B                     Plugin C
   │                            │                            │
   ▼                            ▼                            ▼
input ──→ output (modified) ──→ output (modified) ──→ final output
```

```typescript
// 示例：chat.params hook 链
for (const plugin of plugins) {
  if (plugin["chat.params"]) {
    await plugin["chat.params"](input, output)
    // output = { temperature, topP, ... } 被逐步修改
  }
}
```

### 3.5 内置 Plugin 示例

**Agent Plugin**（`packages/core/src/plugin/agent.ts`）：
- 使用 `ctx.agent.transform()` 在应用启动时注册所有内置 Agent
- 为每个 Agent 设置默认权限规则

**自定义工具插件**（`packages/plugin/src/example.ts`）：

```typescript
export const ExamplePlugin: Plugin = async (_ctx) => {
  return {
    tool: {
      mytool: tool({
        description: "This is a custom tool",
        args: {
          foo: tool.schema.string().describe("foo"),
        },
        async execute(args) {
          return `Hello ${args.foo}!`
        },
      }),
    },
  }
}
```

### 3.6 设计优点

| 优点 | 说明 |
|------|------|
| **函数式 Plugin 接口** | `(input, options) => Promise<Hooks>` 极简，无类继承 |
| **流水线模式** | 每个 hook 输入是前一个的输出，支持渐进式修改 |
| **Zod Schema 工具定义** | `tool.schema.string().describe("foo")` 声明式、类型安全 |
| **完整的 Plugin Context** | Plugin 拥有 SDK 客户端 + Shell + 项目信息，完全自治 |
| **OAuth/API Key 认证** | `auth` hook 支持多步 OAuth 流程（文本输入、下拉选择、回调） |
| **Workspace 适配器** | `experimental_workspace.register()` 可注册自定义工作空间类型 |

### 3.7 对 Jeeves 的启示

```
Jeeves 当前: 无 Plugin 系统
建议:
  1. Plugin = async (ctx: PluginContext) -> Hooks
  2. Hooks = { on_tool_before, on_tool_after, on_llm_request, on_permission_check, ... }
  3. 支持 pip 包 + 本地目录 + 全局目录 三种来源
  4. Hook 链采用流水线模式（前一个输出 → 下一个输入）
  5. PluginContext 提供 Python SDK client + subprocess shell
```

---

## 4. Permission 权限模型

### 4.1 规则结构

```typescript
// packages/schema/src/permission.ts
export const Rule = Schema.Struct({
  action: Schema.String,    // 通配符匹配：read, edit, bash, *, etc.
  resource: Schema.String,  // glob 模式：*.env, /etc/**/*, etc.
  effect: Schema.Literals(["allow", "deny", "ask"]),
})

export const Ruleset = Schema.Array(Rule)  // 有序规则列表
```

### 4.2 匹配引擎

```typescript
// packages/core/src/permission.ts
export function evaluate(
  action: string,
  resource: string,
  ...rulesets: Permission.Ruleset[]
): Permission.Rule {
  return rulesets
    .flat()
    .findLast((rule) =>          // ★ last-match-wins!
      Wildcard.match(action, rule.action) &&
      Wildcard.match(resource, rule.resource)
    ) ?? { action, resource: "*", effect: "ask" }  // 默认：询问
}

// 实际验证：
const evaluateInput = Effect.fnUntraced(function* (input: AssertInput) {
  const rules = yield* configured(input.sessionID, input.agent)
  if (denied(input, rules)) return { effect: "deny", rules }

  const all = [...rules, ...(yield* savedRules())]  // 合并持久化规则
  const effects = input.resources.map(
    (resource) => evaluate(input.action, resource, all).effect
  )
  // 任一 deny → deny; 任一 ask → ask; 全部 allow → allow
  const effect = effects.includes("deny") ? "deny"
    : effects.includes("ask") ? "ask" : "allow"
  return { effect, rules: all }
})
```

### 4.3 Wildcard 匹配

```typescript
// packages/core/src/util/wildcard.ts
export function match(input: string, pattern: string) {
  const normalized = input.replaceAll("\\", "/")
  let escaped = pattern
    .replaceAll("\\", "/")
    .replace(/[.+^${}()|[\]\\]/g, "\\$&")  // 转义正则特殊字符
    .replace(/\*/g, ".*")                    // * → .*
    .replace(/\?/g, ".")                     // ? → .
  // 末尾 " *" 的特殊处理：匹配可选后缀
  if (escaped.endsWith(" .*"))
    escaped = escaped.slice(0, -3) + "( .*)?"
  return new RegExp("^" + escaped + "$").test(normalized)
}
```

### 4.4 三级权限 + Last-Match-Wins

```
规则列表（有序）：
  { action: "*",     resource: "*",             effect: "allow" }    // 1. 全放通
  { action: "read",  resource: "*.env",         effect: "ask" }      // 2. .env 需确认
  { action: "edit",  resource: "*",             effect: "deny" }     // 3. 禁止编辑（Plan Agent）
  { action: "edit",  resource: ".opencode/plans/*.md", effect: "allow" }  // 4. 例外：plan 文件可写

对于 read .env → findLast 匹配 rule 2 → ask
对于 edit src/main.ts → findLast 匹配 rule 3 → deny
对于 edit .opencode/plans/plan.md → findLast 匹配 rule 4 → allow
```

### 4.5 持久化批准

```typescript
// packages/core/src/permission/saved.ts
// 用户点击 "Always allow" → 写入 SQLite
yield* saved.add({
  projectID: location.project.id,
  action: existing.request.action,
  resources: existing.request.save,  // 具体的资源列表
})

// 自动级联：批准一个后，检查其他 pending 权限是否也满足
for (const [id, item] of pending) {
  if (item.request.resources.every(
    (resource) => evaluate(item.request.action, resource, effective).effect === "allow"
  )) {
    yield* Deferred.succeed(item.deferred, undefined)
    pending.delete(id)
  }
}
```

### 4.6 权限请求流

```
Tool.call → PermissionV2.assert(action, resources)
  ├── deny → BlockedError → LLM 收到 "denied" 消息
  ├── allow → 立即执行
  └── ask → 创建 Pending request → UI 弹窗
       ├── "Allow once" → Deferred.succeed → 执行
       ├── "Always allow" → Deferred.succeed + 写入 SQLite
       └── "Reject" → Deferred.fail → 级联拒绝同 Session 其他 pending
```

### 4.7 设计优点

| 优点 | 说明 |
|------|------|
| **Last-Match-Wins** | 规则列表有序，后定义的覆盖前面的，精确规则可覆盖通配规则 |
| **Action × Resource 二维匹配** | Glob + Glob 的笛卡尔积，灵活性极高 |
| **持久化批准自动级联** | 批准一个后自动检查并批准其他 pending 权限 |
| **项目级隔离** | `projectID` 确保不同项目的持久化批准互不干扰 |
| **Deferred/Effect 模型** | 权限请求用 Deferred 挂起，不阻塞 Event Loop |

### 4.8 对 Jeeves 的启示

```
Jeeves 当前: 无细粒度权限控制
建议:
  1. PermissionRule = { action: str, resource: str, effect: "allow"|"deny"|"ask" }
  2. 采用 last-match-wins 优先级
  3. action 和 resource 都支持 glob 匹配
  4. 持久化批准存 SQLite，按 project_id 隔离
  5. "deny" 优先级最高（只要有一条 deny 就拒绝）
```

---

## 5. ProviderTransform — Provider 差异隔离

### 5.1 Route 抽象

```typescript
// packages/core/src/session/runner/model.ts
export const fromCatalogModel = (
  model: ModelV2.Info,
  credential?: Credential.Value,
): Effect.Effect<Model, UnsupportedApiError> => {
  // 根据 api.type + api.package 选择不同的 Route 实现
  if (resolved.api.type === "aisdk" && resolved.api.package === "@ai-sdk/openai") {
    return Effect.succeed(
      withDefaults(resolved, OpenAIResponses.route)
        .with({ auth: Auth.bearer(key) })
        .model({ id: resolved.api.id }),
    )
  }
  if (resolved.api.type === "aisdk" && resolved.api.package === "@ai-sdk/anthropic") {
    return Effect.succeed(
      withDefaults(resolved, AnthropicMessages.route)
        .with({ auth: Auth.header("x-api-key", key) })
        .model({ id: resolved.api.id }),
    )
  }
  if (resolved.api.type === "aisdk" && resolved.api.package === "@ai-sdk/openai-compatible") {
    return Effect.succeed(
      withDefaults(resolved, OpenAICompatibleChat.route)
        .with({ auth: Auth.bearer(key) })
        .model({ id: resolved.api.id }),
    )
  }
}
```

### 5.2 Model Variant 系统

```typescript
const withVariant = (model, variantID) => {
  const variant = model.variants.find(item => item.id === variantID)
  return Effect.succeed(
    variant
      ? produce(model, (draft) => {
          // immer 不可变合并
          Object.assign(draft.request.headers, variant.headers)
          Object.assign(draft.request.body, variant.body)
        })
      : model,
  )
}
```

### 5.3 设计优点

| 优点 | 说明 |
|------|------|
| **Route 模式** | 每个 Provider 有独立的 Route（URL、认证、Header），统一 `Model` 接口 |
| **Variant 继承** | Model 可以有多个 Variant（如 `default`、`thinking`），每个 Variant 继承基础配置并覆盖 |
| **Immer 不可变合并** | `produce(model, draft => ...)` 确保原始对象不被修改 |
| **AISDK 桥接** | 通过 `@ai-sdk/*` 包统一调用，上层不感知 API 差异 |

### 5.4 对 Jeeves 的启示

```
Jeeves 当前: 直接调用 OpenAI SDK
建议:
  1. 抽象 LLMClient 接口：stream(messages, tools, system) -> AsyncIterator[Event]
  2. 每个 Provider 实现同一个接口（Anthropic/OpenAI/DeepSeek）
  3. 通过 factory 函数根据 provider_config 创建对应的 client
```

---

## 6. Session 管理

### 6.1 事件溯源架构

OpenCode 的 Session 是整个系统的心脏，采用**事件溯源（Event Sourcing）**模式：

```
用户输入 ──→ Event("user.prompted") ──→ Projector ──→ SQLite 投影表
LLM 响应 ──→ Event("text.delta")   ──→ Projector ──→ SessionMessageTable
工具执行 ──→ Event("tool.success") ──→ Projector ──→ SessionMessageTable
```

### 6.2 核心 API

```typescript
// packages/core/src/session.ts
export interface Interface {
  create: (input: CreateInput) => Effect<SessionInfo>
  prompt: (input: { sessionID, prompt, delivery? }) => Effect<Admitted>
  messages: (input: { sessionID, limit?, cursor? }) => Effect<Message[]>
  context: (sessionID) => Effect<Message[]>
  switchAgent: (input: { sessionID, agent }) => Effect<void>
  compact: (input: CompactInput) => Effect<void>
  resume: (sessionID) => Effect<void>
  interrupt: (sessionID) => Effect<void>
  revert: {
    stage: (input: { sessionID, messageID, files? }) => Effect<Revert.State>
    commit: (sessionID) => Effect<void>
  }
}
```

### 6.3 输入排队与调度

```typescript
// SessionInput
// 三种 delivery 模式：
type Delivery = "steer" | "queue"

// steer: 立刻中断当前 LLM 流，插入用户消息
// queue: 等待当前步完成后，再处理下一条用户消息

const run = Effect.fn(function* (input) {
  const hasSteer = yield* SessionInput.hasPending(db, sessionID, "steer")
  const hasQueue = hasSteer ? false : yield* SessionInput.hasPending(db, sessionID, "queue")

  let promotion: Delivery | undefined = hasSteer ? "steer" : hasQueue ? "queue" : undefined
  while (shouldRun) {
    let needsContinuation = true
    let step = 1
    while (needsContinuation) {
      const result = yield* runTurn(sessionID, promotion, step)
      needsContinuation = result.needsContinuation
      step = result.step + 1
      promotion = "steer"  // 后续轮次始终检查 steer 输入
    }
    shouldRun = yield* SessionInput.hasPending(db, sessionID, "queue")
  }
})
```

### 6.4 Session 每项目独立 SQLite

```typescript
// packages/core/src/database/database.ts
// 每个项目使用独立的 SQLite 数据库文件
// 存储内容：
// - SessionTable: session_id, project_id, directory, title, agent, model, cost, tokens
// - SessionMessageTable: session_id, id, type, data (JSON), seq
// - SessionInputTable: session_id, id, prompt, delivery, admitted_seq, promoted_seq
// - SessionContextEpochTable: session_id, baseline, snapshot, baseline_seq
// - PermissionTable: id, project_id, action, resource
```

### 6.5 Revert（回退）

```typescript
// 三步回退流程：
1. stage: 基于 snapshot diff 计算需要还原的文件变更
2. (用户确认)
3. commit: 应用还原 → 删除后续消息 → 发布 SessionEvent.Reverted
```

### 6.6 设计优点

| 优点 | 说明 |
|------|------|
| **事件溯源** | 所有状态变化都是事件，支持重放、审计、时间旅行 |
| **Steer 机制** | 用户可在 LLM 流式输出过程中实时注入指令（`delivery: "steer"`） |
| **每项目独立 SQLite** | 隔离性好，无跨项目数据泄漏 |
| **Cursor 分页** | 消息列表支持基于 seq 的前后翻页 |
| **Revert 三步确认** | Stage → Review → Commit，安全可控 |

### 6.7 对 Jeeves 的启示

```
Jeeves 当前: 无 Session 概念，对话不持久化
建议:
  1. Session = Event Sourcing + SQLite 投影
  2. 支持 steer（中途中断）和 queue（排队）两种输入模式
  3. 每项目独立 .jeeves.db
  4. 实现 checkpoint/revert 机制
```

---

## 7. Context 管理

### 7.1 Compaction 触发条件

```typescript
// packages/core/src/session/compaction.ts
const compactIfNeeded = Effect.fn(function* (input: Input) {
  if (!config.auto) return false
  const context = input.model.route.defaults.limits?.context  // 模型最大上下文
  if (context === undefined || context <= 0) return false
  const output = input.model.route.defaults.limits?.output ?? 0

  // 当前请求 token 数 > context - max(output, buffer)
  if (
    estimate({ system, messages, tools }) <=
    context - Math.max(output, config.buffer)  // buffer 默认 20K
  )
    return false
  return yield* compactAfterOverflow(input)
})
```

### 7.2 三阶段 Compaction

```
阶段 1: Selection（选择哪些消息参与压缩）
  ├── 从后往前累积 token，直到达到 keep.tokens (默认 8000)
  ├── 剩余的消息形成 head（旧对话）
  └── 保留的消息形成 recent（最近对话）

阶段 2: Generation（生成摘要）
  ├── 使用 compaction Agent（隐藏，tool disabled）
  ├── Prompt: 如果已有 previous summary → "Update the anchored summary"
  │                        否则 → "Create a new anchored summary"
  └── 输出格式:
      ## Objective
      ## Important Details
      ## Work State (Completed / Active / Blocked)
      ## Next Move
      ## Relevant Files

阶段 3: Replacement（替换）
  ├── 持久化 Compaction 消息（包含 summary + recent）
  └── SessionContextEpoch 更新 baseline（标记哪些系统消息已过期）
```

### 7.3 Compaction 摘要模板

```typescript
const SUMMARY_TEMPLATE = `Output exactly the Markdown structure shown inside <template>...

<template>
## Objective
- [one or two brief sentences describing what the user is trying to accomplish]

## Important Details
- [constraints/preferences, decisions and why, important facts/assumptions]

## Work State
### Completed
- [finished work, verified facts, or changes made; otherwise "(none)"]

### Active
- [current work, partial changes, or investigation state]

### Blocked
- [blockers, failing commands, or unknowns]

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

### 7.4 Subagent 生成摘要的具体 Prompt

```typescript
// Compaction Agent 的 System Prompt：
const PROMPT_COMPACTION = `You are an anchored context summarization assistant for coding sessions.

Summarize only the conversation history you are given. The newest turns may be kept verbatim outside your summary, so focus on the older context that still matters for continuing the work.

If the prompt includes a <previous-summary> block, treat it as the current anchored summary. Update it with the new history by preserving still-true details, removing stale details, and merging in new facts.

Always follow the exact output structure requested by the user prompt. Keep every section, preserve exact file paths and identifiers when known, and prefer terse bullets over paragraphs.

Do not answer the conversation itself. Do not mention that you are summarizing, compacting, or merging context. Respond in the same language as the conversation.`
```

### 7.5 Truncation 策略

```typescript
// 工具输出截断：保持最早 5 条完整，按字符裁剪（而非 token）
const truncate = (value: string) =>
  value.length <= TOOL_OUTPUT_MAX_CHARS
    ? value
    : `${value.slice(0, TOOL_OUTPUT_MAX_CHARS)}\n[truncated]`

// 消息序列化规则：
const serialize = (message: Message) => {
  if (message.type === "user")
    return `[User]: ${message.text}`
  if (message.type === "assistant")
    return message.content.map(part => {
      if (part.type === "text") return `[Assistant]: ${part.text}`
      if (part.type === "tool")
        return `[Assistant tool call]: ${part.name}(${input})\n[Tool result]: ${truncate(output)}`
      return ""
    }).join("\n")
  // system, synthetic, shell...
}
```

### 7.6 Context 版本管理（Epoch）

```typescript
// 系统上下文（工作区信息、git 状态等）通过 Epoch 机制管理版本
export function prepare(db, events, context, sessionID) {
  const [value, stored, compaction] = yield* Effect.all([
    context,              // 从 SystemContextRegistry 加载实时上下文
    find(db, sessionID),  // SQLite 中的快照
    latestCompaction(db, sessionID),
  ])
  if (!stored) {
    // 首次：初始化
    const generation = yield* SystemContext.initialize(value)
    yield* insert(db, sessionID, generation)
    return { baseline: generation.baseline, baselineSeq }
  }
  // 已有快照：
  //   有 compaction → replace（完全替换）
  //   无 compaction → reconcile（增量更新）
  if (compaction !== undefined && compaction.seq > stored.baseline_seq) {
    const result = yield* SystemContext.replace(value, snapshot)
    if (result._tag === "ReplacementReady")
      yield* replace(db, sessionID, baselineSeq, result.generation)
  } else {
    const result = yield* SystemContext.reconcile(value, snapshot)
    if (result._tag === "Changed")
      yield* events.publish(SessionEvent.ContextUpdated, { text: result.text })
  }
}
```

### 7.7 设计优点

| 优点 | 说明 |
|------|------|
| **Anchored Summary** | 增量更新摘要（而非每次全新生成），保留跨 compaction 的连续性 |
| **双窗口策略** | head（旧对话摘要化）+ recent（最近 N tokens 保留原文） |
| **结构化摘要模板** | 强制保留 Objective/Work State/Next Move 等关键信息 |
| **Compaction Agent 隔离** | 专门隐藏的 Agent，禁止所有工具，仅生成摘要 |
| **Epoch 版本管理** | 系统上下文（非对话内容）通过版本号跟踪变化，支持增量更新 |

### 7.8 对 Jeeves 的启示

```
Jeeves 当前: 无 context 管理，全量发送所有消息
建议:
  1. 实现 Selection→Generation→Replacement 三阶段 compaction
  2. 采用 anchored summary（增量更新历史摘要）
  3. 保留最近 N tokens 原文（窗口 + 摘要双轨制）
  4. 工具输出自动截断（按字符 + 行数双限制）
  5. 系统上下文独立管理（Epoch 版本号）
```

---

## 8. 整体架构评估与对 Jeeves 的启示

### 8.1 OpenCode 的核心设计模式

| 模式 | 体现 | 价值 |
|------|------|------|
| **Effect-TS 代数效应** | 所有 IO 通过 Effect 泛型描述，依赖通过 Layer 注入 | 可测试、可追踪、无副作用泄漏 |
| **Event Sourcing** | Session 所有状态变化都是事件 | 可重放、可审计、支持时间旅行 |
| **依赖注入** | `Node` = Service + Layer + Deps | 模块解耦，支持测试环境替换 |
| **Schema-first** | Effect Schema 作为唯一类型源 | 自动生成 JSON Schema + 运行时验证 |
| **Pipeline** | Plugin hooks 链式修改 | 可组合、可插拔 |
| **Last-Match-Wins** | 权限规则优先级 | 精确规则覆盖通配规则 |

### 8.2 Jeeves 可复用的设计

```
优先级排序（由高到低）：

1. Permission 权限模型
   - action × resource glob 匹配
   - allow/deny/ask 三级 + last-match-wins
   - 持久化批准（SQLite）
   
2. Agent 定义系统
   - YAML frontmatter + markdown body 配置
   - Agent = system_prompt + permission_ruleset + model + steps
   - 隐藏 Agent 用于内部任务

3. 工具执行流水线
   - Pydantic Schema → JSON Schema + 运行时验证
   - pending → running → completed/error 状态机
   - 作用域绑定注册（context manager）
   - 大输出自动截断 + 文件持久化

4. Context Compaction
   - 三阶段：Selection → Generation → Replacement
   - Anchored Summary（增量更新）
   - 结构化摘要模板

5. Plugin 系统
   - 函数式 Hook 接口
   - 流水线模式
   - pip/本地/全局 三种来源

6. Session 管理
   - Event Sourcing + SQLite
   - Steer/Queue 两种输入
   - Checkpoint/Revert
```

### 8.3 不推荐直接复用的设计

- **Effect-TS**：Python 无等价物，可用 `asyncio` + 依赖注入库代替
- **Monorepo (Bun/Turborepo)**：Jeeves 是 Python 项目，不需要
- **AISDK 桥接**：过度抽象，直接使用各 Provider 的 Python SDK 更简单

### 8.4 最小可行架构建议（Phase 1）

```
jeeves/
├── agents/
│   ├── build.yaml        # Agent 定义（YAML frontmatter）
│   └── plan.yaml
├── tools/
│   ├── read.py           # Tool = Pydantic input + async execute(ctx)
│   └── write.py
├── plugins/
│   └── my_plugin.py      # async def plugin(ctx) -> Hooks
├── permissions/
│   └── rules.py          # Rule = { action, resource, effect }
├── sessions/
│   ├── schema.py         # Session SQLite schema
│   ├── event.py          # Event types
│   └── projector.py      # Event → table projection
├── context/
│   ├── compaction.py     # Selection → Generation → Replacement
│   └── epoch.py          # System context versioning
└── harness.py            # 主编排循环
```

---

> **文档版本**: v1.0 | **生成日期**: 2026-08-08 | **基于源码**: anomalyco/opencode (main branch, latest)
