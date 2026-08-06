import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  Loader2,
  Plug,
  RefreshCw,
  XCircle,
} from "lucide-react";
import { useState } from "react";

import { api } from "@/lib/api";

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

export default function McpPanel() {
  const qc = useQueryClient();
  const [expanded, setExpanded] = useState<string | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["mcpServers"],
    queryFn: api.mcpServers,
  });
  const { data: pending } = useQuery({
    queryKey: ["mcpPending"],
    queryFn: api.mcpPending,
  });

  const reload = useMutation({
    mutationFn: api.mcpReload,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["mcpServers"] });
      // 工具集/人格文件改了，固定开销跟着变 ——
      // 不失效的话上下文条显示的还是旧值。
      qc.invalidateQueries({ queryKey: ["contextOverhead"] });
      void qc.invalidateQueries({ queryKey: ["mcpPending"] });
    },
  });

  const totalTokens = (data?.items ?? []).reduce(
    (sum, s) => sum + (s.status === "ready" ? s.estimated_tokens : 0),
    0,
  );

  return (
    <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <div className="mb-1 flex items-center gap-2">
        <h2 className="text-sm font-medium">MCP 服务器</h2>
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
          没有配置 MCP 服务器。在 <code>config/mcp_servers.yaml</code> 里添加。
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
                  className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5"
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
                      {meta.label}
                    </span>
                    {s.status === "ready" && (
                      <button
                        type="button"
                        onClick={() => setExpanded(open ? null : s.server_id)}
                        className="shrink-0 text-[10px] text-[var(--color-muted)] hover:underline"
                      >
                        {s.tool_count} 个工具 · ~{s.estimated_tokens} token
                      </button>
                    )}
                  </div>
                  {s.error && (
                    <p className="mt-1 break-words text-[10px] text-[var(--color-err)]">
                      {s.error}
                    </p>
                  )}
                  {open && (
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
