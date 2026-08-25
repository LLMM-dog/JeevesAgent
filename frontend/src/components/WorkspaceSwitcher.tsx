/**
 * 对话页的工作区快捷切换。
 *
 * 选工作区 = 换根目录 + 执行环境（本机 / Docker 容器）。
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronUp, Folder, Plus } from "lucide-react";
import clsx from "clsx";
import { api } from "@/lib/api";
import { WorkspaceEditor } from "./WorkspacePanel";
import type { WorkspaceItem } from "@/lib/types";

export function WorkspaceSwitcher({ sessionId }: { sessionId: string }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [err, setErr] = useState("");
  const [creating, setCreating] = useState(false);

  const { data: session } = useQuery({
    queryKey: ["session", sessionId],
    queryFn: () => api.getSession(sessionId),
  });
  const { data: workspaces, isLoading } = useQuery({
    queryKey: ["workspaces"],
    queryFn: api.listWorkspaces,
    enabled: open,
  });

  const pick = useMutation({
    mutationFn: (ws: WorkspaceItem) =>
      api.patchSession(sessionId, { workspace_id: ws.id }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["session", sessionId] });
      setOpen(false);
    },
    onError: (e: Error) => setErr(e.message),
  });

  const items = workspaces ?? [];
  const current = items.find((w) => w.id === session?.workspace_id);
  const label = current ? current.name : isLoading ? "检测中…" : "选择工作区";

  return (
    <div className="relative">
      <button onClick={() => setOpen((v) => !v)} className="flex max-w-44 items-center gap-1.5 rounded px-2 py-1 text-xs" style={{ background: "var(--color-surface-2)", color: current ? "var(--color-fg)" : "var(--color-muted)" }} aria-expanded={open}>
        <Folder size={12} />
        <span className="truncate">{label}</span>
        <ChevronUp size={12} className={clsx("transition-transform", open && "rotate-180")} />
      </button>

      {open && (
        <div className="absolute left-0 top-full z-50 mt-1 w-72 rounded-lg border p-1 shadow-xl" style={{ borderColor: "var(--color-border)", background: "var(--color-surface)" }}>
          {items.map((ws) => (
            <button key={ws.id} type="button" onClick={() => pick.mutate(ws)} className="flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-xs transition hover:bg-[var(--color-surface-2)]">
              <span className="min-w-0 flex-1">
                <span className="block truncate" style={{ color: "var(--color-text)" }}>{ws.name}</span>
                <span className="block truncate text-[10px] text-[var(--color-muted)]">{ws.root_path}</span>
              </span>
              <WorkspaceBadge ws={ws} />
              {ws.id === session?.workspace_id && <Check size={13} className="shrink-0 text-[var(--color-accent)]" aria-hidden />}
            </button>
          ))}
          <button type="button" onClick={() => { setOpen(false); setCreating(true); }} className="mt-1 flex w-full items-center gap-2 rounded-md border-t px-2 py-2 text-left text-xs text-[var(--color-accent)] transition hover:bg-[var(--color-surface-2)]" style={{ borderColor: "var(--color-border)" }}>
            <Plus size={13} aria-hidden />新建工作区
          </button>
          {err && <p className="px-2 py-1 text-[10px] text-[var(--color-err)]">{err}</p>}
        </div>
      )}
      {creating && <WorkspaceEditor ws={null} onClose={() => { setCreating(false); }} />}
    </div>
  );
}

function WorkspaceBadge({ ws }: { ws: WorkspaceItem }) {
  if (ws.sandbox_backend !== "docker") {
    return <span className="shrink-0 rounded-full bg-[var(--color-surface-2)] px-1.5 py-0.5 text-[10px] text-[var(--color-muted)]">本机</span>;
  }
  const st = ws.container_status;
  const color = st === "running" ? "var(--color-accent)" : st === "stopped" ? "var(--color-warn)" : "var(--color-muted)";
  const label = st === "running" ? "运行" : st === "stopped" ? "停止" : st === "not_found" ? "未建" : "…";
  return (
    <span className="inline-flex shrink-0 items-center gap-1 rounded-full px-1.5 py-0.5 text-[10px]" style={{ color }}>
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />
      {label}
    </span>
  );
}
