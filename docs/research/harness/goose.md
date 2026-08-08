# Goose 驾驭工程深度分析

> 项目：aaif-goose/goose (原 block/goose) — 15K+ Stars，Rust 编写，2025-12 捐赠给 Linux Foundation Agentic AI Foundation
> 分析版本：2026年8月 main 分支

---

## 目录

1. [总体架构概览](#1-总体架构概览)
2. [Agent 结构体深度解析](#2-agent-结构体深度解析)
3. [State Machine 架构](#3-state-machine-架构)
4. [硬编码保护体系](#4-硬编码保护体系)
5. [MCP 原生扩展系统](#5-mcp-原生扩展系统)
6. [Compaction 实现](#6-compaction-实现)
7. [Provider Usage 追踪](#7-provider-usage-追踪)
8. [Tool 分类系统](#8-tool-分类系统)
9. [三层安全检查体系](#9-三层安全检查体系)
10. [与 Hermes Agent 的对比](#10-与-hermes-agent-的对比)

---

## 1. 总体架构概览

Goose 的驾驭层（Harness Layer）采用**基于有序管道（Ordered Pipeline）的状态机**设计。每个用户输入触发一次 `reply()`，创建一条 `StateMachine`，它包含一组按优先级排列的 `Operation`（操作），在 `run()` 的 `loop` 中反复执行 `step()` → `apply()`，直到某个步骤返回 `yield_to_client: true`。

```
┌──────────────────────────────────────────────────────┐
│  Agent::reply(user_message)                          │
│    ├─ 持久化 user_message 到 Session                  │
│    ├─ create_state_machine(provider, config, ...)    │
│    │    ├─ 构建 Operations 有序列表 (16个)             │
│    │    └─ 构建 InferenceRunner (LLM调用)             │
│    ├─ StateMachine::run()                            │
│    │    └─ loop { step() → apply() }                 │
│    └─ 返回 AgentEvent 流                              │
└──────────────────────────────────────────────────────┘
```

核心设计理念：**"Operation 是有序的、可重入的、基于持久化会话状态的管道步骤"**。

---

## 2. Agent 结构体深度解析

### 2.1 完整字段定义

```rust
// crates/goose/src/agents/agent.rs:250-274
pub struct Agent {
    pub(super) provider: SharedProvider,           // Arc<Mutex<Option<Arc<dyn Provider>>>>
    pub config: AgentConfig,                       // 包含 SessionManager、PermissionManager 等
    pub(super) current_goose_mode: Mutex<GooseMode>, // Approve / SmartApprove / Chat

    // MCP 扩展管理
    pub extension_manager: Arc<ExtensionManager>,  // MCP 原生支持的核心
    pub(super) final_output_tool: Arc<Mutex<Option<FinalOutputTool>>>,
    pub(super) frontend_extensions: Mutex<HashMap<String, ExtensionConfig>>,
    pub(super) frontend_tools: Mutex<HashMap<String, FrontendTool>>,
    pub(super) frontend_instructions: Mutex<Option<String>>,

    // 提示词管理
    pub(super) prompt_manager: Mutex<PromptManager>,

    // 工具确认路由器
    pub tool_confirmation_router: ToolConfirmationRouter,

    // 工具结果通道 (mpsc)
    pub(super) tool_result_tx: mpsc::Sender<(String, ToolResult<CallToolResult>)>,
    pub(super) tool_result_rx: ToolResultReceiver,

    // 重试管理
    pub(super) retry_manager: RetryManager,

    // 工具检查管理（包含 5 个 Inspector）
    pub(super) tool_inspection_manager: ToolInspectionManager,

    // Hook 管理
    pub(super) hook_manager: crate::hooks::HookManager,

    // 容器（Docker 执行环境）
    container: Mutex<Option<Container>>,

    // 目标和持久化工作
    pub(super) goal: Mutex<Option<String>>,
    pub(super) grind: Mutex<Option<String>>,

    // 用户中途引导队列
    steer_queues: Mutex<HashMap<String, SteerQueue>>,
}
```

### 2.2 字段职责分析

| 字段 | 职责 | 设计亮点 |
|------|------|----------|
| `provider` | 多态 LLM Provider，通过 `Arc<Mutex<Option<>>>` 实现延迟设置和热切换 | 支持 40+ 模型提供商，通过 trait 抽象 |
| `extension_manager` | **MCP 协议原生支持**的战略核心 | 管理 MCP Server 生命周期、工具发现与注册、OAuth 认证 |
| `tool_inspection_manager` | 5 个 Inspector 组成的检查链，逐层审查所有工具调用 | 支持用户自定义 adversary.md 规则 |
| `hook_manager` | 遵循 Open Plugins 规范的 11 种生命周期 Hook | PreToolUse 可以 `emit_blocking` 阻断工具调用 |
| `retry_manager` | 支持 success_checks 和 on_failure 命令 | 可重置消息历史到初始状态重试 |
| `goal` / `grind` | 用户可设置持久化目标，Agent 在完成前自动检查 | `RetryOperation` 会主动 nudge Agent |
| `steer_queues` | 用户中途注入引导消息的队列 | 支持实时人工干预而不中断状态机 |

### 2.3 三层安全检查体系

```rust
// agent.rs:697-724 - create_tool_inspection_manager()
fn create_tool_inspection_manager(...) -> ToolInspectionManager {
    let mut manager = ToolInspectionManager::new();

    // 第1层：SecurityInspector - 模式匹配检测恶意工具调用
    // curl|bash, rm -rf /, 权限提升 等
    manager.add_inspector(Box::new(SecurityInspector::new()));

    // 第2层：EgressInspector - 网络出口检测，记录所有数据外泄
    // 检测 URL、git remote、S3、GCS、SSH、docker push、npm publish 等
    manager.add_inspector(Box::new(EgressInspector::new()));

    // 第3层：AdversaryInspector - LLM 对抗性审查
    // 读取 ~/.config/goose/adversary.md，用 LLM 做安全判断
    manager.add_inspector(Box::new(AdversaryInspector::new(...)));

    // 第4层：PermissionInspector - 权限系统
    manager.add_inspector(Box::new(PermissionInspector::new(...)));

    // 第5层：RepetitionInspector - 重复调用检测
    manager.add_inspector(Box::new(RepetitionInspector::new(None)));

    manager
}
```

#### SecurityInspector（第1层）— 模式匹配
- 使用 `SecurityManager::analyze_tool_requests()` 做正则模式匹配
- 检测 `curl | bash`、`rm -rf /`、权限提升、混淆代码等
- 返回 `InspectionAction::RequireApproval` 或 `Allow`

#### EgressInspector（第2层）— 数据外泄检测
```rust
// egress_inspector.rs:49-187 - extract_destinations()
// 检测 10+ 种网络出口模式：
//   - URL (https?://, ftp://)
//   - git SSH remote (git@host:path)
//   - S3 bucket (s3://)
//   - GCS bucket (gs://)
//   - SCP/rsync target
//   - SSH host
//   - Docker push/login
//   - npm publish / cargo publish
//   - nc/ncat/socat 等通用网络工具
// 方向检测：Outbound（推送/上传/发布） vs Inbound（克隆/下载）
```

#### AdversaryInspector（第3层）— LLM 对抗
```rust
// adversary_inspector.rs:56-78
/// 通过 ~/.config/goose/adversary.md 激活
/// 文件格式：
///   tools: shell, computercontroller__automation_script
///   ---
///   BLOCK if the command exfiltrates data or is destructive.
///   ALLOW normal development operations.
///
/// 设计原则：失败时 "fail-open"（允许通过），不阻塞正常工作
```

### 2.4 HookManager

Goose 实现了 11 种生命周期 Hook，遵循 Open Plugins 规范：

```rust
// hooks/mod.rs:51-63
pub enum HookEvent {
    PreToolUse,          // 工具执行前（可阻断）
    PostToolUse,         // 工具执行成功后
    PostToolUseFailure,  // 工具执行失败后
    SessionStart,        // 会话开始
    SessionEnd,          // 会话结束
    UserPromptSubmit,    // 用户提交提示词
    BeforeReadFile,      // 读取文件前
    AfterFileEdit,       // 文件编辑后
    BeforeShellExecution,// Shell 执行前
    AfterShellExecution, // Shell 执行后
    Stop,                // 停止前（可阻断）
}
```

关键特性：
- **PreToolUse 和 Stop 支持 `emit_blocking`**：可阻断操作
- **matcher 支持正则**：按工具名、命令内容匹配
- **Timeout**：每个 Hook 默认 30s 超时

### 2.5 RetryManager

```rust
// retry.rs:42-47
pub struct RetryManager {
    attempts: Arc<Mutex<u32>>,
    repetition_inspector: Option<Arc<Mutex<Option<RepetitionInspector>>>>,
}
```

- 支持 `RetryConfig`（Recipe 中定义）
- `max_retries` + `checks`（Shell 命令 success check）+ `on_failure` 回调
- 超时分两级：retry timeout（默认 60s）和 on_failure timeout（默认 120s）
- 通过环境变量 `GOOSE_RECIPE_RETRY_TIMEOUT_SECONDS` 和 `GOOSE_RECIPE_ON_FAILURE_TIMEOUT_SECONDS` 可配置

**对 Jeeves 的启示**：
1. Agent 结构体应分离关注点：ExtensionManager、InspectionManager、HookManager 各自独立
2. 三层安全设计（模式匹配 → 出口检测 → LLM 审查）比单一安全层可靠得多
3. HookManager 遵循 Open Plugins 规范，确保生态兼容性

---

## 3. State Machine 架构

### 3.1 核心概念

Goose 的状态机不是传统的"状态 → 事件 → 状态"FSM，而是一种**基于有序操作管道（Ordered Operation Pipeline）**的可重入执行模型。

```rust
// machine.rs:31-35
pub struct StateMachine<'a> {
    steps: Vec<Step<'a>>,       // 有序步骤列表
    cancel: CancellationToken,  // 取消令牌
    hook_manager: HookManager,  // 生命周期 Hook
}

// machine.rs:17-20
pub enum Step<'a> {
    Operation(Arc<dyn Operation + 'a>),   // 非推理操作
    Inference(Arc<dyn Inference + 'a>),    // LLM 推理（唯一）
}
```

### 3.2 Operation Trait

```rust
// operation.rs:79-141
#[async_trait]
pub trait Operation: Send + Sync {
    fn name(&self) -> &'static str;

    // 四个"入口点"：
    async fn run(&self, session, conversation, emit) -> Result<OperationResult>;
    async fn run_command(&self, command, session, conversation, emit) -> Result<OperationResult>;
    async fn inference_tools(&self, session) -> Result<Vec<Tool>>;     // 提供工具
    async fn prompt_parts(&self, session, conversation) -> Result<Vec<(String, String)>>;
    async fn moim_parts(&self, session, conversation) -> Result<Vec<String>>; // 给模型的上下文元信息
    async fn cancel(&self, session, conversation, result, emit) -> Result<OperationResult>;
}
```

### 3.3 完整的 Operation 管道（16个操作）

```rust
// agent.rs:1617-1680 - create_state_machine()
let operations: Vec<Arc<dyn Operation + '_>> = vec![
    // 1. SteerOperation - 用户中途引导注入
    Arc::new(SteerOperation::new(steer_queue, self.hook_manager.clone())),

    // 2. MaxTurnsOperation - 最大轮次限制（默认1000）
    Arc::new(MaxTurnsOperation::new(max_turns)),

    // 3. BangShellOperation - 解析 !command 语法
    Arc::new(BangShellOperation::new()),

    // 4. CompactionOperation - 自动/手动 compact
    Arc::new(CompactionOperation::new(provider, model_config, context_limit, threshold)),

    // 5. ToolPairCompactionOperation - 压缩旧的工具调用对
    Arc::new(ToolPairCompactionOperation::new(provider, model_config, cutoff, enabled)),

    // 6. ToolApprovalOperation - 工具审批（检查权限）
    Arc::new(ToolApprovalOperation::new(&self.current_goose_mode, &self.tool_inspection_manager)),

    // 7. DoctorOperation - 诊断
    Arc::new(DoctorOperation),

    // 8. ProjectOperation - 项目上下文
    Arc::new(ProjectOperation),

    // 9. SkillOperation - 技能加载
    Arc::new(SkillOperation),

    // 10. RecipeOperation - Recipe 执行
    Arc::new(RecipeOperation),

    // 11. ToolExecutionOperation - 批量执行工具调用
    Arc::new(ToolExecutionOperation::new(...)),

    // 12. UnknownToolOperation - 处理未知工具
    Arc::new(UnknownToolOperation),

    // 13. RetryOperation - goal/grind/retry 逻辑
    Arc::new(RetryOperation::new(&self.goal, &self.grind, ...)),

    // 14. StopHookOperation - Stop Hook 检查
    Arc::new(StopHookOperation::new(self.hook_manager.clone(), stop_hook_block_cap)),

    // 15. ExitOnErrorOperation - 错误退出
    Arc::new(ExitOnErrorOperation),
];

// 额外：SlashCommandOperation（包裹所有 operations + inference）作为第一个 step
// 最后：InferenceRunner 作为唯一的 Inference step
let steps = [SlashCommandOperation, ...operations, InferenceRunner]
    .map(Step::Operation / Step::Inference)
```

### 3.4 状态机执行流程

```rust
// machine.rs:260-326 - StateMachine::run()
pub async fn run(&self, session_manager, session_id, emit) -> Result<Session> {
    loop {
        let session = session_manager.get_session(session_id, true).await?;

        // step: 遍历 steps 列表，第一个返回 Applied 的 step 胜出
        let Some(mut result) = self.step(&session, emit).await? else {
            break;  // 所有 step 都返回 NotApplicable → 结束
        };

        // apply: 将 step 产生的 effects 持久化到 session
        self.apply(session_manager, &session, &mut result, emit).await?;

        // 检查是否需要让出控制权给客户端
        if result.yield_to_client {
            break;
        }
    }
    Ok(session)
}
```

**执行流程时间线**：
```
Turn 开始
  ↓
step() → 依次尝试每个 Operation
  ├─ SlashCommandOperation: 检查是否为斜杠命令，是→处理并 yield
  ├─ SteerOperation: 检查是否有待注入的引导消息
  ├─ MaxTurnsOperation: 检查是否超过 1000 轮 → yield "已达最大轮次"
  ├─ CompactionOperation: 检查 token 是否超 80% 阈值 → compact → applied
  ├─ ToolPairCompactionOperation: 压缩旧工具调用
  ├─ ToolApprovalOperation: 审批新的工具请求
  ├─ ToolExecutionOperation: 执行已批准的工具
  ├─ UnknownToolOperation: 标记未处理的工具请求
  ├─ RetryOperation: 检查 goal/grind/retry → nudge 或重置
  ├─ StopHookOperation: 检查 Stop Hook → 允许/拒绝
  ├─ ExitOnErrorOperation: 遇到错误 → yield
  └─ InferenceRunner (Step::Inference): 调用 LLM → 产生新消息
  ↓
apply() → 持久化 effects (AppendMessage, ReplaceConversation, ...)
  ↓
如果 !yield_to_client → 继续 loop
否则 → 返回
```

### 3.5 OperationResult 与 StateEffect

```rust
// operation.rs:179-227
pub enum OperationResult {
    NotApplicable,  // 不适用，继续下一个 step
    Applied(StepResult {
        effects: Vec<StateEffect>,
        yield_to_client: bool,  // true → 退出 loop，把控制权交还用户
    }),
}

pub enum StateEffect {
    AppendMessage(Message),                    // 追加消息
    ReplaceConversation { conversation, usage }, // 替换整个对话（compaction 后）
    PatchToolRequestMeta { tool_call_id, patch }, // 修补工具请求元数据
    SetMessageVisibility { message_id, user_visible, agent_visible },
    SetRecipe(Box<Option<Recipe>>),
    SetExtensionData(ExtensionData),
    RecordUsage(ProviderUsage),                // 记录用量
}
```

### 3.6 为什么选择状态机而非 while 循环？

1. **可重入性**：每个 step 读取持久化的 Session → 状态机可以从任意点恢复，支持中断-恢复
2. **可组合性**：新功能只需添加一个新的 Operation，放在管道的合适位置
3. **可测试性**：每个 Operation 独立测试（见 `state_machine/tests/` 下有 7 个测试文件）
4. **斜杠命令支持**：`SlashCommandOperation` 包装了所有操作，`!command` 可以通过 `BangShellOperation` 解析
5. **强制分离"决策"与"执行"**：step() 只做决策（产生 effects），apply() 负责持久化和发射事件
6. **并发友好**：多个 step 可以依赖共享的持久化 Session 状态

**对 Jeeves 的启示**：
- Python 的 while 循环 vs Rust 的状态机：Python 可以用类似的 "pipeline pattern" + "not_applicable / applied / yielded" 三元返回值来模拟
- 每个功能模块（compaction、retry、max_turns）独立为一个操作，清晰且可测试
- `StepResult.effects` 是声明式的副作用描述，apply() 集中处理持久化

---

## 4. 硬编码保护体系

### 4.1 核心常量

```rust
// agent.rs:80-86
const DEFAULT_MAX_TURNS: u32 = 1000;             // 防无限循环
const DEFAULT_STOP_HOOK_BLOCK_CAP: u32 = 8;      // 防 hook 死循环
const MAX_EMPTY_TURN_RETRIES: u32 = 3;           // 防空回复
const EMPTY_TURN_MESSAGE: &str = "The model returned an empty response...";
const COMPACTION_PROGRESS_TEXT: &str = "goose is compacting the conversation...";
```

```rust
// context_mgmt/mod.rs:26-28
pub const DEFAULT_COMPACTION_THRESHOLD: f64 = 0.8;  // 80% 上下文窗口 → 触发 compact
pub(crate) const TOOLCALL_SUMMARIZATION_BATCH_SIZE: usize = 10;
```

```rust
// ops_compaction.rs:23
pub(super) const MAX_CONTEXT_ERROR_COMPACTIONS: usize = 2;  // ContextLengthExceeded 后最多 compact 2次
```

```rust
// retry.rs:35-38
const GOOSE_RECIPE_RETRY_TIMEOUT_SECONDS: &str = "GOOSE_RECIPE_RETRY_TIMEOUT_SECONDS";       // 默认 60
const GOOSE_RECIPE_ON_FAILURE_TIMEOUT_SECONDS: &str = "GOOSE_RECIPE_ON_FAILURE_TIMEOUT_SECONDS"; // 默认 120
```

### 4.2 MAX_TURNS = 1000

```rust
// ops_maxturns.rs:52-66
async fn run(&self, session, conversation, emit) -> Result<OperationResult> {
    let messages = messages_since_kickoff(conversation)?;
    if assistant_turn_count(messages) < self.max_turns {
        return not_applicable();  // 还没到上限
    }

    let message = Message::assistant()
        .with_text("I've reached the maximum number of actions I can do without user input.
                    Would you like me to continue?");
    let message = emit.message(message).await;
    yielded_with([message.into()])  // 让出控制权
}
```

**设计精妙之处**：
- 不是直接报错退出，而是**友好地询问用户是否继续**
- 通过 `GOOSE_MAX_TURNS` 环境变量可覆盖
- `turn_budget_part()` 在达到 50% 时向模型提示剩余预算（`<turn-budget>500/1000 used</turn-budget>`）

### 4.3 STOP_HOOK_BLOCK_CAP = 8

```rust
// ops_stop_hook.rs:88-104
HookDecision::Deny { reason, plugin } => {
    let blocks = messages.iter()
        .filter(|message| self.message_meta(message, DENIED).is_some())
        .count() as u32 + 1;
    if blocks > self.block_cap {
        // 超过上限 → 强制通过，发警告
        let warning = block_cap_warning(&plugin, self.block_cap);
        yielded_with([warning.into()])
    } else {
        // 还在上限内 → 注入拒绝提示，继续
        let mut denial = denial_context_message(&plugin, &reason);
        self.set_message_meta(&mut denial, DENIED, serde_json::json!(true));
        applied([denial.into()])
    }
}
```

**设计亮点**：
- 连续 8 次拒绝后**强制通过**，避免死循环
- 通过 `GOOSE_STOP_HOOK_BLOCK_CAP` 环境变量可调
- 每次拒绝给 Agent 注入**策略解释**（`"Address this policy hook denial before trying to stop again"`）

### 4.4 MAX_EMPTY_TURN_RETRIES = 3

模型返回空响应后的重试逻辑，避免模型因为某些原因持续返回空内容。

### 4.5 Compaction 阈值 = 80%

```rust
// ops_compaction.rs:71-76
fn over_threshold(&self, tokens: usize) -> bool {
    if self.threshold <= 0.0 || self.threshold >= 1.0 {
        return false;
    }
    (tokens as f64 / self.context_limit as f64) > self.threshold
}
```

- 80% 是一个平衡值：太早（如 50%）浪费计算资源，太晚（如 95%）可能在 compaction 过程中超出限制
- 通过 `GOOSE_AUTO_COMPACT_THRESHOLD` 环境变量可调
- `MAX_CONTEXT_ERROR_COMPACTIONS = 2`：已经因为上下文过长而报错后，最多再尝试 2 次 compact

### 4.6 常量调优理由

| 常量 | 默认值 | 调优理由 |
|------|--------|----------|
| `MAX_TURNS` | 1000 | 单轮 Agent 任务的合理上限；超过后大概率是死循环或模型迷路了 |
| `STOP_HOOK_BLOCK_CAP` | 8 | 允许 Hook 多次"纠正"Agent，但防止 Hook 本身有问题导致永久卡住 |
| `COMPACTION_THRESHOLD` | 0.8 | 给 compaction 留 20% 缓冲空间，避免在 compact 过程中 OOM |
| `MAX_CONTEXT_ERROR_COMPACTIONS` | 2 | 已经出错时最多 compact 两次，第三次可能说明 compaction 提示词过大 |
| `EMPTY_TURN_RETRIES` | 3 | 偶尔的空响应可能是网络抖动，3 次后应报错 |

**对 Jeeves 的启示**：
- MAX_TURNS=1000 是合理默认值，但应允许环境变量覆盖
- STOP_HOOK（或对应机制）必须有防死循环上限
- Compaction 阈值 80% 是经过实践验证的平衡点
- 所有硬编码常量都应提供环境变量覆盖

---

## 5. MCP 原生扩展系统

### 5.1 ExtensionManager 架构

```rust
// extension_manager.rs:193-201
pub struct ExtensionManager {
    extensions: Mutex<HashMap<String, Extension>>,   // 活跃的 MCP Server 连接
    context: PlatformExtensionContext,
    provider: SharedProvider,
    tools_cache: Mutex<Option<Arc<Vec<Tool>>>>,       // 工具列表缓存
    tools_cache_version: AtomicU64,                   // 缓存版本号（用于失效）
    client_name: String,
    capabilities: ExtensionManagerCapabilities,
}
```

### 5.2 MCP Server 生命周期管理

```rust
// extension_manager.rs:124-168
struct Extension {
    pub config: ExtensionConfig,              // 原始配置
    resolved_config: ExtensionConfig,          // 解析后的配置（含 keyring secrets）
    client: McpClientBox,                     // MCP 客户端连接
    server_info: Option<ServerInfo>,           // MCP Server 信息（含 capabilities）
    _temp_dir: Option<tempfile::TempDir>,     // 临时目录（stdin transport 用）
}
```

**连接方式**：
- **子进程（Stdio）**：`TokioChildProcess` + stdio transport
- **HTTP（Streamable HTTP）**：`StreamableHttpClientTransport`
- **OAuth 支持**：通过 `GooseCredentialStore` 处理 OAuth 认证流程
- **Docker 容器**：支持在 Docker 容器中启动 MCP Server

### 5.3 工具发现与注册

```rust
// extension_manager.rs
// 1. 启动阶段：为每个配置的 extension 创建 MCP 客户端
// 2. 调用 client.list_tools() 获取工具列表
// 3. 工具名格式：extension_name__tool_name（双下划线分隔）
// 4. 工具列表缓存：tools_cache + tools_cache_version 实现高效失效

// agent.rs 中 prepare_tools_and_prompt():
// - 调用 extension_manager.get_tools(session_id)
// - 合并 frontend_tools
// - 构建 system prompt 的工具描述部分
```

**工具命名约定**：
```rust
// 扩展 "developer" 的工具 "shell" → "developer__shell"
// 扩展 "github" 的工具 "create_issue" → "github__create_issue"
// 内置扩展 "computercontroller" → 特殊前缀映射
```

### 5.4 工具执行流程

```rust
// agent.rs:1108-1238 - dispatch_tool_call()
pub async fn dispatch_tool_call(&self, tool_call, request_id, cancellation_token, session) {
    // 1. PreToolUse Hook (emit_blocking，可阻断)
    // 2. Pre-tool extended hooks (BeforeShellExecution, BeforeReadFile)
    // 3. 前端工具 vs 后端工具路由
    //    a. 前端工具 → 返回给前端执行
    //    b. 后端工具 → extension_manager.dispatch_tool_call()
    // 4. PostToolUse / PostToolUseFailure Hook
    // 5. Extended post hooks (AfterShellExecution, AfterFileEdit)
}
```

### 5.5 与 OpenCode/Claude Code MCP 的对比

| 维度 | Goose (Rust) | Claude Code (TypeScript) | OpenCode (Rust) | Hermes Agent (Python) |
|------|--------------|--------------------------|-----------------|----------------------|
| MCP 集成方式 | **原生内置**，ExtensionManager 是 Agent 的一等公民 | 原生内置，MCP Server 进程管理 | 插件机制 | 通过 `mcp_client` 工具调用 |
| 工具命名 | `extension__tool` 双下划线约定 | 直接使用原始工具名 | `server__tool` | JSON-RPC 工具调用 |
| 生命周期管理 | 自动启动/停止、OAuth、Docker、凭证轮转 | 自动启动/停止 | 插件生命周期 | 无自动管理 |
| 工具缓存 | `AtomicU64` 版本号失效 | 无显式缓存 | 无 | 无 |
| 安全隔离 | PreToolUse Hook 可阻断 | 权限确认 | 无 | 无 |

**Goose 的 MCP 设计亮点**：
1. **ExtensionConfig 类型安全**：`Frontend` / `Builtin` / `Platform` 多种扩展类型
2. **凭证轮转检测**：`resolved_config` 与原始 config 对比，自动检测 Secret 轮转
3. **搜索路径解析**：`SearchPaths::builder().with_npm().resolve(cmd)` 自动查找 npm 全局安装的 MCP Server
4. **通知通道**：MCP Server 可以发送 `ServerNotification`，通过 `AgentEvent::McpNotification` 传递
5. **资源支持**：除了 Tools，还支持 MCP Resources 和 Prompts

**对 Jeeves 的启示**：
- MCP 应作为架构的一等公民，而非作为普通工具注入
- 工具缓存 + 版本号失效模式高效且简单
- 凭证轮转检测是生产级 MCP 集成的必要功能
- `extension__tool` 命名空间约定解决了工具名冲突问题

---

## 6. Compaction 实现

### 6.1 触发机制

Goose 有两级 compaction：

**第一级：自动 CompactionOperation**（管道中的第4个 step）
```rust
// ops_compaction.rs:207-312 - CompactionOperation::run()
async fn run(&self, session, conversation, emit) -> Result<OperationResult> {
    // 1. 检查：provider 是否自己管理上下文（如 Gemini）→ 跳过
    if self.manages_own_context { return not_applicable(); }

    // 2. 反应式 compact：尾部错误是 ContextLengthExceeded
    if trailing_error(conversation) == Some(MessageErrorKind::ContextLengthExceeded) {
        if context_errors > MAX_CONTEXT_ERROR_COMPACTIONS { return not_applicable(); }
        // 隐藏错误消息，然后 compact
    }

    // 3. 主动式 compact：token 使用超过 threshold
    if last_effective_role(messages) == EffectiveRole::User {
        if session.usage.total_tokens > self.over_threshold(tokens) {
            // compact
        }
    }

    // 4. 执行 compact_messages，替换对话
}
```

**第二级：ToolPairCompactionOperation**（管道中的第5个 step）
```rust
// ops_tool_pair_compaction.rs:50-163
async fn run(&self, session, conversation, _emit) -> Result<OperationResult> {
    // 当工具调用次数超过 cutoff 时
    // 将旧的 tool_request + tool_response 对：
    //   1. SetMessageVisibility { agent_visible: false } 隐藏原始消息
    //   2. 调用 LLM 做工具调用摘要
    //   3. 追加摘要消息
}
```

### 6.2 compact_messages 核心算法

```rust
// context_mgmt/mod.rs:78-215
pub async fn compact_messages(provider, model_config, session_id, conversation, manual_compact) {
    // 1. 保留最近的用户消息（非手动 compact 时）
    //    从后往前找：agent_visible + User role + text-only + 非 turn_context
    let (preserved_user_message, preserved_idx, is_most_recent) = ...;

    // 2. 调用 do_compact() 让 LLM 生成摘要
    let (summary_message, summarization_usage) = do_compact(...).await?;

    // 3. 构建最终消息列表：
    //    a. 所有原始消息 → agent_visible = false（保留用户可见）
    //    b. 摘要消息 → agent_only
    //    c. 继续提示 → agent_only
    //    d. 保留的用户消息 → agent_only
    //    e. 携带 turn_context 事件

    // 4. 统计压缩后的 token 数
    let retained_context_tokens = count_context_tokens(&conversation).await?;
}
```

### 6.3 do_compact — 渐进式减量策略

```rust
// context_mgmt/mod.rs:337-422
async fn do_compact(provider, model_config, session_id, messages) {
    // 渐进式尝试：依次移除 0%, 10%, 20%, 50%, 100% 的工具响应消息
    let removal_percentages = [0, 10, 20, 50, 100];

    for (attempt, &remove_percent) in removal_percentages.iter().enumerate() {
        let filtered_messages = filter_tool_responses(&agent_visible_messages, remove_percent);

        // 格式化消息 → 调用 LLM → 检查是否 context_length_exceeded
        match complete_fast(provider, model_config, session_id, &system_prompt, &request, &[]) {
            Ok((mut response, mut provider_usage)) => {
                // 成功 → 应用结构化摘要模板
                apply_structured_summary(&mut response);
                return Ok((response, provider_usage));
            }
            Err(ProviderError::ContextLengthExceeded(_)) => {
                // 上下文仍超限 → 尝试更激进的减量
                continue;
            }
            Err(e) => return Err(e.into());
        }
    }
}
```

**设计亮点**：
- **渐进式**：0% → 10% → 20% → 50% → 100%，每次失败后更激进地移除工具响应
- **中间剔除**（middle-out）：`filter_tool_responses` 从中间向外移除工具响应，保留开头（最近上下文）和结尾（最新操作）
- **结构化摘要**：LLM 返回 JSON，解析后通过模板重新渲染，确保格式可控且节省 token

### 6.4 StructuredSummary

```rust
// context_mgmt/structured.rs:14-38
pub struct StructuredSummary {
    pub user_intent: Vec<String>,           // 用户意图
    pub technical_concepts: Vec<String>,     // 涉及的技术概念
    pub files: Vec<FileActivity>,            // 文件修改记录
    pub errors_and_fixes: Vec<String>,       // 错误和修复
    pub problem_solving: Vec<String>,        // 问题解决过程
    pub user_messages: Vec<String>,          // 用户消息摘要
    pub pending_tasks: Vec<String>,          // 未完成任务
    pub current_work: Option<String>,        // 当前工作
    pub next_step: Option<String>,           // 下一步
}
```

**容错反序列化**：使用 `lenient_string_list` 等自定义反序列化器，即使模型返回了错误类型（如 object 而非 string），也能优雅降级而非丢弃整个摘要。

### 6.5 ToolPairCompaction 的智能去重

```rust
// ops_tool_pair_compaction.rs:92-118
// 关键检查：
// 1. 必须是一对一：一个 tool_request + 一个 tool_response
// 2. 防止重复隐藏：message.id 在 hidden_messages 中 → 跳过
// 3. 兄弟调用检查：同一消息中可能有多个并行工具调用
//    → 如果 request_ids != response_ids，说明有 sibling 分散在不同消息中 → 跳过
```

**对 Jeeves 的启示**：
- 渐进式减量策略（0% → 100%）是处理 LLM 上下文限制的成熟方案
- 结构化摘要 + 容错解析确保了 compaction 的可靠性
- ToolPairCompaction 是对 CompactionOperation 的有效补充——压缩旧工具调用而非整个对话
- 压缩后保留"most recent user message"确保 LLM 不丢失用户最新意图

---

## 7. Provider Usage 追踪

### 7.1 attach_turn_usage

```rust
// agent.rs:330-353
fn attach_turn_usage(
    messages: &mut Conversation,
    usage: &ProviderUsage,
    preferred_message_id: Option<&str>,
) -> Option<(Option<String>, MessageUsage)> {
    // 1. 优先按 preferred_message_id 定位 message_index
    //    rposition: 从后往前找 role=Assistant + id=preferred
    // 2. 降级：从后往前找任意 role=Assistant 的消息
    let message_index = preferred_message_id
        .and_then(|id| {
            messages.messages().iter().rposition(|msg| {
                msg.role == Assistant && msg.id.as_deref() == Some(id)
            })
        })
        .or_else(|| {
            messages.messages().iter()
                .rposition(|msg| msg.role == Assistant)
        })?;

    // 3. 将 usage 数据附加到该消息的 metadata 上
    let message = &mut messages.messages_mut()[message_index];
    let has_user_visible_content = !message.user_visible_content().content.is_empty();
    let message_usage = MessageUsage::from_provider_usage(usage, false);
    message.metadata.usage = Some(Box::new(message_usage.clone()));

    // 4. 只有有用户可见内容的 assistant 消息才对外暴露 usage
    has_user_visible_content.then(|| (message.id.clone(), message_usage))
}
```

### 7.2 状态机中的 Usage 处理

```rust
// usage.rs:9-61
fn attach_to_last_assistant(effects: &mut [StateEffect], usage: &ProviderUsage) {
    // 从后往前找 effects 中最后一个非错误的 assistant AppendMessage
    let message = effects.iter_mut().rev().find_map(|effect| match effect {
        StateEffect::AppendMessage(message)
            if message.role == Assistant && message.error_kind().is_none() => Some(message),
        _ => None,
    })?;
    message.metadata.usage = Some(Box::new(MessageUsage::from_provider_usage(usage, false)));
}

pub(super) fn enrich(session: &Session, effects: &mut [StateEffect]) {
    for effect in effects {
        match extract_usage(effect) {
            Some(usage) => {
                // 估算成本（如果 provider 没报告）
                let (cost, cost_source) = estimate_cost(session, &usage);
                let mut enriched = usage.clone();
                enriched.cost = cost;
                enriched.cost_source = cost_source;
                // 附加到最近的 assistant 消息
                attach_to_last_assistant(effects, &enriched);
                *usage = enriched;
            }
        }
    }
}
```

### 7.3 Usage 的用途

1. **Compaction 触发**：`session.usage.total_tokens` 与 `context_limit * threshold` 比较
2. **UI 展示**：`AgentEvent::MessageUsage` 发送给前端，实时显示 token 消耗
3. **成本估算**：通过 `canonical::maybe_get_canonical_model` 查找定价信息
4. **会话持久化**：`session_manager.record_usage_metrics()` 写入数据库
5. **OpenTelemetry 追踪**：`gen_ai.usage.input_tokens/output_tokens` span 属性

**对 Jeeves 的启示**：
- Usage 应附着在具体消息上（而非全局），便于追踪每次 LLM 调用的成本
- 从后往前找最后一个 assistant 消息是标准模式
- 成本估算作为 provider 报告成本的降级方案

---

## 8. Tool 分类系统

### 8.1 硬编码分类

```rust
// agent.rs:88-104
enum ToolCategory { Shell, Read, Write, Other }

fn categorize_tool(tool_name: &str) -> ToolCategory {
    let local = tool_name.rsplit("__").next().unwrap_or(tool_name);
    match local {
        "shell" | "bash" | "exec" | "run" => ToolCategory::Shell,
        "read" | "view" | "cat" | "read_file" => ToolCategory::Read,
        "write" | "edit" | "patch" | "write_file" | "edit_file" => ToolCategory::Write,
        _ => ToolCategory::Other,
    }
}
```

### 8.2 差异化处理

```rust
// agent.rs:556-593 - emit_pre_tool_extended_hooks()
match categorize_tool(tool_name) {
    ToolCategory::Shell => {
        // 触发 BeforeShellExecution Hook
        // matcher = command 参数值
        let cmd = tool_input.and_then(|v| extract_string_arg(v, &["command"]));
        emit_with_matcher(BeforeShellExecution, session_id, &cmd, tool_name, ...);
    }
    ToolCategory::Read => {
        // 触发 BeforeReadFile Hook
        // matcher = path/file/file_path 参数值
        let path = tool_input.and_then(|v| extract_string_arg(v, &["path", "file", "file_path"]));
        emit_with_matcher(BeforeReadFile, session_id, &path, tool_name, ...);
    }
    ToolCategory::Write | ToolCategory::Other => {}
}

// agent.rs:659-683 - with_post_tool_hook()
match category {
    ToolCategory::Shell => {
        // AfterShellExecution Hook
        // matcher = command
    }
    ToolCategory::Write => {
        // AfterFileEdit Hook
        // matcher = path/file/file_path
    }
    _ => {}
}
```

**设计亮点**：
- 通过 `rsplit("__").next()` 从 `extension__tool` 格式中提取基础工具名
- `extract_string_arg` 支持多键查找（`["command", "cmd", "script", "input"]`）
- 分类主要用于触发**扩展的领域 Hook**（BeforeShellExecution、AfterFileEdit 等）

**对 Jeeves 的启示**：
- 工具分类是触发差异化行为的关键
- 应从 MCP 工具名中提取基础名称
- 分类不应影响功能正确性，只影响 Hook/日志等附加行为

---

## 9. 三层安全检查体系（补充细节）

### 9.1 InspectionAction 决策级联

```rust
// tool_inspection.rs:23-30
pub enum InspectionAction {
    Allow,                          // 直接允许
    Deny,                           // 完全拒绝
    RequireApproval(Option<String>), // 需要用户确认（可附带警告消息）
}
```

**决策合并规则**：多个 Inspector 的结果取最严格的：
- 任何一个返回 `Deny` → 拒绝
- 任何一个返回 `RequireApproval` → 至少需要确认
- 全部 `Allow` → 允许

### 9.2 EgressInspector 覆盖的网络协议

| 检测类型 | 检测命令 | 方向检测 |
|----------|----------|----------|
| HTTP/HTTPS | `curl`, `wget` | POST/PUT → Outbound, GET → Inbound |
| Git | `git push`, `git clone`, `git remote add` | push → Outbound, clone/pull → Inbound |
| S3 | `aws s3 cp`, `s3://` | Sync-aware |
| GCS | `gsutil`, `gs://` | Upload/Download |
| SCP/RSync | `scp`, `rsync` | 目标含 `:` → Outbound |
| SSH | `ssh user@host` | Connection |
| Docker | `docker push`, `docker login` | push/login → Outbound |
| Package | `npm publish`, `cargo publish`, `pip upload`, `twine upload`, `gem push` | Publish → Outbound |
| Generic | `nc`, `ncat`, `netcat`, `socat`, `ftp`, `httpie`, `xh` | Catch-all |

### 9.3 AdversaryInspector 的 Fail-Open 设计

```rust
// adversary_inspector.rs:469-488
Err(e) => {
    // LLM 调用失败 → fail-open（允许通过）
    tracing::warn!(security.action = "ALLOW", "adversary review: error (fail-open)");
    results.push(InspectionResult {
        action: InspectionAction::Allow,
        reason: format!("Adversary error (fail-open): {}", e),
        confidence: 0.0,
    });
}
```

这是安全系统的黄金法则：**可用性优先**。如果一个安全检查器本身出错了，不应阻止用户正常工作。

---

## 10. 与 Hermes Agent 的对比

### 10.1 架构对比

| 维度 | Goose (Rust) | Hermes Agent (Python) |
|------|--------------|----------------------|
| **核心循环** | `StateMachine` 有序管道 | `while True` + `agent_loop()` |
| **扩展机制** | MCP 原生 (ExtensionManager) | MCP 作为工具 (mcp_client) |
| **安全模型** | 三层检查器 + 11 种 Hook + adversary.md | 工具级权限检查 |
| **Compaction** | 渐进式减量 + 结构化摘要 + ToolPair | 基于 token 计数的触发 |
| **状态持久化** | SessionManager + StepResult effects | Session 文件 |
| **并发模型** | async/await (Tokio) + mpsc 通道 | async/await (asyncio) |
| **配置管理** | 环境变量 + Config::global() | YAML config + env |
| **可观察性** | OpenTelemetry + AgentEvent 流 + Langfuse | 日志 + 指标 |

### 10.2 Rust vs Python 性能

| 维度 | Rust (Goose) | Python (Hermes) |
|------|--------------|-----------------|
| 启动速度 | 毫秒级 | 秒级 |
| 工具执行延迟 | 极低（零成本抽象） | 低（asyncio + JSON-RPC） |
| 内存占用 | 低（编译期优化） | 中（GC + 动态类型） |
| Token 计数 | 通过 tiktoken-rs FFI | 通过 tiktoken Python 绑定 |
| MCP 连接 | 直接 rmcp (Rust MCP SDK) | mcp Python SDK |

### 10.3 状态机 vs while 循环

| 维度 | 状态机 (Goose) | while 循环 (Hermes) |
|------|---------------|---------------------|
| 可重入性 | ✅ 天然支持（step 读持久化状态） | ⚠️ 需要手动实现 |
| 可测试性 | ✅ 每个 Operation 独立测试 | ⚠️ 需要 mock 整个循环 |
| 可组合性 | ✅ 新功能 = 新 Operation | ⚠️ 修改循环逻辑 |
| 可观察性 | ✅ 每个 step 可独立追踪 | ⚠️ 需要日志插桩 |
| 学习曲线 | 较高 | 较低 |

### 10.4 MCP 原生 vs 工具级 MCP

| 维度 | MCP 原生 (Goose) | 工具级 MCP (Hermes) |
|------|------------------|---------------------|
| 工具发现 | 连接时自动 list_tools + 缓存 | 每次调用前查询 |
| 生命周期 | 完整管理（启动/停止/重启/OAuth） | 无管理，作为独立工具 |
| 工具命名 | `extension__tool` 格式化 | 原始名称 |
| 凭证管理 | Keyring + 轮转检测 | 环境变量 |
| 通知 | ServerNotification → AgentEvent | 无 |
| 错误恢复 | 自动重连 + 健康检查 | 手动 |

### 10.5 对 Jeeves 的设计建议

1. **引入 Operation Pipeline 模式**（Python 可模拟）：
   ```python
   class Operation(ABC):
       async def run(self, session, conversation, emit) -> OperationResult:
           ...
       # OperationResult: NotApplicable | Applied(effects, yield_to_client)
   ```

2. **建立三层安全检查**：
   - 模式匹配（正则）→ 快速反击已知攻击
   - 出口检测（Egress）→ 监控数据流向
   - LLM 审查（Adversary）→ 语义级理解（可选，fail-open）

3. **Compaction 采用渐进式减量**：先移除 10% 工具响应，不够则 20% → 50% → 100%

4. **MCP 升级为原生**：ExtensionManager 作为独立模块，管理连接、缓存、重连

5. **常量保护体系**：MAX_TURNS、STOP_HOOK_BLOCK_CAP、COMPACTION_THRESHOLD 全部支持环境变量覆盖

6. **Hook 系统**：至少实现 PreToolUse（可阻断）和 PostToolUse（日志/通知）

---

## 附录：关键源码路径索引

| 组件 | 路径 |
|------|------|
| Agent 结构体 | `crates/goose/src/agents/agent.rs:250-274` |
| StateMachine | `crates/goose/src/agents/state_machine/machine.rs` |
| Operation trait | `crates/goose/src/agents/state_machine/operation.rs` |
| Operation 管道构建 | `crates/goose/src/agents/agent.rs:1617-1682` |
| ExtensionManager | `crates/goose/src/agents/extension_manager.rs` |
| Compaction | `crates/goose/src/agents/state_machine/ops_compaction.rs` |
| compact_messages | `crates/goose/src/context_mgmt/mod.rs:78-215` |
| ToolPairCompaction | `crates/goose/src/agents/state_machine/ops_tool_pair_compaction.rs` |
| SecurityInspector | `crates/goose/src/security/security_inspector.rs` |
| EgressInspector | `crates/goose/src/security/egress_inspector.rs` |
| AdversaryInspector | `crates/goose/src/security/adversary_inspector.rs` |
| ToolInspectionManager | `crates/goose/src/tool_inspection.rs` |
| MaxTurns | `crates/goose/src/agents/state_machine/ops_maxturns.rs` |
| StopHook | `crates/goose/src/agents/state_machine/ops_stop_hook.rs` |
| Retry | `crates/goose/src/agents/state_machine/ops_retry.rs` |
| HookManager | `crates/goose/src/hooks/mod.rs` |
| Usage 追踪 | `crates/goose/src/agents/state_machine/usage.rs` |
| StructuredSummary | `crates/goose/src/context_mgmt/structured.rs` |
| PromptManager | `crates/goose/src/agents/prompt_manager.rs` |
| Token 计数 | `crates/goose/src/token_counter.rs` |
