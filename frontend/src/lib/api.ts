/**
 * REST 客户端。
 *
 * 所有请求走同一个 request()，错误统一转成 ApiError ——
 * 组件层只需处理一种错误形状。
 */

import { ApiError } from "./sse";
import type {
  BindingOut,
  BrowseResult,
  CronRun,
  CronTask,
  CronValidateResult,
  MemoryOut,
  MessageOut,
  MetaResponse,
  ModelItem,
  ModelOut,
  PersonaFile,
  ProbeResponse,
  ProviderOut,
  Purpose,
  SessionDetail,
  SessionListResponse,
  TodoItem,
  TodoStats,
  TraceSpan,
  WhitelistItem,
} from "./types";

const BASE = "/api";

async function request<T>(
  path: string,
  init?: RequestInit & { json?: unknown },
): Promise<T> {
  const { json, ...rest } = init ?? {};
  const headers = new Headers(rest.headers);
  if (json !== undefined) headers.set("Content-Type", "application/json");

  const resp = await fetch(`${BASE}${path}`, {
    ...rest,
    headers,
    body: json !== undefined ? JSON.stringify(json) : rest.body,
  });

  if (!resp.ok) {
    let code = "http_error";
    let message = `HTTP ${resp.status}`;
    let hint: string | null = null;
    try {
      const body = await resp.json();
      const d = body?.detail;
      if (d && typeof d === "object") {
        code = d.code ?? code;
        message = d.message ?? message;
        hint = d.hint ?? null;
      } else if (typeof d === "string") {
        message = d;
      }
    } catch {
      /* 非 JSON 响应 */
    }
    throw new ApiError(resp.status, code, message, hint);
  }

  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

/**
 * 下载文件。
 *
 * ## 为什么不能用 request()
 *
 * request() 结尾是 `resp.json()` —— 导出返回的是 Markdown 文本或带
 * Content-Disposition 的文件流，用它会在解析 JSON 时抛错。
 *
 * ## 为什么不直接 window.open(url)
 *
 * 那样错误无法处理：后端返回 404/500 时浏览器会打开一个显示错误 JSON
 * 的新标签页，而不是走应用的错误提示。用 fetch + blob 能保留统一的
 * ApiError 处理。
 *
 * ## 文件名从响应头取
 *
 * 后端已经做了文件名清理（模型生成的标题可能含 Windows 非法字符）和
 * RFC 5987 编码。前端自己拼名字等于把那套逻辑写第二遍。
 */
async function download(path: string, fallbackName: string): Promise<void> {
  const resp = await fetch(`${BASE}${path}`);
  if (!resp.ok) {
    let code = "http_error";
    let message = `HTTP ${resp.status}`;
    let hint: string | null = null;
    try {
      const body = await resp.json();
      const d = body?.detail;
      if (d && typeof d === "object") {
        code = d.code ?? code;
        message = d.message ?? message;
        hint = d.hint ?? null;
      }
    } catch {
      /* 非 JSON 响应 */
    }
    throw new ApiError(resp.status, code, message, hint);
  }

  // 优先用 filename*=UTF-8''（真名），退回 filename=（ASCII 兜底名）
  const disp = resp.headers.get("content-disposition") ?? "";
  let name = fallbackName;
  const utf8 = /filename\*=UTF-8''([^;]+)/i.exec(disp);
  if (utf8) {
    try {
      name = decodeURIComponent(utf8[1]);
    } catch {
      /* 编码坏了就用兜底名 */
    }
  } else {
    const plain = /filename="?([^";]+)"?/i.exec(disp);
    if (plain) name = plain[1];
  }

  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // 必须释放，否则 blob 会一直占内存直到页面刷新
  URL.revokeObjectURL(url);
}

// ─────────────────────────── 会话 ───────────────────────────

export const api = {
  meta: () => request<MetaResponse>("/meta"),

  listSessions: (params?: { page?: number; size?: number; q?: string }) => {
    const q = new URLSearchParams();
    if (params?.page) q.set("page", String(params.page));
    if (params?.size) q.set("size", String(params.size));
    if (params?.q) q.set("q", params.q);
    const suffix = q.toString() ? `?${q}` : "";
    return request<SessionListResponse>(`/sessions${suffix}`);
  },

  createSession: (body: { title?: string; workspace_id?: string } = {}) =>
    request<SessionDetail>("/sessions", { method: "POST", json: body }),

  getSession: (id: string) => request<SessionDetail>(`/sessions/${id}`),

  /**
   * 核验模型的图片输入能力。
   *
   * 会发一次真实的多模态请求 —— 模型列表接口不返回这个信息，
   * 模型名也看不出来（gpt-4o-mini 支持、deepseek-chat 不支持，
   * 两个名字里都没有 vision 字样）。
   */
  verifyVision: (modelPk: string) =>
    request<{
      model_pk: string;
      model_id: string;
      supports_vision: "true" | "false" | "unknown";
      checked_at: number | null;
      detail: string;
    }>(`/models/${modelPk}/verify-vision`, { method: "POST" }),

  uploadImage: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<{
      data_url: string;
      mime: string;
      bytes: number;
      filename: string;
    }>("/images/upload", { method: "POST", body: fd });
  },

  /**
   * 引用候选。
   *
   * 文件候选【必须后端搜】—— 文件可能上万个，前端拉全量不现实。
   * 后端会跳过 node_modules / .venv 这类目录，否则候选列表会被淹掉。
   */
  refCandidates: (
    kind: "file" | "skill" | "tool" | "macro",
    q: string,
    sessionId?: string,
  ) =>
    request<{
      items: { name: string; path?: string; detail: string }[];
      // 文件搜索时后端会带上这两个字段：没设工作目录 vs 目录里真的没有匹配。
      // 少了它们前端只能一律显示"没有匹配的文件"，而用户根本不知道
      // 要先去设工作目录。
      reason?: string;
      hint?: string;
    }>(
      `/ref-candidates?kind=${kind}&q=${encodeURIComponent(q)}` +
        (sessionId ? `&session_id=${encodeURIComponent(sessionId)}` : ""),
    ),

// ── 模型个体管理 ──

  /**
   * 模型列表。
   *
   * enabledOnly 给对话页的切换菜单用 —— 设置页要看到全部（含禁用的），
   * 否则用户没法把它重新启用。
   */
  models: (opts?: { providerId?: string; enabledOnly?: boolean }) => {
    const q = new URLSearchParams();
    if (opts?.providerId) q.set("provider_id", opts.providerId);
    if (opts?.enabledOnly) q.set("enabled_only", "true");
    const s = q.toString();
    return request<{ items: ModelItem[] }>(`/models${s ? `?${s}` : ""}`);
  },

/**
   * 拉这个供应商可用的模型列表。
   *
   * 用已存的端点和 Key —— 往【已有】供应商下加模型时，让用户再填一遍
   * base_url 和 Key 是荒谬的（而且 Key 存的是密文，前端拿不到明文）。
   *
   * already_added 标出已加过的，前端置灰。不标的话用户点了才知道重复。
   */
/**
   * 固定上下文开销（工具定义 + 系统提示词）。
   *
   * 这两项在发消息之前就确定了，而"还剩多少空间粘代码"恰恰是发消息
   * 之前的问题。只靠 run 期间的 context_usage 事件的话，
   * 切一次页面就只剩对话内容那一段。
   */
  contextOverhead: (sessionId?: string) =>
    request<{
      tools_tokens: number;
      system_tokens: number;
      tool_count: number;
      window_tokens: number;
      is_estimate: boolean;
    }>(
      "/context-overhead" +
        (sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ""),
    ),

  availableModels: (providerId: string) =>
    request<{
      items: {
        model_id: string;
        context_window: number;
        window_source: string;
        looks_non_chat: boolean;
        already_added: boolean;
      }[];
    }>(`/providers/${providerId}/available-models`),

  addModel: (body: {
    provider_id: string;
    model_id: string;
    display_name?: string;
    context_window?: number;
  }) => request<ModelItem>("/models", { method: "POST", json: body }),

  patchModel: (
    pk: string,
    body: { enabled?: boolean; display_name?: string; context_window?: number },
  ) =>
    request<ModelItem>(`/models/${pk}`, {
      method: "PATCH",
      json: body,
    }),

  deleteModel: (pk: string) =>
    request<{ ok: boolean }>(`/models/${pk}`, { method: "DELETE" }),

  // ── 人格与偏好 ──

  personas: () => request<{ items: PersonaFile[] }>("/personas"),

  savePersona: (key: string, content: string) =>
    request<PersonaFile>(`/personas/${key}`, {
      method: "PUT",
      json: { content },
    }),

  resetPersona: (key: string) =>
    request<PersonaFile>(`/personas/${key}/reset`, { method: "POST" }),

  // ── 文件访问：白名单与目录浏览 ──

  /**
   * 白名单列表。
   *
   * 带 sessionId 时返回【该会话实际生效的集合】—— 会话级条目加全局条目。
   * 界面显示的必须和 agent 用的一致，否则用户看着白名单里有某个目录，
   * 却不明白为什么工具还是被拒。
   */
  whitelist: (sessionId?: string) =>
    request<{ items: WhitelistItem[] }>(
      "/whitelist" + (sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ""),
    ),

  addWhitelist: (
    body: { path: string; can_write: boolean; note?: string },
    sessionId?: string,
  ) =>
    request<WhitelistItem>(
      "/whitelist" + (sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ""),
      { method: "POST", json: body },
    ),

  patchWhitelist: (id: string, body: { can_write?: boolean; note?: string }) =>
    request<WhitelistItem>(`/whitelist/${id}`, {
      method: "PATCH",
      json: body,
    }),

  deleteWhitelist: (id: string) =>
    request<{ ok: boolean }>(`/whitelist/${id}`, { method: "DELETE" }),

  /**
   * 浏览目录，供工作目录选择器用。
   *
   * path 留空返回常用起点（盘符、主目录、项目目录）—— 让用户不用手打
   * 绝对路径。Windows 路径又长又容易打错，打错了得到"目录不存在"，
   * 试几次就放弃了。
   */
  browse: (path?: string, dirsOnly = true) =>
    request<BrowseResult>(
      `/browse?dirs_only=${dirsOnly}` +
        (path ? `&path=${encodeURIComponent(path)}` : ""),
    ),

  /**
   * 导出会话。
   *
   * fmt=markdown 给人读（工具调用折叠），json 用于备份迁移（保留全部字段）。
   */
  exportSession: (id: string, fmt: "markdown" | "json") =>
    download(
      `/sessions/${id}/export?fmt=${fmt}`,
      `session_${id}.${fmt === "json" ? "json" : "md"}`,
    ),

  // ---- 联网搜索 ----

  websearch: () =>
    request<{
      backend: string;
      has_tavily_key: boolean;
      registered: boolean;
      ddg_available: boolean;
      tavily_available: boolean;
    }>("/websearch"),

  setWebsearch: (backend: string, tavily_api_key?: string) =>
    request<{
      backend: string;
      registered: boolean;
      tools: string[];
      persisted: boolean;
      persist_hint: string;
    }>("/websearch", { method: "PUT", json: { backend, tavily_api_key } }),

  // ---- 定时任务 ----

  cronTasks: () =>
    request<{
      items: CronTask[];
      scheduler_loaded: number;
      scheduler_inflight: number;
    }>("/cron/tasks"),

  createCronTask: (body: {
    name?: string;
    prompt: string;
    cron: string;
    timezone?: string;
    on_missed?: string;
    enabled?: boolean;
  }) => request<CronTask>("/cron/tasks", { method: "POST", json: body }),

  patchCronTask: (id: string, body: Partial<CronTask>) =>
    request<CronTask>(`/cron/tasks/${id}`, { method: "PATCH", json: body }),

  deleteCronTask: (id: string) =>
    request<{ ok: boolean }>(`/cron/tasks/${id}`, { method: "DELETE" }),

  cronRuns: (id: string) =>
    request<{ items: CronRun[] }>(`/cron/tasks/${id}/runs`),

  /** 手动触发一次。定时任务的反馈周期很长，调试期需要它 */
  runCronTask: (id: string) =>
    request<{ ok: boolean; scheduled_at: number }>(`/cron/tasks/${id}/run`, {
      method: "POST",
    }),

  /** 校验并预览接下来 5 次触发时间 */
  validateCron: (cron: string, timezone = "") =>
    request<CronValidateResult>("/cron/validate", {
      method: "POST",
      json: { cron, timezone },
    }),

  // ---- MCP ----

  mcpServers: () =>
    request<{
      items: {
        server_id: string;
        transport: string;
        status: string;
        error: string;
        tool_count: number;
        tools: { name: string; raw_name: string; description: string }[];
        estimated_tokens: number;
        connected_at: number | null;
        /** 用户关掉的服务器不连接，工具定义不占上下文 */
        enabled?: boolean;
      }[];
      config_errors: string[];
    }>("/mcp/servers"),

  /**
   * 待确认的 stdio 启动命令。
   *
   * 本地 MCP 服务器等于任意代码执行 —— 规范要求执行前必须让用户看到
   * 完整命令（不截断）并确认。
   */
  mcpPending: () =>
    request<{
      items: {
        server_id: string;
        command: string;
        cwd: string;
        env_keys: string[];
        warnings: { pattern: string; reason: string }[];
      }[];
    }>("/mcp/pending-approval"),

  mcpReload: () =>
    request<{
      servers: number;
      ready: number;
      tools: number;
      config_errors: string[];
    }>("/mcp/reload", { method: "POST" }),

  // ---- 长期记忆 ----
  listMemories: (opts?: { includeArchived?: boolean; theme?: string }) => {
    const q = new URLSearchParams();
    if (opts?.includeArchived) q.set("include_archived", "true");
    if (opts?.theme) q.set("theme", opts.theme);
    const qs = q.toString();
    return request<{
      items: MemoryOut[];
      themes: { theme: string; count: number }[];
    }>(`/memories${qs ? `?${qs}` : ""}`);
  },

  createMemory: (content: string, theme: string) =>
    request<MemoryOut>("/memories", {
      method: "POST",
      json: { content, theme },
    }),

  updateMemory: (id: string, body: { content?: string; theme?: string; reason?: string }) =>
    request<MemoryOut>(`/memories/${id}`, {
      method: "PATCH",
      json: body,
    }),

  archiveMemory: (id: string) =>
    request<MemoryOut>(`/memories/${id}`, { method: "DELETE" }),

  restoreMemory: (id: string) =>
    request<MemoryOut>(`/memories/${id}/restore`, { method: "POST" }),

  searchMemories: (q: string) =>
    request<{
      items: (MemoryOut & { score: number })[];
      injection_preview: string;
    }>(`/memories-search?q=${encodeURIComponent(q)}`),
  // ---- 追踪 ----
  listTraces: (sessionId?: string) =>
    request<{
      items: {
        run_id: string;
        session_id: string;
        agent_name: string;
        status: string;
        stop_reason: string;
        started_at: number;
        duration_ms: number | null;
        turns: number;
        total_tokens: number;
        cost_usd: number;
        error: string;
      }[];
    }>(`/traces${sessionId ? `?session_id=${sessionId}` : ""}`),

  getTrace: (runId: string) =>
    request<{
      run_id: string;
      status: string;
      stop_reason: string;
      duration_ms: number | null;
      turns: number;
      total_tokens: number;
      span_totals: {
        total_tokens: number;
        cost_usd: number;
        by_agent: {
          agent_name: string;
          total_tokens: number;
          cost_usd: number;
          llm_calls: number;
        }[];
      };
      spans: TraceSpan[];
    }>(`/traces/${runId}`),

/**
   * 按会话汇总的执行记录。
   *
   * 追踪的第一层。原来直接铺开所有 run，几十条 run_id 后 8 位混在一起，
   * 没有线索说明哪条属于哪个对话。
   */
  traceSessions: () =>
    request<{
      items: {
        session_id: string;
        title: string;
        runs: number;
        total_tokens: number;
        cost_usd: number;
        errors: number;
        last_at: number;
      }[];
    }>("/traces-sessions"),

  traceStats: () =>
    request<{
      runs: number;
      spans: number;
      total_tokens: number;
      total_cost_usd: number;
      retain_days: number;
      writer?: {
        written: number;
        dropped: number;
        failed: number;
        recent_errors: string[];
      };
    }>("/traces-stats"),

  cleanupTraces: (retainDays: number) =>
    request<{ spans: number; runs: number }>(
      `/traces/cleanup?retain_days=${retainDays}`,
      { method: "POST" },
    ),

  // ---- 技能 ----
  listSkills: () =>
    request<{
      items: {
        name: string;
        description: string;
        version: string;
        keywords: string[];
        files: string[];
        /** 关掉的技能不进系统提示词。缺省视为启用 */
        enabled?: boolean;
      }[];
      diagnostics: { level: string; message: string; path: string }[];
    }>("/skills"),

  reloadSkills: () =>
    request<{ count: number; names: string[] }>("/skills/reload", {
      method: "POST",
    }),

  uploadSkill: (file: File, overwrite = false) => {
    const form = new FormData();
    form.append("file", file);
    // FormData 不能设 Content-Type —— 浏览器要自己加 boundary。
    // 手动设了会让后端解析失败。
    return request<{
      name: string;
      files: number;
      skipped: string[];
      skill_count: number;
    }>(`/skills/upload?overwrite=${overwrite}`, { method: "POST", body: form });
  },

  deleteSkill: (name: string) =>
    request<{ deleted: string; skill_count: number }>(
      `/skills/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    ),

  // ---- 宏 ----
/** 新建或更新宏。overwrite=false 时撞名会 409 */
  reloadMacros: () =>
    request<{ count: number; names: string[] }>("/macros/reload", {
      method: "POST",
    }),

  upsertMacro: (body: {
    name: string;
    description: string;
    body: string;
    keywords?: string[];
    overwrite?: boolean;
  }) =>
    request<{ name: string; created: boolean }>("/macros", {
      method: "POST",
      json: body,
    }),

  /**
   * 取宏的可编辑字段。
   *
   * 不用 getMacro —— 那个返回渲染后的正文（${MACRO_DIR} 已替换成真实
   * 路径），保存时写回去宏就跟当前机器绑死了。
   */
  getMacroSource: (name: string) =>
    request<{
      name: string;
      description: string;
      body: string;
      keywords: string[];
    }>(`/macros/${encodeURIComponent(name)}/source`),

  deleteMacro: (name: string) =>
    request<{ ok: boolean }>(`/macros/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),

  /** 开关一个技能。关掉的不进系统提示词 */
  toggleSkill: (name: string, enabled: boolean) =>
    request<{ name: string; enabled: boolean }>(
      `/skills/${encodeURIComponent(name)}/enabled`,
      { method: "PATCH", json: { enabled } },
    ),

  /** 开关一个 MCP 服务器。会改 yaml 并立刻重连 */
  toggleMcpServer: (serverId: string, enabled: boolean) =>
    request<{ server_id: string; enabled: boolean; tools: number }>(
      `/mcp/servers/${encodeURIComponent(serverId)}/enabled`,
      { method: "PATCH", json: { enabled } },
    ),

  listMacros: () =>
    request<{
      items: { name: string; description: string; keywords: string[] }[];
    }>("/macros"),

  getMacro: (name: string) =>
    request<{ name: string; body: string }>(
      `/macros/${encodeURIComponent(name)}`,
    ),

  patchSession: (
    id: string,
    body: Partial<{
      /** 这次对话用哪个模型。传空串回到默认绑定 */
      model_pk: string;
      /** 这次对话的工作目录。传空串清除 */
      work_dir: string;
      title: string;
      pinned: boolean;
      approval_mode: "manual" | "auto";
      private_mode: boolean;
      amnesia_mode: boolean;
      vision_mode: boolean;
    }>,
  ) => request<SessionDetail>(`/sessions/${id}`, { method: "PATCH", json: body }),

  deleteSession: (id: string) =>
    request<{ ok: boolean }>(`/sessions/${id}`, { method: "DELETE" }),

  listMessages: (id: string, agent_name = "") =>
    request<{ items: MessageOut[] }>(
      `/sessions/${id}/messages?agent_name=${encodeURIComponent(agent_name)}`,
    ),

  truncateFrom: (sessionId: string, messageId: string) =>
    request<{ deleted_count: number }>(
      `/sessions/${sessionId}/messages/${messageId}`,
      { method: "DELETE" },
    ),

  // ─────────────────────────── run ───────────────────────────

  cancelRun: (runId: string) =>
    request<{ run_id: string; status: string }>(`/runs/${runId}/cancel`, {
      method: "POST",
    }),

  approve: (runId: string, call_id: string, approved: boolean) =>
    request<{ ok: boolean }>(`/runs/${runId}/approve`, {
      method: "POST",
      json: { call_id, approved },
    }),

  answer: (
    runId: string,
    call_id: string,
    payload: { answer?: string; selected?: string[] },
  ) =>
    request<{ ok: boolean }>(`/runs/${runId}/answer`, {
      method: "POST",
      json: { call_id, ...payload },
    }),

  // ─────────────────────────── 供应商 ───────────────────────────

  probe: (base_url: string, api_key: string) =>
    request<ProbeResponse>("/providers/probe", {
      method: "POST",
      json: { base_url, api_key },
    }),

  listProviders: () => request<{ items: ProviderOut[] }>("/providers"),

  createProvider: (body: {
    name: string;
    base_url: string;
    api_key: string;
    models: { model_id: string; display_name?: string; context_window?: number }[];
  }) => request<ProviderOut>("/providers", { method: "POST", json: body }),

  deleteProvider: (id: string) =>
    request<{ ok: boolean }>(`/providers/${id}`, { method: "DELETE" }),

  listModels: (provider_id?: string) =>
    request<{ items: ModelOut[] }>(
      `/models${provider_id ? `?provider_id=${provider_id}` : ""}`,
    ),

  listBindings: () => request<{ items: BindingOut[] }>("/bindings"),

  setBinding: (purpose: Purpose, model_pk: string, agent_name = "") =>
    request<{ id: string }>("/bindings", {
      method: "PUT",
      json: { purpose, model_pk, agent_name },
    }),

  // ─────────────────────────── Todo ───────────────────────────

  listTodos: (sessionId: string) =>
    request<{ items: TodoItem[]; stats: TodoStats }>(
      `/sessions/${sessionId}/todos`,
    ),

  patchTodo: (
    id: string,
    body: Partial<Pick<TodoItem, "content" | "status" | "priority" | "order_index">>,
  ) => request<TodoItem>(`/todos/${id}`, { method: "PATCH", json: body }),

  deleteTodo: (id: string) =>
    request<{ ok: boolean }>(`/todos/${id}`, { method: "DELETE" }),

  archiveTodos: (sessionId: string) =>
    request<{ archived_count: number }>(`/sessions/${sessionId}/todos/archive`, {
      method: "POST",
    }),
};
