/**
 * REST 客户端。
 *
 * 所有请求走同一个 request()，错误统一转成 ApiError ——
 * 组件层只需处理一种错误形状。
 */

import { ApiError } from "./sse";
import type {
  AuthMeResponse,
  CpolarAction,
  CpolarStatus,
  WorkspaceItem,
  DeploySettingsResponse,
  DeployStatus,
  EnableAuthResponse,
  LoginResponse,
  TailscaleAction,
  TailscaleStatus,
  UserItem,
  AgentItem,
  BindingOut,
  BrowseResult,
  CronRun,
  CronTask,
  CronValidateResult,
  MemoryItem,
  MemoryListResponse,
  MemoryRebuildResult,
  MemorySearchResult,
  MemorySettingItem,
  MemoryTrace,
  MemoryTraceListResponse,
  MemoryVectorizeResponse,
  MemoryVectorStatus,
  MemoryWriteRequest,
  MemoryWriteResponse,
  MessageOut,
  MetaResponse,
  ModelItem,
  ModelOut,
  ProbeResponse,
  EndpointOut,
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

  // 会话失效（过期/被吊销）→ 踢回登录页。
  // login 自己的 401 是"密码错误"，不触发 —— 它本来就在登录页上。
  if (resp.status === 401 && path !== "/auth/login") {
    // 动态 import 避免循环依赖：api.ts ← store/auth.ts ← api.ts
    const { useAuth } = await import("@/store/auth");
    useAuth.getState().sessionExpired();
  }

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

  // ── 鉴权 ──

  authMe: () => request<AuthMeResponse>("/auth/me"),
  login: (username: string, password: string) =>
    request<LoginResponse>("/auth/login", {
      method: "POST",
      json: { username, password },
    }),
  logout: () => request<{ ok: boolean }>("/auth/logout", { method: "POST" }),
  changePassword: (old_password: string, new_password: string) =>
    request<{ ok: boolean }>("/auth/password", {
      method: "POST",
      json: { old_password, new_password },
    }),
  listUsers: () => request<UserItem[]>("/auth/users"),
  createUser: (body: {
    username: string;
    password: string;
    is_admin?: boolean;
  }) => request<UserItem>("/auth/users", { method: "POST", json: body }),
  patchUser: (
    id: string,
    body: { password?: string; is_admin?: boolean; enabled?: boolean },
  ) =>
    request<UserItem>(`/auth/users/${id}`, { method: "PATCH", json: body }),
  deleteUser: (id: string) =>
    request<{ ok: boolean }>(`/auth/users/${id}`, { method: "DELETE" }),

  // ── 工作区 ──

  listWorkspaces: () => request<WorkspaceItem[]>("/workspaces"),
  createWorkspace: (body: {
    name: string;
    root_path: string;
    sandbox_backend?: "local" | "docker";
    docker_container?: string;
    docker_image?: string;
    docker_network?: "none" | "bridge";
  }) => request<WorkspaceItem>("/workspaces", { method: "POST", json: body }),
  patchWorkspace: (
    id: string,
    body: Partial<{
      name: string;
      sandbox_backend: "local" | "docker";
      docker_container: string;
      docker_image: string;
      docker_network: "none" | "bridge";
    }>,
  ) => request<WorkspaceItem>(`/workspaces/${id}`, { method: "PATCH", json: body }),
  deleteWorkspace: (id: string) =>
    request<{ ok: boolean }>(`/workspaces/${id}`, { method: "DELETE" }),

  // ── 部署 ──

  deployStatus: () => request<DeployStatus>("/deploy/status"),
  cpolarStatus: () => request<CpolarStatus>("/deploy/cpolar"),
  cpolarInstall: () =>
    request<CpolarAction>("/deploy/cpolar/install", { method: "POST" }),
  cpolarAuthtoken: (token: string) =>
    request<CpolarAction>("/deploy/cpolar/authtoken", {
      method: "POST",
      json: { token },
    }),
  cpolarStart: (port: number) =>
    request<CpolarAction>("/deploy/cpolar/start", {
      method: "POST",
      json: { port },
    }),
  cpolarStop: () =>
    request<CpolarAction>("/deploy/cpolar/stop", { method: "POST" }),
  deploySettings: () =>
    request<DeploySettingsResponse>("/deploy/settings"),
  updateDeploySettings: (values: Record<string, unknown>) =>
    request<DeploySettingsResponse>("/deploy/settings", {
      method: "PUT",
      json: { values },
    }),
  enableAuth: (username: string, password: string) =>
    request<EnableAuthResponse>("/deploy/enable-auth", {
      method: "POST",
      json: { username, password },
    }),
  tailscaleInstall: () =>
    request<TailscaleAction>("/deploy/tailscale/install", { method: "POST" }),
  tailscaleLogin: () =>
    request<TailscaleAction>("/deploy/tailscale/login", { method: "POST" }),
  tailscaleDaemon: () =>
    request<TailscaleAction>("/deploy/tailscale/daemon", { method: "POST" }),
  tailscaleStatus: () => request<TailscaleStatus>("/deploy/tailscale"),
  tailscaleServe: (port: number) =>
    request<TailscaleAction>("/deploy/tailscale/serve", {
      method: "POST",
      json: { port },
    }),
  tailscaleServeStop: () =>
    request<TailscaleAction>("/deploy/tailscale/serve/stop", { method: "POST" }),
  tailscaleFunnel: (port: number) =>
    request<TailscaleAction>("/deploy/tailscale/funnel", {
      method: "POST",
      json: { port },
    }),
  tailscaleFunnelStop: () =>
    request<TailscaleAction>("/deploy/tailscale/funnel/stop", { method: "POST" }),

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
    kind: "file" | "skill" | "tool",
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
  models: (opts?: { endpointId?: string; enabledOnly?: boolean }) => {
    const q = new URLSearchParams();
    if (opts?.endpointId) q.set("endpoint_id", opts.endpointId);
    if (opts?.enabledOnly) q.set("enabled_only", "true");
    const s = q.toString();
    return request<{ items: ModelItem[] }>(`/models${s ? `?${s}` : ""}`);
  },

/**
   * 拉这个端点可用的模型列表。
   *
   * 用已存的端点和 Key —— 往【已有】端点下加模型时，让用户再填一遍
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

  availableModels: (endpointId: string) =>
    request<{
      items: {
        model_id: string;
        context_window: number;
        window_source: string;
        looks_non_chat: boolean;
        model_type: string;
        already_added: boolean;
      }[];
    }>(`/endpoints/${endpointId}/available-models`),

  addModel: (body: {
    endpoint_id: string;
    model_id: string;
    display_name?: string;
    context_window?: number;
    model_type?: string;
    enabled?: boolean;
  }) => request<ModelItem>("/models", { method: "POST", json: body }),

  patchModel: (
    pk: string,
    body: {
      enabled?: boolean;
      display_name?: string;
      context_window?: number;
      endpoint_id?: string;
      model_type?: string;
      price_in_per_1m?: number | null;
      price_out_per_1m?: number | null;
      supports_vision?: "true" | "false" | "unknown";
      supports_tools?: "true" | "false" | "unknown";
    },
  ) =>
    request<ModelItem>(`/models/${pk}`, {
      method: "PATCH",
      json: body,
    }),

  deleteModel: (pk: string) =>
    request<{ ok: boolean }>(`/models/${pk}`, { method: "DELETE" }),

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
      /** 脱敏的 Key 尾号，如 "****X7Qs"。空 = 未配置 */
      key_hint: string;
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

  mcpGetServer: (serverId: string) =>
    request<{
      server_id: string;
      transport: string;
      enabled: boolean;
      url: string;
      headers: Record<string, string>;
      command: string;
      args: string[];
      env: Record<string, string>;
      cwd: string;
      command_approved: boolean;
      status: string;
      error: string;
      tool_count: number;
      tools: { name: string; raw_name: string; description: string }[];
      estimated_tokens: number;
      connected_at: number | null;
    }>(`/mcp/servers/${encodeURIComponent(serverId)}`),

  mcpAdd: (body: {
    server_id: string;
    transport: string;
    url?: string;
    headers?: Record<string, string>;
    command?: string;
    args?: string[];
    env?: Record<string, string>;
    cwd?: string;
    enabled?: boolean;
  }) =>
    request<{ server_id: string; transport: string; enabled: boolean }>(
      "/mcp/servers",
      { method: "POST", json: body },
    ),

  mcpUpdate: (
    serverId: string,
    body: {
      transport?: string;
      url?: string;
      headers?: Record<string, string>;
      command?: string;
      args?: string[];
      env?: Record<string, string>;
      cwd?: string;
      enabled?: boolean;
    },
  ) =>
    request<{ server_id: string; ok: boolean }>(
      `/mcp/servers/${encodeURIComponent(serverId)}`,
      { method: "PATCH", json: body },
    ),

  mcpDelete: (serverId: string) =>
    request<{ ok: boolean }>(
      `/mcp/servers/${encodeURIComponent(serverId)}`,
      { method: "DELETE" },
    ),

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
        /** 第一次执行时间（展示用） */
        first_at: number;
        /** 最近执行时间（排序用） */
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

  patchSession: (
    id: string,
    body: Partial<{
      /** 这次对话用哪个模型。传空串回到默认绑定 */
      model_pk: string;
      /** 这次对话用哪个智能体。传空串清除选择 */
      agent_id: string;
      /** 切换工作区（根目录 + 执行环境） */
      workspace_id: string;
      title: string;
      pinned: boolean;
      approval_mode: "manual" | "auto";
      private_mode: boolean;
      amnesia_mode: boolean;
      vision_mode: boolean;
      stream_enabled: boolean;
    }>,
  ) => request<SessionDetail>(`/sessions/${id}`, { method: "PATCH", json: body }),

  deleteSession: (id: string) =>
    request<{ ok: boolean }>(`/sessions/${id}`, { method: "DELETE" }),

  /**
   * 批量删除会话。单次最多 100 个，部分失败不影响其他会话。
   * 返回区分成功 / 失败 / 不存在，前端要分别反馈。
   */
  batchDeleteSessions: (sessionIds: string[]) =>
    request<{
      total: number;
      succeeded: string[];
      failed: { session_id: string; error: string }[];
      not_found: string[];
    }>("/sessions/batch-delete", {
      method: "POST",
      json: { session_ids: sessionIds },
    }),

  listMessages: (id: string, agent_name = "") =>
    request<{ items: MessageOut[]; watermark: number }>(
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

  // ─────────────────────────── 端点 ───────────────────────────

  probe: (base_url: string, api_key: string) =>
    request<ProbeResponse>("/endpoints/probe", {
      method: "POST",
      json: { base_url, api_key },
    }),

  listEndpoints: () => request<{ items: EndpointOut[] }>("/endpoints"),

  createEndpoint: (body: {
    name?: string;
    base_url: string;
    api_key: string;
    models: {
      model_id: string;
      display_name?: string;
      context_window?: number;
      model_type?: string;
      supports_vision?: "true" | "false" | "unknown";
      supports_tools?: "true" | "false" | "unknown";
    }[];
  }) => request<EndpointOut>("/endpoints", { method: "POST", json: body }),

  patchEndpoint: (
    id: string,
    body: { name?: string; base_url?: string; api_key?: string },
  ) => request<EndpointOut>(`/endpoints/${id}`, { method: "PATCH", json: body }),

  deleteEndpoint: (id: string) =>
    request<{ ok: boolean }>(`/endpoints/${id}`, { method: "DELETE" }),

  listModels: (endpoint_id?: string) =>
    request<{ items: ModelOut[] }>(
      `/models${endpoint_id ? `?endpoint_id=${endpoint_id}` : ""}`,
    ),

  listBindings: () => request<{ items: BindingOut[] }>("/bindings"),

  setBinding: (purpose: Purpose, model_pk: string, agent_name = "") =>
    request<{ id: string }>("/bindings", {
      method: "PUT",
      json: { purpose, model_pk, agent_name },
    }),

  // ─────────────────────────── Todo ───────────────────────────

  /**
   * 这个会话有没有正在后台跑的 run。
   *
   * 切走会话时服务端的生成会继续跑完。切回来要靠这个知道"还在跑"——
   * 否则界面显示的是切走那一刻的历史，而发消息会撞 409。
   */
  activeRun: (sessionId: string) =>
    request<{ run_id: string } | null>(
      `/sessions/${encodeURIComponent(sessionId)}/active-run`,
    ),

  /**
   * 记忆提取状态。后端异步提取，前端轮询它在 UI 上给个轻量提示。
   * extracting 时正在后台整理记忆，不影响对话。
   */
  sessionMemoryStatus: (sessionId: string) =>
    request<{ extracting: boolean; extraction_id: string }>(
      `/sessions/${encodeURIComponent(sessionId)}/memory-status`,
    ),

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

  // ── 智能体 ──

  agents: {
    list: (opts?: boolean | { hidden?: boolean; using_skill?: string; using_mcp?: string }) => {
      const q = new URLSearchParams();
      if (typeof opts === "boolean") {
        q.set("hidden", String(opts));
      } else if (opts) {
        if (opts.hidden !== undefined) q.set("hidden", String(opts.hidden));
        if (opts.using_skill) q.set("using_skill", opts.using_skill);
        if (opts.using_mcp) q.set("using_mcp", opts.using_mcp);
      }
      const qs = q.toString();
      return request<AgentItem[]>(`/agents${qs ? `?${qs}` : ""}`);
    },
    get: (id: string) => request<AgentItem>(`/agents/${id}`),
    create: (data: Partial<AgentItem>) =>
      request<AgentItem>("/agents", { method: "POST", json: data }),
    update: (id: string, data: Partial<AgentItem>) =>
      request<AgentItem>(`/agents/${id}`, { method: "PATCH", json: data }),
    delete: (id: string) =>
      request<{ ok: boolean }>(`/agents/${id}`, { method: "DELETE" }),
  },

  memory: {
    // ── 可调设置 ──
    // 可调项的元信息 + 当前值。
    //
    // 前端【不硬编码】可调项列表 —— 类型、范围、说明都从后端来，
    // 否则后端加一项前端就得跟着改，两边必然不同步。
    settings: () => request<{ items: MemorySettingItem[] }>("/memory/settings"),
    updateSettings: (values: Record<string, unknown>) =>
      request<{ applied: Record<string, unknown>; items: MemorySettingItem[] }>(
        "/memory/settings",
        { method: "PUT", json: { values } },
      ),
    resetSettings: () =>
      request<{ removed: number; items: MemorySettingItem[] }>(
        "/memory/settings/reset",
        { method: "POST" },
      ),

    // ── 向量管理 ──
    vectors: () => request<MemoryVectorStatus>("/memory/vectors"),
    rebuildVectors: (onlyStale = true) =>
      request<MemoryRebuildResult>(
        `/memory/vectors/rebuild?only_stale=${onlyStale}`,
        { method: "POST" },
      ),
    clearVectors: () =>
      request<{ cleared: number }>("/memory/vectors", { method: "DELETE" }),

    // ── 记忆 CRUD ──
    list: (opts?: {
      agent_id?: string;
      session_id?: string;
      memory_type?: string;
      limit?: number;
    }) => {
      const p = new URLSearchParams();
      if (opts?.agent_id) p.set("agent_id", opts.agent_id);
      if (opts?.session_id) p.set("session_id", opts.session_id);
      if (opts?.memory_type) p.set("memory_type", opts.memory_type);
      if (opts?.limit) p.set("limit", String(opts.limit));
      return request<MemoryListResponse>(`/memory/list?${p.toString()}`);
    },

    read: (uri: string) => {
      const p = new URLSearchParams({ uri });
      return request<MemoryItem>(`/memory/read?${p.toString()}`);
    },

    write: (data: MemoryWriteRequest) =>
      request<MemoryWriteResponse>("/memory/write", {
        method: "POST",
        json: data,
      }),

    delete: (uri: string) => {
      const p = new URLSearchParams({ uri });
      return request<{ deleted: boolean; uri: string }>(`/memory/delete?${p.toString()}`, {
        method: "DELETE",
      });
    },

    // ── 向量化 ──
    vectorize: (uris: string[]) =>
      request<MemoryVectorizeResponse>("/memory/vectorize", {
        method: "POST",
        json: { uris },
      }),

    // ── 搜索 ──
    search: (
      q: string,
      opts?: { agent_id?: string; session_id?: string; memory_type?: string; limit?: number },
    ) => {
      const p = new URLSearchParams({ q });
      if (opts?.agent_id) p.set("agent_id", opts.agent_id);
      if (opts?.session_id) p.set("session_id", opts.session_id);
      if (opts?.memory_type) p.set("memory_type", opts.memory_type);
      if (opts?.limit) p.set("limit", String(opts.limit));
      return request<MemorySearchResult>(`/memory/search?${p.toString()}`);
    },

    // ── 初始化智能体 ──
    initAgent: (agent_id: string) =>
      request<{ agent_id: string; created_files: string[] }>(
        `/memory/init-agent?agent_id=${encodeURIComponent(agent_id)}`,
        { method: "POST" },
      ),

    // ── 记忆痕迹 ──
    traces: (opts?: { agent_id?: string; session_id?: string; limit?: number }) => {
      const p = new URLSearchParams();
      if (opts?.agent_id) p.set("agent_id", opts.agent_id);
      if (opts?.session_id) p.set("session_id", opts.session_id);
      if (opts?.limit) p.set("limit", String(opts.limit));
      return request<MemoryTraceListResponse>(`/memory/traces?${p.toString()}`);
    },

    trace: (
      extractionId: string,
      opts?: { agent_id?: string; session_id?: string },
    ) => {
      const p = new URLSearchParams();
      if (opts?.agent_id) p.set("agent_id", opts.agent_id);
      if (opts?.session_id) p.set("session_id", opts.session_id);
      const s = p.toString();
      return request<MemoryTrace>(`/memory/traces/${extractionId}${s ? `?${s}` : ""}`);
    },
  },
};
