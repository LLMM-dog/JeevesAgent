/**
 * REST 客户端。
 *
 * 所有请求走同一个 request()，错误统一转成 ApiError ——
 * 组件层只需处理一种错误形状。
 */

import { ApiError } from "./sse";
import type {
  BindingOut,
  MessageOut,
  MetaResponse,
  ModelOut,
  ProbeResponse,
  ProviderOut,
  Purpose,
  SessionDetail,
  SessionListResponse,
  TodoItem,
  MemoryOut,
  CronTask,
  CronRun,
  CronValidateResult,
  TodoStats,
  TraceSpan,
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
  refCandidates: (kind: "file" | "skill" | "tool" | "macro", q: string) =>
    request<{
      items: { name: string; path?: string; detail: string }[];
    }>(`/ref-candidates?kind=${kind}&q=${encodeURIComponent(q)}`),

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
      body: JSON.stringify({ content, theme }),
    }),

  updateMemory: (id: string, body: { content?: string; theme?: string; reason?: string }) =>
    request<MemoryOut>(`/memories/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
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
