/**
 * 与后端 schema 一一对应。
 *
 * 字段名保持 snake_case，与数据库列和 Pydantic 模型完全一致 ——
 * 不做 camelCase 转换。转换层是跨层 bug 的温床：嵌套结构误转、
 * 后端加字段前端静默丢失、Network 面板字段名和代码对不上。
 */

// ─────────────────────────── 错误 ───────────────────────────

export interface ErrorDetail {
  code: string;
  message: string;
  hint: string | null;
}

// ─────────────────────────── 会话 ───────────────────────────

export interface SessionBrief {
  id: string;
  title: string;
  workspace_id: string;
  pinned: boolean;
  message_count: number;
  last_message_at: number;
  created_at: number;
}

export interface SessionDetail extends SessionBrief {
  /** 这次对话用哪个模型。空串 = 跟随功能位绑定 */
  model_pk: string;
  /** 实际生效的模型窗口。0 表示未配模型 */
  context_window: number;
  /** 这次对话的工作目录。空串 = 未设置 */
  work_dir: string;
  approval_mode: "manual" | "auto";
  private_mode: boolean;
  amnesia_mode: boolean;
  vision_mode: boolean;
}

export interface SessionListResponse {
  items: SessionBrief[];
  total: number;
  page: number;
  size: number;
  pages: number;
}

// ─────────────────────────── 消息 ───────────────────────────

export type MessageRole =
  | "user"
  | "assistant"
  | "tool"
  | "system"
  | "summary"
  | "artifact";

export interface ToolCallRef {
  id: string;
  name: string;
  arguments: string;
}

export interface MessageOut {
  id: string;
  seq: number;
  role: MessageRole;
  agent_name: string;
  content: string;
  reasoning: string | null;
  tool_calls: ToolCallRef[] | null;
  tool_call_id: string | null;
  tool_name: string | null;
  tool_display: Record<string, unknown> | null;
  is_error: boolean;
  refs: Record<string, unknown>[] | null;
  attachments: string[] | null;
  artifact_kind: string | null;
  artifact_path: string | null;
  run_id: string | null;
  span_id: string | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  created_at: number;
}

// ─────────────────────────── SSE 事件 ───────────────────────────

/**
 * 所有事件都带的公共字段。
 * 前端靠 span_id / parent_span_id / depth 把扁平事件流还原成气泡树。
 */
export interface EventCommon {
  ts: number;
  span_id: string | null;
  parent_span_id: string | null;
  depth: number;
}

export type SseEventName =
  | "meta"
  | "message"
  | "thinking"
  | "tool_start"
  | "tool_end"
  | "agent_start"
  | "agent_end"
  | "todo_updated"
  | "context_usage"
  | "approval_required"
  | "approval_resolved"
  | "compacting"
  | "compacted"
  | "artifact_updated"
  | "memory_recalled"
  | "approval_required"
  | "interact_required"
  | "artifact"
  | "title"
  | "model_fallback"
  | "cancelled"
  | "error"
  | "ping"
  | "done";

export interface MetaEvent extends EventCommon {
  run_id: string;
  session_id: string;
  user_message_id: string;
  assistant_message_id: string | null;
}

export interface DeltaEvent extends EventCommon {
  delta: string;
}

export interface ToolStartEvent extends EventCommon {
  /** 发起这次调用的智能体。空串是主智能体 */
  agent_name: string;
  call_id: string;
  tool_name: string;
  args: Record<string, unknown>;
}

export interface ToolEndEvent extends EventCommon {
  /** 发起这次调用的智能体。空串是主智能体 */
  agent_name: string;
  call_id: string;
  tool_name: string;
  is_error: boolean;
  duration_ms: number;
  display: Record<string, unknown> | null;
  /** 完整内容已落库，需要时拉 message 接口。工具输出可能几万字符 */
  content_preview: string;
}

export interface AgentStartEvent extends EventCommon {
  agent_name: string;
  task: string | null;
}

export interface AgentEndEvent extends EventCommon {
  agent_name: string;
  stop_reason: string;
  turns: number;
  prompt_tokens: number;
  completion_tokens: number;
}

export interface ContextUsageEvent extends EventCommon {
  used_tokens: number;
  window_tokens: number;
  ratio: number;
  compact_at: number;
  /** true 表示本地估算（上游没返回 usage），UI 要标注出来 */
  is_estimate: boolean;
  /**
   * 工具定义占的 token。每一轮都重发。
   *
   * 必须显示出来 —— 发一句"你好"看到 4551 token 时，用户唯一的解释
   * 是"计数错了"。实际是 18 个工具的 schema 占了 4298。
   */
  tools_tokens?: number;
  /** 系统提示词占的 token。同样每轮重发 */
  system_tokens?: number;
  tool_count?: number;
}

export interface TodoItem {
  id: string;
  content: string;
  status: "pending" | "in_progress" | "completed" | "cancelled";
  priority: "high" | "medium" | "low";
  order_index: number;
  archived_at: number | null;
  created_at: number;
}

export interface TodoStats {
  total: number;
  pending: number;
  in_progress: number;
  completed: number;
  cancelled: number;
}

export interface TodoUpdatedEvent extends EventCommon {
  items: TodoItem[];
  stats: TodoStats;
}

export interface TitleEvent extends EventCommon {
  session_id: string;
  title: string;
}

export interface ModelFallbackEvent extends EventCommon {
  purpose: string;
  requested: string;
  used: string;
  reason: string;
}

export interface ErrorEvent extends EventCommon {
  code: string;
  message: string;
  hint: string | null;
  retryable: boolean;
}

export interface CancelledEvent extends EventCommon {
  run_id: string;
  partial_saved: boolean;
}

export interface DoneEvent extends EventCommon {
  run_id: string;
  status: "done" | "cancelled" | "error";
}

/** 事件名 → 载荷类型的映射，供 onEvent 做窄化 */
export interface ApprovalRequiredEvent extends EventCommon {
  call_id: string;
  tool_name: string;
  /**
   * 完整参数。只给工具名等于让用户盲签 ——
   * 审批一条命令时必须能看到命令原文。
   */
  args: Record<string, unknown>;
  /**
   * 截止时刻（毫秒时间戳），不是剩余秒数。
   *
   * 用绝对时刻是因为倒计时要算的是"还剩多久"，而事件到达前端有网络延迟、
   * 标签页切到后台还会被节流。给剩余秒数的话前端得自己记事件到达时间，
   * 而那个时间本身就已经偏了。
   */
  timeout_at: number;
}

export interface ApprovalResolvedEvent extends EventCommon {
  call_id: string;
  approved: boolean;
  reason?: string;
}

export interface CompactingEvent extends EventCommon {
  victim_count: number;
}

export interface CompactedEvent extends EventCommon {
  before_tokens: number;
  after_tokens: number;
  victim_count: number;
  summary_chars: number;
}

export interface ArtifactUpdatedEvent extends EventCommon {
  kind: string;
  path: string | null;
  chars: number;
}

export interface SseEventMap {
  meta: MetaEvent;
  message: DeltaEvent;
  thinking: DeltaEvent;
  tool_start: ToolStartEvent;
  tool_end: ToolEndEvent;
  agent_start: AgentStartEvent;
  agent_end: AgentEndEvent;
  todo_updated: TodoUpdatedEvent;
  context_usage: ContextUsageEvent;
  approval_required: ApprovalRequiredEvent;
  approval_resolved: ApprovalResolvedEvent;
  compacting: CompactingEvent;
  compacted: CompactedEvent;
  artifact_updated: ArtifactUpdatedEvent;
  memory_recalled: MemoryRecalledEvent;
  interact_required: EventCommon & {
    call_id: string;
    question: string;
    options: string[] | null;
    multi: boolean;
  };
  artifact: EventCommon & { kind: string; path: string | null };
  title: TitleEvent;
  model_fallback: ModelFallbackEvent;
  cancelled: CancelledEvent;
  error: ErrorEvent;
  ping: EventCommon;
  done: DoneEvent;
}

// ─────────────────────────── 供应商 ───────────────────────────

export interface ProbedModel {
  model_id: string;
  context_window: number;
  /** matched | manual | default。default 要在 UI 上提示用户手动确认 */
  window_source: "matched" | "manual" | "default";
  /** 名字看起来不是对话模型（嵌入/TTS/重排等），默认不勾选 */
  looks_non_chat: boolean;
}

export interface ProbeResponse {
  /** 规范化后的地址。要回显 —— 用户填的可能被改过（补了 /v1） */
  normalized_base_url: string;
  models: ProbedModel[];
}

export interface ProviderOut {
  id: string;
  name: string;
  base_url: string;
  /** 只有尾 4 位，永远没有明文 */
  key_hint: string;
  enabled: boolean;
  model_count: number;
  last_probe_at: number | null;
  created_at: number;
}

export interface ModelOut {
  id: string;
  provider_id: string;
  model_id: string;
  display_name: string;
  context_window: number;
  window_source: string;
  supports_vision: "true" | "false" | "unknown";
  supports_tools: "true" | "false" | "unknown";
  enabled: boolean;
}

export type Purpose = "chat" | "vision" | "title" | "compact" | "embedding";

export interface BindingOut {
  id: string;
  agent_name: string;
  purpose: Purpose;
  model_pk: string;
  model_id: string;
  provider_name: string;
}

// ─────────────────────────── 元信息 ───────────────────────────

export interface MetaResponse {
  version: string;
  sandbox_backend: string;
  sandbox_docker_available: boolean;
  websearch_backend: string;
  has_chat_model: boolean;
  /**
   * 配了 docker 但降级到本地执行的原因。空表示没降级。
   *
   * 非空时必须【持续显示】提示条（不是一闪而过的 toast）——
   * 用户配了 docker 就是想要隔离，静默用本地执行的话他会以为命令跑在
   * 容器里，于是放心地让 agent 执行危险操作。
   */
  sandbox_fallback_reason: string;
  /** 当前后端是否真隔离。local 后端为 false */
  sandbox_isolated: boolean;
  /** false 时显示无鉴权警示条 */
  host_is_localhost: boolean;
  skill_count: number;
  macro_count: number;
  mcp_tool_count: number;
  tool_names: string[];
}

/** 执行树的一个节点。落库的 span 与 SSE 气泡树结构同源 */
export interface TraceSpan {
  span_id: string;
  parent_span_id: string | null;
  depth: number;
  kind: "llm" | "tool" | "agent" | "compaction";
  name: string;
  agent_name: string;
  status: string;
  started_at: number;
  duration_ms: number | null;
  model_id: string;
  total_tokens: number;
  cost_usd: number;
  /** 单价是否配过。用来区分"零成本"和"没配价" */
  has_price: boolean;
  input_preview: string;
  input_truncated: boolean;
  input_bytes: number;
  output_preview: string;
  output_truncated: boolean;
  output_bytes: number;
  error: string;
  children: TraceSpan[];
}

/** 一条长期记忆 */
export interface MemoryOut {
  id: string;
  content: string;
  theme: string;
  hit: number;
  /** 置信度。用户手写 1.0，模型调工具记 0.8，后台自动提炼 0.6 */
  confidence: number;
  /** manual（用户手写）/ tool（模型主动记）/ auto（自动提炼） */
  source: string;
  origin_session_id: string;
  archived: boolean;
  updated_at: number;
  /**
   * 变更历史。排查"AI 为什么以为我喜欢 X"时这是唯一线索 ——
   * 少见实现做了这个设计
   */
  history: {
    op: string;
    reason: string;
    before: Record<string, unknown>;
    at: number;
  }[];
}

/** 记忆被召回的事件 */
export interface MemoryRecalledEvent extends EventCommon {
  count: number;
  items: {
    memory_id: string;
    theme: string;
    content: string;
    score: number;
  }[];
}

/** 定时任务。 */
export interface CronTask {
  id: string;
  name: string;
  prompt: string;
  cron: string;
  /** 表达式的中文描述。显示 "0 9 * * *" 的话用户每次都要在心里解析一遍 */
  cron_text: string;
  timezone: string;
  workspace_id: string;
  enabled: boolean;
  /** skip | run_once。错过窗口时的处理策略 */
  on_missed: string;
  last_fired_at: number;
  next_fire_at: number;
  run_count: number;
  fail_count: number;
  created_at: number;
}

/** 一次执行记录。 */
export interface CronRun {
  id: string;
  task_id: string;
  scheduled_at: number;
  started_at: number;
  finished_at: number;
  /** ok | failed | missed | running */
  status: string;
  detail: string;
  /** 关联的会话。用户点进去能看 agent 做了什么 */
  session_id: string;
}

export interface CronValidateResult {
  valid: boolean;
  error: string;
  text?: string;
  /** 接下来 5 次触发时间（毫秒） */
  next: number[];
}

// ── 文件访问 ──

export interface WhitelistItem {
  id: string;
  path: string;
  can_write: boolean;
  note: string;
  /** 内置条目不可删、权限不可改 —— 删了 agent 就完全不能读写文件 */
  builtin: boolean;
  /** null = 全局条目，对所有会话生效 */
  session_id: string | null;
  /**
   * 路径当前是否真实存在。
   *
   * 必须显示出来 —— 目录被移走时白名单还在但工具会失败，
   * 而错误信息只说"路径不在白名单内"，指向完全错误的方向。
   */
  exists: boolean;
}

export interface BrowseEntry {
  name: string;
  path: string;
  is_dir: boolean;
}

export interface BrowseResult {
  path: string;
  parent: string | null;
  entries: BrowseEntry[];
  /** 常用起点：盘符、主目录、项目目录 */
  roots: BrowseEntry[];
}

// ── 模型与人格 ──

export interface ModelItem {
  id: string;
  provider_id: string;
  /** 供应商名，用于菜单里显示"供应商 / 模型" */
  provider_name: string;
  model_id: string;
  display_name: string;
  context_window: number;
  window_source: string;
  supports_vision: string;
  supports_tools: string;
  /** 禁用的不出现在对话页切换菜单里，但配置保留 */
  enabled: boolean;
  price_in_per_1m: number | null;
  price_out_per_1m: number | null;
}

export interface PersonaFile {
  key: string;
  filename: string;
  label: string;
  hint: string;
  content: string;
  exists: boolean;
}
