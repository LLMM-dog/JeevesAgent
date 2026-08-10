import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  Loader2,
  Pencil,
  Plug,
  Plus,
  RefreshCw,
  ToggleLeft,
  ToggleRight,
  Trash2,
  XCircle,
  X,
} from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "@/lib/api";
import type { AgentItem } from "@/lib/types";

/**
 * MCP 服务器面板。
 *
 * ## 为什么要显示 token 成本
 *
 * MCP 工具定义是**常驻上下文成本** —— 每轮请求都要带全部工具的名字、
 * 描述、入参 schema。配 5 个服务器共 60 个工具可能就是上万 token，
 * 每轮都烧。
 *
 * 看不到这个数字的话，用户会觉得"多开几个 MCP 没坏处"。
 *
 * ## 为什么要有待确认区
 *
 * 规范要求本地 MCP 服务器启动前必须让用户看到完整命令并确认 ——
 * 它等于任意代码执行，且以应用相同权限运行。
 */

const STATUS_META: Record<
  string,
  { label: string; cls: string; icon: typeof CheckCircle2 }
> = {
  ready: { label: "已连接", cls: "text-[var(--color-ok)]", icon: CheckCircle2 },
  connecting: { label: "连接中", cls: "text-[var(--color-muted)]", icon: Loader2 },
  error: { label: "失败", cls: "text-[var(--color-err)]", icon: XCircle },
  disconnected: { label: "未连接", cls: "text-[var(--color-muted)]", icon: Plug },
};

interface McpFormData {
  server_id: string;
  transport: string;
  url: string;
  headers: string;
  command: string;
  args: string;
  env: string;
  cwd: string;
  enabled: boolean;
}

function emptyForm(): McpFormData {
  return {
    server_id: "",
    transport: "http",
    url: "",
    headers: "",
    command: "",
    args: "",
    env: "",
    cwd: "",
    enabled: true,
  };
}

/** 把 headers JSON 字符串解析成对象，非法则返空对象 */
function parseHeaders(raw: string): Record<string, string> {
  if (!raw.trim()) return {};
  try {
    const obj = JSON.parse(raw);
    if (typeof obj === "object" && obj !== null && !Array.isArray(obj)) {
      return Object.fromEntries(
        Object.entries(obj).map(([k, v]) => [k, String(v)]),
      );
    }
  } catch {
    /* ignore */
  }
  return {};
}

/** 把 env JSON 字符串解析成对象 */
function parseEnv(raw: string): Record<string, string> {
  return parseHeaders(raw);
}

/** 把 args 字符串按逗号/空格分割 */
function parseArgs(raw: string): string[] {
  return raw
    .split(/[,\s]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

export default function McpPanel() {
  const qc = useQueryClient();
  const [expanded, setExpanded] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<McpFormData>(emptyForm());
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["mcpServers"],
    queryFn: api.mcpServers,
  });
  const { data: pending } = useQuery({
    queryKey: ["mcpPending"],
    queryFn: api.mcpPending,
  });

  const toggle = useMutation({
    mutationFn: (v: { id: string; enabled: boolean }) =>
      api.toggleMcpServer(v.id, v.enabled),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["mcpServers"] });
      void qc.invalidateQueries({ queryKey: ["contextOverhead"] });
    },
  });

  const reload = useMutation({
    mutationFn: api.mcpReload,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["mcpServers"] });
      qc.invalidateQueries({ queryKey: ["contextOverhead"] });
      void qc.invalidateQueries({ queryKey: ["mcpPending"] });
    },
  });

  const totalTokens = (data?.items ?? []).reduce(
    (sum, s) => sum + (s.status === "ready" ? s.estimated_tokens : 0),
    0,
  );

  const navigate = useNavigate();

  /** 查看某个 MCP 服务器被哪些智能体预设使用 */
  function McpAgentUsers({ serverId }: { serverId: string }) {
    const { data: agents, isLoading: loadingAgents } = useQuery({
      queryKey: ["agents", "using_mcp", serverId],
      queryFn: () => api.agents.list({ using_mcp: serverId }),
      enabled: !!serverId,
      staleTime: 30_000,
    });

    const removeAgent = useMutation({
      mutationFn: (agent: AgentItem) =>
        api.agents.update(agent.id, {
          mcp_servers: agent.mcp_servers.filter((s) => s !== serverId),
        }),
      onSuccess: () => {
        void qc.invalidateQueries({ queryKey: ["agents", "using_mcp", serverId] });
        void qc.invalidateQueries({ queryKey: ["agents"] });
      },
    });

    if (loadingAgents) {
      return (
        <div className="mt-2 text-[11px] text-[var(--color-muted)]">
          加载引用信息…
        </div>
      );
    }

    if (!agents || agents.length === 0) {
      return (
        <div className="mt-2 text-[11px] text-[var(--color-muted)]">
          未被任何智能体预设使用
        </div>
      );
    }

    return (
      <div className="mt-2 border-t border-[var(--color-border)] pt-2">
        <div className="mb-1 text-[11px] font-medium text-[var(--color-muted)]">
          被 {agents.length} 个智能体预设使用：
        </div>
        <ul className="space-y-1">
          {agents.map((agent) => (
            <li
              key={agent.id}
              className="flex items-center gap-1.5 text-[11px]"
            >
              <span className="shrink-0">
                {agent.avatar ?? "🤖"}
              </span>
              <span className="min-w-0 flex-1 truncate font-medium">
                {agent.name}
              </span>
              <button
                type="button"
                onClick={() => navigate("/settings?tab=agents")}
                className="shrink-0 rounded px-1 py-0.5 text-[10px] text-[var(--color-accent)] hover:bg-[var(--color-accent)]/10 hover:underline"
                title="跳转到智能体设置页"
              >
                跳转
              </button>
              <button
                type="button"
                onClick={() => {
                  if (
                    confirm(
                      `从智能体「${agent.name}」中移除 MCP「${serverId}」？`,
                    )
                  ) {
                    removeAgent.mutate(agent);
                  }
                }}
                disabled={removeAgent.isPending}
                className="shrink-0 rounded px-1 py-0.5 text-[10px] text-[var(--color-err)] hover:bg-[var(--color-err)]/10 hover:underline disabled:opacity-50"
                title="从此智能体的预设中移除此 MCP 服务器"
              >
                {removeAgent.isPending ? "移除中…" : "移除"}
              </button>
            </li>
          ))}
        </ul>
      </div>
    );
  }

  /** 打开新增表单 */
  function openAddForm() {
    setForm(emptyForm());
    setEditingId(null);
    setFormError(null);
    setShowForm(true);
  }

  /** 打开编辑表单：从列表项里已有的字段回填 */
  function openEditForm(serverId: string) {
    const s = (data?.items ?? []).find((i) => i.server_id === serverId);
    if (!s) return;
    setForm({
      server_id: s.server_id,
      transport: s.transport || "http",
      url: "",
      headers: "",
      command: "",
      args: "",
      env: "",
      cwd: "",
      enabled: s.enabled !== false,
    });
    setEditingId(serverId);
    setFormError(null);
    setShowForm(true);
  }

  /** 提交表单：新增或更新 */
  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);

    if (!editingId && !form.server_id.trim()) {
      setFormError("server_id 不能为空");
      return;
    }

    const body: Record<string, unknown> = {
      transport: form.transport,
      enabled: form.enabled,
    };

    if (form.transport === "http") {
      body.url = form.url.trim();
      body.headers = parseHeaders(form.headers);
    } else {
      body.command = form.command.trim();
      body.args = parseArgs(form.args);
      body.env = parseEnv(form.env);
      body.cwd = form.cwd.trim();
    }

    // 只有新增时才传 server_id
    if (!editingId) {
      body.server_id = form.server_id.trim();
    }

    setSubmitting(true);
    try {
      if (editingId) {
        await api.mcpUpdate(editingId, body);
      } else {
        await api.mcpAdd(body as Parameters<typeof api.mcpAdd>[0]);
      }
      setShowForm(false);
      void qc.invalidateQueries({ queryKey: ["mcpServers"] });
    } catch (err: unknown) {
      const msg =
        err instanceof Error ? err.message : "保存失败，请检查配置";
      setFormError(msg);
    } finally {
      setSubmitting(false);
    }
  }

  /** 删除服务器 */
  async function handleDelete(serverId: string) {
    setSubmitting(true);
    try {
      await api.mcpDelete(serverId);
      setDeleteConfirm(null);
      void qc.invalidateQueries({ queryKey: ["mcpServers"] });
    } catch (err: unknown) {
      setFormError(
        err instanceof Error ? err.message : "删除失败",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <div className="mb-1 flex items-center gap-2">
        <h2 className="text-sm font-medium">MCP 服务器</h2>
        <button
          type="button"
          onClick={openAddForm}
          className="flex items-center gap-1 rounded border border-[var(--color-accent)]/40 px-1.5 py-0.5 text-[10px] text-[var(--color-accent)] hover:bg-[var(--color-accent)]/10"
        >
          <Plus size={10} aria-hidden />
          添加服务器
        </button>
        <button
          type="button"
          onClick={() => reload.mutate()}
          disabled={reload.isPending}
          className="ml-auto flex items-center gap-1 rounded border border-[var(--color-border)] px-1.5 py-0.5 text-[10px] hover:bg-[var(--color-bg)] disabled:opacity-50"
        >
          <RefreshCw
            size={10}
            aria-hidden
            className={reload.isPending ? "animate-spin" : ""}
          />
          {reload.isPending ? "重载中…" : "重载配置"}
        </button>
      </div>
      <p className="mb-3 text-xs text-[var(--color-muted)]">
        通过 Model Context Protocol 接外部工具，改 <code>config/mcp_servers.yaml</code>{" "}
        后点重载。MCP 工具全部需要审批 —— 它们是第三方代码，
        且服务器自述的"只读"标记不可信。
      </p>

      {/* 新增 / 编辑表单 */}
      {showForm && (
        <div className="mb-3 rounded-lg border border-[var(--color-accent)]/30 bg-[var(--color-bg)] p-3">
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-xs font-medium">
              {editingId ? `编辑 ${editingId}` : "添加 MCP 服务器"}
            </h3>
            <button
              type="button"
              onClick={() => setShowForm(false)}
              className="rounded p-0.5 text-[var(--color-muted)] hover:text-[var(--color-fg)]"
            >
              <X size={14} aria-hidden />
            </button>
          </div>

          {formError && (
            <div className="mb-2 rounded bg-[var(--color-err)]/10 px-2 py-1 text-[11px] text-[var(--color-err)]">
              {formError}
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-2">
            {/* server_id */}
            <label className="block">
              <span className="text-[10px] text-[var(--color-muted)]">
                server_id
              </span>
              <input
                type="text"
                value={form.server_id}
                onChange={(e) =>
                  setForm({ ...form, server_id: e.target.value })
                }
                disabled={!!editingId}
                placeholder="my-mcp-server"
                className="mt-0.5 w-full rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1 text-[11px] placeholder:text-[var(--color-muted)] disabled:opacity-50"
              />
            </label>

            {/* transport */}
            <label className="block">
              <span className="text-[10px] text-[var(--color-muted)]">
                transport
              </span>
              <select
                value={form.transport}
                onChange={(e) =>
                  setForm({ ...form, transport: e.target.value })
                }
                className="mt-0.5 w-full rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1 text-[11px]"
              >
                <option value="http">http</option>
                <option value="stdio">stdio</option>
              </select>
            </label>

            {form.transport === "http" ? (
              <>
                <label className="block">
                  <span className="text-[10px] text-[var(--color-muted)]">
                    URL
                  </span>
                  <input
                    type="text"
                    value={form.url}
                    onChange={(e) =>
                      setForm({ ...form, url: e.target.value })
                    }
                    placeholder="http://localhost:8080/mcp"
                    className="mt-0.5 w-full rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1 text-[11px] placeholder:text-[var(--color-muted)]"
                  />
                </label>
                <label className="block">
                  <span className="text-[10px] text-[var(--color-muted)]">
                    headers（JSON）
                  </span>
                  <input
                    type="text"
                    value={form.headers}
                    onChange={(e) =>
                      setForm({ ...form, headers: e.target.value })
                    }
                    placeholder='{"Authorization": "Bearer xxx"}'
                    className="mt-0.5 w-full rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1 text-[11px] placeholder:text-[var(--color-muted)]"
                  />
                </label>
              </>
            ) : (
              <>
                <label className="block">
                  <span className="text-[10px] text-[var(--color-muted)]">
                    command
                  </span>
                  <input
                    type="text"
                    value={form.command}
                    onChange={(e) =>
                      setForm({ ...form, command: e.target.value })
                    }
                    placeholder="npx"
                    className="mt-0.5 w-full rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1 text-[11px] placeholder:text-[var(--color-muted)]"
                  />
                </label>
                <label className="block">
                  <span className="text-[10px] text-[var(--color-muted)]">
                    args（逗号或空格分隔）
                  </span>
                  <input
                    type="text"
                    value={form.args}
                    onChange={(e) =>
                      setForm({ ...form, args: e.target.value })
                    }
                    placeholder="-y @modelcontextprotocol/server-filesystem"
                    className="mt-0.5 w-full rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1 text-[11px] placeholder:text-[var(--color-muted)]"
                  />
                </label>
                <label className="block">
                  <span className="text-[10px] text-[var(--color-muted)]">
                    env（JSON）
                  </span>
                  <input
                    type="text"
                    value={form.env}
                    onChange={(e) =>
                      setForm({ ...form, env: e.target.value })
                    }
                    placeholder='{"KEY": "val"}'
                    className="mt-0.5 w-full rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1 text-[11px] placeholder:text-[var(--color-muted)]"
                  />
                </label>
                <label className="block">
                  <span className="text-[10px] text-[var(--color-muted)]">
                    cwd
                  </span>
                  <input
                    type="text"
                    value={form.cwd}
                    onChange={(e) =>
                      setForm({ ...form, cwd: e.target.value })
                    }
                    placeholder="/path/to/working/dir"
                    className="mt-0.5 w-full rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1 text-[11px] placeholder:text-[var(--color-muted)]"
                  />
                </label>
              </>
            )}

            {/* enabled */}
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={(e) =>
                  setForm({ ...form, enabled: e.target.checked })
                }
                className="h-3.5 w-3.5"
              />
              <span className="text-[11px]">启用</span>
            </label>

            {/* buttons */}
            <div className="flex gap-2 pt-1">
              <button
                type="submit"
                disabled={submitting}
                className="rounded bg-[var(--color-accent)] px-3 py-1 text-[11px] font-medium text-white hover:opacity-90 disabled:opacity-50"
              >
                {submitting
                  ? "保存中…"
                  : editingId
                    ? "保存修改"
                    : "添加"}
              </button>
              <button
                type="button"
                onClick={() => setShowForm(false)}
                className="rounded border border-[var(--color-border)] px-3 py-1 text-[11px] hover:bg-[var(--color-bg)]"
              >
                取消
              </button>
            </div>
          </form>
        </div>
      )}

      {/* 删除确认 */}
      {deleteConfirm && (
        <div className="mb-3 rounded-lg border border-[var(--color-err)]/30 bg-[var(--color-err)]/5 p-2.5">
          <p className="mb-2 text-[11px]">
            确定要删除 MCP 服务器 <code>{deleteConfirm}</code> 吗？
            这个操作不可撤销。
          </p>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => handleDelete(deleteConfirm)}
              disabled={submitting}
              className="rounded bg-[var(--color-err)] px-3 py-1 text-[11px] font-medium text-white hover:opacity-90 disabled:opacity-50"
            >
              {submitting ? "删除中…" : "确认删除"}
            </button>
            <button
              type="button"
              onClick={() => setDeleteConfirm(null)}
              className="rounded border border-[var(--color-border)] px-3 py-1 text-[11px] hover:bg-[var(--color-bg)]"
            >
              取消
            </button>
          </div>
        </div>
      )}

      {/* 待确认的启动命令。放最上面 —— 它阻塞着服务器连接 */}
      {(pending?.items.length ?? 0) > 0 && (
        <div className="mb-3 rounded-lg border border-[var(--color-warn)]/40 bg-[var(--color-warn)]/5 p-2.5">
          <div className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-[var(--color-warn)]">
            <AlertTriangle size={12} aria-hidden />
            有 {pending!.items.length} 个服务器的启动命令待确认
          </div>
          <p className="mb-2 text-[11px] text-[var(--color-muted)]">
            本地 MCP 服务器会以与本应用相同的权限执行下面的命令，等同于任意代码执行。
            确认前请逐字读完整条命令。确认方式：在 yaml 里给该服务器加{" "}
            <code>command_approved: true</code>。
          </p>
          {pending!.items.map((p) => (
            <div key={p.server_id} className="mb-1.5 last:mb-0">
              <div className="text-[11px] font-medium">{p.server_id}</div>
              {/* 完整命令，不截断 —— 省略的部分正是藏 payload 的地方 */}
              <pre className="mt-0.5 overflow-x-auto whitespace-pre-wrap break-all rounded bg-[var(--color-bg)] px-2 py-1 text-[10px]">
                {p.command}
              </pre>
              {p.cwd && (
                <div className="text-[10px] text-[var(--color-muted)]">
                  工作目录：{p.cwd}
                </div>
              )}
              {p.env_keys.length > 0 && (
                <div className="text-[10px] text-[var(--color-muted)]">
                  环境变量：{p.env_keys.join(", ")}
                </div>
              )}
              {p.warnings.length > 0 && (
                <ul className="mt-0.5 space-y-0.5">
                  {p.warnings.map((w) => (
                    <li
                      key={w.pattern}
                      className="text-[10px] text-[var(--color-err)]"
                    >
                      含 <code>{w.pattern}</code> —— {w.reason}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ))}
        </div>
      )}

      {/* 配置错误 */}
      {(data?.config_errors.length ?? 0) > 0 && (
        <div className="mb-2 rounded-lg bg-[var(--color-err)]/10 px-2.5 py-1.5 text-[11px] text-[var(--color-err)]">
          {data!.config_errors.map((e, i) => (
            <div key={i}>{e}</div>
          ))}
        </div>
      )}

      {isLoading ? (
        <div className="flex items-center gap-1.5 text-xs text-[var(--color-muted)]">
          <Loader2 size={12} className="animate-spin" aria-hidden />
          加载中…
        </div>
      ) : (data?.items.length ?? 0) === 0 ? (
        <div className="rounded-lg border border-dashed border-[var(--color-border)] px-3 py-5 text-center text-xs text-[var(--color-muted)]">
          没有配置 MCP 服务器。
          <button
            type="button"
            onClick={openAddForm}
            className="ml-1 text-[var(--color-accent)] hover:underline"
          >
            点击添加
          </button>
        </div>
      ) : (
        <>
          <ul className="space-y-1">
            {data!.items.map((s) => {
              const meta = STATUS_META[s.status] ?? STATUS_META.disconnected;
              const Icon = meta.icon;
              const open = expanded === s.server_id;
              return (
                <li
                  key={s.server_id}
                  className={`rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5 ${
                    s.enabled === false ? "opacity-55" : ""
                  }`}
                >
                  <div className="flex items-center gap-2">
                    <Icon
                      size={12}
                      className={`shrink-0 ${meta.cls} ${
                        s.status === "connecting" ? "animate-spin" : ""
                      }`}
                      aria-hidden
                    />
                    <span className="min-w-0 flex-1 truncate text-xs">
                      {s.server_id}
                      <span className="ml-1 text-[10px] text-[var(--color-muted)]">
                        {s.transport}
                      </span>
                    </span>
                    <span className={`shrink-0 text-[10px] ${meta.cls}`}>
                      {s.enabled === false ? "已关闭" : meta.label}
                    </span>
                    {/* 编辑按钮 */}
                    <button
                      type="button"
                      onClick={() => openEditForm(s.server_id)}
                      className="shrink-0 rounded p-0.5 text-[var(--color-muted)] hover:text-[var(--color-fg)]"
                      title="编辑服务器"
                    >
                      <Pencil size={12} aria-hidden />
                    </button>
                    {/* 删除按钮 */}
                    <button
                      type="button"
                      onClick={() => setDeleteConfirm(s.server_id)}
                      className="shrink-0 rounded p-0.5 text-[var(--color-muted)] hover:text-[var(--color-err)]"
                      title="删除服务器"
                    >
                      <Trash2 size={12} aria-hidden />
                    </button>
                    {/* 开关 */}
                    <button
                      type="button"
                      onClick={() =>
                        toggle.mutate({
                          id: s.server_id,
                          enabled: s.enabled === false,
                        })
                      }
                      disabled={toggle.isPending}
                      aria-pressed={s.enabled !== false}
                      title={
                        s.enabled === false
                          ? "已关闭：不连接，工具定义不占上下文。点击开启"
                          : `已启用：${s.tool_count} 个工具常驻上下文。点击关闭`
                      }
                      className="shrink-0 text-[var(--color-muted)] hover:text-[var(--color-fg)] disabled:opacity-50"
                    >
                      {s.enabled === false ? (
                        <ToggleLeft size={18} aria-hidden />
                      ) : (
                        <ToggleRight
                          size={18}
                          className="text-[var(--color-accent)]"
                          aria-hidden
                        />
                      )}
                    </button>
                    {s.status === "ready" ? (
                      <button
                        type="button"
                        onClick={() => setExpanded(open ? null : s.server_id)}
                        className="shrink-0 text-[10px] text-[var(--color-muted)] hover:underline"
                      >
                        {s.tool_count} 个工具 · ~{s.estimated_tokens} token
                      </button>
                    ) : (
                      <button
                        type="button"
                        onClick={() => setExpanded(open ? null : s.server_id)}
                        className="shrink-0 text-[10px] text-[var(--color-muted)] hover:underline"
                      >
                        详情
                      </button>
                    )}
                  </div>
                  {s.error && (
                    <p className="mt-1 break-words text-[10px] text-[var(--color-err)]">
                      {s.error}
                    </p>
                  )}
                  {open && (
                    <>
                      {s.status === "ready" && s.tools.length > 0 && (
                        <ul className="mt-1 space-y-0.5 border-t border-[var(--color-border)] pt-1">
                          {s.tools.map((t) => (
                            <li key={t.name} className="text-[10px]">
                              <code className="text-[var(--color-accent)]">{t.name}</code>
                              {t.raw_name !== t.name.split("__").pop() && (
                                <span className="ml-1 text-[var(--color-muted)]">
                                  （原名 {t.raw_name}）
                                </span>
                              )}
                            </li>
                          ))}
                        </ul>
                      )}
                      <McpAgentUsers serverId={s.server_id} />
                    </>
                  )}
                </li>
              );
            })}
          </ul>
          {totalTokens > 0 && (
            <p className="mt-2 text-[10px] text-[var(--color-muted)]">
              已连接的 MCP 工具定义约占 {totalTokens.toLocaleString()} token
              的常驻上下文 —— 这部分每一轮都会发送。用不到的服务器建议在
              yaml 里设 <code>enabled: false</code>。
            </p>
          )}
        </>
      )}
    </section>
  );
}
