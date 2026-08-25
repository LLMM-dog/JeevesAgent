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
  /** 这次对话用哪个智能体。空串 = 未选择（直接用模型对话） */
  agent_id: string;
  /** 实际生效的模型窗口。0 表示未配模型 */
  context_window: number;
  /** 这次对话的工作目录。空串 = 未设置 */
  work_dir: string;
  approval_mode: "manual" | "auto";
  private_mode: boolean;
  amnesia_mode: boolean;
  vision_mode: boolean;
  /** 会话级流式开关（LLM stream 参数） */
  stream_enabled: boolean;
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
  | "summary";

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
  | "refs_expanded"
  | "interact_required"
  | "sandbox_fallback"
  | "mcp_unavailable"
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
  /**
   * 引用展开结果。
   *
   * failures 非空时必须让用户看到 —— 他打了 @文件 却没生效的话，
   * 不提示的话他只会觉得"AI 没看我给的文件"，而不知道是引用失败了。
   */
  refs_expanded: {
    ok: number;
    failures: string[];
    bytes_used: number;
    skills?: string[];
  } & EventCommon;

  /**
   * 下面两个枚举里有定义但后端目前不 emit —— 降级提示最后改成走
   * /api/meta 的字段（前端轮询读）。留着声明是为了将来启用时
   * TS 能立刻检查到漏处理的地方。
   */
  sandbox_fallback: { reason: string } & EventCommon;
  mcp_unavailable: { server_id: string; reason: string } & EventCommon;

  interact_required: EventCommon & {
    call_id: string;
    question: string;
    options: string[] | null;
    multi: boolean;
  };
  title: TitleEvent;
  model_fallback: ModelFallbackEvent;
  cancelled: CancelledEvent;
  error: ErrorEvent;
  ping: EventCommon;
  done: DoneEvent;
}

// ─────────────────────────── 端点 ───────────────────────────

export interface ProbedModel {
  model_id: string;
  context_window: number;
  /** matched | manual | default。default 要在 UI 上提示用户手动确认 */
  window_source: "matched" | "manual" | "default";
  /** 名字看起来不是对话模型（嵌入/TTS/重排等），默认不勾选 */
  looks_non_chat: boolean;
  /** 模型类型：chat / reasoning / embedding / ...，探测时按名字推断 */
  model_type: string;
}

export interface ProbeResponse {
  /** 规范化后的地址。要回显 —— 用户填的可能被改过（补了 /v1） */
  normalized_base_url: string;
  /** 从地址推断的分组名，用于"添加模型"自动分组 */
  suggested_name: string;
  models: ProbedModel[];
}

export interface EndpointOut {
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

/** 模型类型。chat 之外的都是按名字启发式判定，用户可改。 */
export type ModelType =
  | "chat"
  | "reasoning"
  | "embedding"
  | "rerank"
  | "tts"
  | "audio"
  | "image";

export interface ModelOut {
  id: string;
  endpoint_id: string;
  endpoint_name: string;
  model_id: string;
  display_name: string;
  context_window: number;
  window_source: string;
  supports_vision: "true" | "false" | "unknown";
  supports_tools: "true" | "false" | "unknown";
  model_type: ModelType;
  enabled: boolean;
  price_in_per_1m: number | null;
  price_out_per_1m: number | null;
  /** 被绑定到的功能位（purpose）列表，如 ["chat", "memory"] */
  bindings: string[];
}

export type Purpose =
  | "chat"
  | "vision"
  | "title"
  | "compact"
  | "embedding"
  | "memory"
  | "memory_rerank";

export interface BindingOut {
  id: string;
  agent_name: string;
  purpose: Purpose;
  model_pk: string;
  model_id: string;
  endpoint_name: string;
}

// ─────────────────────────── 元信息 ───────────────────────────

// ── 鉴权 ──

export interface AuthMeResponse {
  auth_enabled: boolean;
  authenticated: boolean;
  username: string;
  is_admin: boolean;
  session_ttl_days: number;
}

export interface LoginResponse {
  authenticated: boolean;
  username: string;
  is_admin: boolean;
  token: string;
}

export interface UserItem {
  id: string;
  username: string;
  is_admin: boolean;
  enabled: boolean;
  created_at: number;
  last_login_at: number;
}

// ── 部署 ──

export interface DeployStatus {
  host: string;
  port: number;
  is_localhost: boolean;
  auth_enabled: boolean;
  https: boolean;
}

export interface TailscaleStatus {
  installed: boolean;
  installed_hint?: string;
  /** 是否使用项目目录里的便携版（随项目删除即卸载） */
  bundled?: boolean;
  /** 待授权的登录链接（后台 tailscale login 抓到的） */
  login_url?: string;
  /** tailscaled 的错误输出（展示给用户，避免静默失败） */
  daemon_error?: string;
  backend_state?: string;
  logged_in?: boolean;
  device_name?: string;
  ipv4?: string;
  serve?: {
    serve_on: boolean;
    funnel_on: boolean;
  };
}

export interface TailscaleAction {
  ok: boolean;
  detail: string;
  status: TailscaleStatus;
}

export interface CpolarStatus {
  installed: boolean;
  authtoken_configured: boolean;
  running: boolean;
  url: string;
}

export interface CpolarAction {
  ok: boolean;
  detail: string;
  status: CpolarStatus;
}

export interface DeploySettingItem {
  key: string;
  section: string;
  type: "int" | "float" | "bool" | "str";
  label: string;
  hint: string;
  min: number | null;
  max: number | null;
  restart: boolean;
  value: number | boolean | string;
}

export interface DeploySettingsResponse {
  items: DeploySettingItem[];
}

export interface EnableAuthResponse {
  username: string;
  token: string;
}

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
  /** 远程访问鉴权是否开启。true 且未登录时前端显示登录页。 */
  auth_enabled: boolean;
  skill_count: number;
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

// ── 模型 ──

export interface ModelItem {
  id: string;
  endpoint_id: string;
  /** 端点名，用于菜单里显示"端点 / 模型" */
  endpoint_name: string;
  model_id: string;
  display_name: string;
  context_window: number;
  window_source: string;
  supports_vision: "true" | "false" | "unknown";
  supports_tools: "true" | "false" | "unknown";
  /** 模型类型，前端用图标显示 */
  model_type: ModelType;
  /** 禁用的不出现在对话页切换菜单里，但配置保留 */
  enabled: boolean;
  price_in_per_1m: number | null;
  price_out_per_1m: number | null;
  /** 被绑定到的功能位（purpose）列表，如 ["chat", "memory"] */
  bindings: string[];
}

// ── 智能体 ──

export interface AgentItem {
  id: string;
  name: string;
  description: string;
  avatar: string | null;
  hidden: boolean;
  is_default: boolean;
  permission_read: boolean;
  permission_write: boolean;
  permission_shell: boolean;
  permission_network: boolean;
  permission_subagent: boolean;
  system_prompt: string;
  model_id: string | null;
  skill_names: string[];
  mcp_servers: string[];
  /** 额外 LLM 参数（如 thinking: {"type": "disabled"}），解析后透传给上游 */
  extra_llm_params: string;
  created_at: number;
  updated_at: number;
}

// ── 记忆 ──

/**
 * 一个可调设置项。类型/范围/说明都由后端给 ——
 * 前端硬编码这份列表的话，后端加一项两边就不同步了。
 */
export interface MemorySettingItem {
  key: string;
  type: "int" | "float" | "bool" | "str";
  label: string;
  hint: string;
  min: number | null;
  max: number | null;
  value: number | boolean | string;
  /** 归属的设置页：memory / websearch。记忆页只渲染 memory 项 */
  section: string;
}

/** 向量新鲜度。三种失效原因分开，因为用户的处理方式不同。 */
export interface MemoryVectorStatus {
  total: number;
  /** 从没算过（新记忆，或刚配上嵌入模型） */
  never: number;
  /** 换了嵌入模型，旧向量已停止参与召回 */
  model: number;
  /** 记忆改过但向量没跟上 */
  content: number;
  fresh: number;
  embedding_configured: boolean;
  embedding_model: string;
}

export interface MemoryRebuildResult {
  attempted: number;
  succeeded: number;
  skipped: number;
  model: string;
  dim: number;
  errors: string[];
}

export interface MemorySearchHit {
  uri: string;
  memory_type: string;
  title: string;
  score: number;
  scope: string;
}

export interface MemorySearchResult {
  query: string;
  hits: MemorySearchHit[];
  /** 空结果有两种原因，前端要能区分：没配模型 vs 确实没有相关记忆 */
  embedding_configured: boolean;
}

/** 记忆列表项（/memory/list 返回的元数据，不含正文） */
export interface MemoryListItem {
  uri: string;
  scope: string;
  memory_type: string;
  agent_id: string;
  session_id: string;
  peer_agent_id: string;
  title: string;
  version: number;
  /** 召回命中次数，热度分的一部分 */
  active_count: number;
  updated_at: number;
}

/** 记忆完整内容（/memory/read 返回） */
export interface MemoryItem {
  uri: string;
  memory_type: string;
  /** 渲染后的正文（去掉 frontmatter） */
  body: string;
  /** 原始文件内容（含 frontmatter） */
  raw_content: string;
  /** 业务字段（由 schema 定义） */
  fields: Record<string, unknown>;
  version: number;
  created_at: number;
  updated_at: number;
}

/** 记忆列表响应 */
export interface MemoryListResponse {
  items: MemoryListItem[];
  total: number;
}

/** 记忆写入请求 */
export interface MemoryWriteRequest {
  agent_id?: string;
  session_id?: string;
  memory_type: string;
  fields: Record<string, unknown>;
}

/** 记忆写入响应 */
export interface MemoryWriteResponse {
  uri: string;
  version: number;
  /** true = 新建，false = 更新 */
  created: boolean;
}

/** 向量化请求 */
export interface MemoryVectorizeRequest {
  uris: string[];
}

/** 向量化响应 */
export interface MemoryVectorizeResponse {
  attempted: number;
  succeeded: number;
  skipped: number;
  model: string;
  dim: number;
  errors: string[];
}

// ── 记忆痕迹 ──

/** 一次记忆提取的痕迹（.trace/ 下的 JSON 文件） */
export interface MemoryTrace {
  extraction_id: string;
  /** 痕迹链 ID（多次提取属于同一次会话提交） */
  trace_id: string;
  /** 提取时间戳（毫秒） */
  extracted_at: number;
  /** 文件名（列表接口附带） */
  file?: string;
  written: { uri: string }[];
  edited: { uri: string }[];
  unchanged: { uri: string }[];
  failed: { uri: string; error: string }[];
  deletes: { uri: string; memory_type: string; deleted_content: string }[];
  errors: string[];
  summary: {
    total_adds: number;
    total_updates: number;
    total_deletes: number;
    total_unchanged: number;
    total_errors: number;
  };
  /** 详情里的完整操作记录（含 before/after 正文） */
  operations?: {
    adds: { uri: string; memory_type: string; after: string }[];
    updates: { uri: string; memory_type: string; before: string; after: string }[];
    unchanged: { uri: string; memory_type: string }[];
    failed: { uri: string; memory_type: string; error: string }[];
    deletes: { uri: string; memory_type: string; deleted_content: string }[];
  };
}

export interface MemoryTraceListResponse {
  traces: MemoryTrace[];
  total: number;
}
