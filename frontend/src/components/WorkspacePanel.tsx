/**
 * 工作区管理：每个工作区可独立绑定执行环境（本机 / Docker 容器）。
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Box, Folder, Loader2, Pencil, Plus, Trash2 } from "lucide-react";
import clsx from "clsx";
import { api } from "@/lib/api";
import type { WorkspaceItem } from "@/lib/types";

const inputCls =
  "w-full rounded-lg border px-3 py-2 text-sm outline-none focus:border-[var(--color-accent)]";
const inputStyle = { borderColor: "var(--color-border)", background: "var(--color-bg)" } as const;

function ContainerBadge({ ws }: { ws: WorkspaceItem }) {
  if (ws.sandbox_backend !== "docker") {
    return <span className="rounded-full border px-2 py-0.5 text-xs text-[var(--color-muted)]" style={{ borderColor: "var(--color-border)" }}>本机</span>;
  }
  const st = ws.container_status;
  const color = st === "running" ? "var(--color-accent)" : st === "stopped" ? "var(--color-warn)" : "var(--color-muted)";
  const label = st === "running" ? "运行中" : st === "stopped" ? "已停止" : st === "not_found" ? "未创建" : "检测中";
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs" style={{ borderColor: color, color }}>
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />
      {ws.docker_container || "容器"} · {label}
    </span>
  );
}

export default function WorkspacePanel() {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<WorkspaceItem | "new" | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["workspaces"],
    queryFn: api.listWorkspaces,
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteWorkspace(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["workspaces"] }),
    onError: (e: Error) => alert(e.message),
  });

  if (isLoading || !data) {
    return <div className="flex items-center gap-2 text-sm text-[var(--color-muted)]"><Loader2 size={14} className="animate-spin" />加载中…</div>;
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-[var(--color-muted)]">
          每个工作区可独立选择执行环境：本机，或一个专属 Docker 容器（选工作区即自动创建/复用）。
        </p>
        <button type="button" onClick={() => setEditing("new")} className="flex items-center gap-1.5 rounded-lg bg-[var(--color-accent)] px-3 py-1.5 text-xs font-medium text-white transition hover:brightness-110">
          <Plus size={13} aria-hidden />新建工作区
        </button>
      </div>

      <div className="space-y-2">
        {data.map((ws) => (
          <div key={ws.id} className="flex flex-wrap items-center gap-3 rounded-lg border p-3" style={{ borderColor: "var(--color-border)", background: "var(--color-surface)" }}>
            <Folder size={16} className="shrink-0 text-[var(--color-muted)]" aria-hidden />
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="truncate text-sm font-medium" style={{ color: "var(--color-text)" }}>{ws.name}</span>
                {ws.is_default && <span className="rounded-full bg-[var(--color-surface-2)] px-2 py-0.5 text-xs text-[var(--color-muted)]">默认</span>}
              </div>
              <p className="truncate text-xs text-[var(--color-muted)]" title={ws.root_path}>{ws.root_path}</p>
            </div>
            <ContainerBadge ws={ws} />
            <div className="flex items-center gap-1">
              <button type="button" title="编辑" onClick={() => setEditing(ws)} className="rounded-md border p-1.5 transition hover:bg-[var(--color-surface-2)]" style={{ borderColor: "var(--color-border)", color: "var(--color-muted)" }}><Pencil size={13} aria-hidden /></button>
              {!ws.is_default && <button type="button" title="删除" onClick={() => { if (window.confirm(`删除工作区「${ws.name}」？`)) remove.mutate(ws.id); }} className="rounded-md border p-1.5 transition hover:bg-[var(--color-err)]/15" style={{ borderColor: "var(--color-border)", color: "var(--color-err)" }}><Trash2 size={13} aria-hidden /></button>}
            </div>
          </div>
        ))}
      </div>

      {editing && (
        <WorkspaceEditor ws={editing === "new" ? null : editing} onClose={() => setEditing(null)} />
      )}
    </div>
  );
}

export function WorkspaceEditor({ ws, onClose }: { ws: WorkspaceItem | null; onClose: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState(ws?.name ?? "");
  const [root, setRoot] = useState(ws?.root_path ?? "");
  const [backend, setBackend] = useState<"local" | "docker">(ws?.sandbox_backend ?? "local");
  const [container, setContainer] = useState(ws?.docker_container ?? "");
  const [image, setImage] = useState(ws?.docker_image ?? "python:3.12-slim");
  const [network, setNetwork] = useState<"none" | "bridge">(ws?.docker_network ?? "none");
  const [err, setErr] = useState("");

  const save = useMutation({
    mutationFn: () =>
      ws
        ? api.patchWorkspace(ws.id, { name, sandbox_backend: backend, docker_container: backend === "docker" ? container : "", docker_image: image, docker_network: network })
        : api.createWorkspace({ name, root_path: root, sandbox_backend: backend, docker_container: backend === "docker" ? container : "", docker_image: image, docker_network: network }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["workspaces"] });
      onClose();
    },
    onError: (e: Error) => setErr(e.message),
  });

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className="w-full max-w-md space-y-3 rounded-xl border p-4" style={{ borderColor: "var(--color-border)", background: "var(--color-surface)" }} onClick={(e) => e.stopPropagation()}>
        <h3 className="text-sm font-medium" style={{ color: "var(--color-text)" }}>{ws ? "编辑工作区" : "新建工作区"}</h3>

        <label className="block">
          <span className="mb-1 block text-xs text-[var(--color-muted)]">名称</span>
          <input className={inputCls} style={inputStyle} value={name} onChange={(e) => setName(e.target.value)} placeholder="如：我的项目" />
        </label>

        {!ws && (
          <label className="block">
            <span className="mb-1 block text-xs text-[var(--color-muted)]">根目录（绝对路径）</span>
            <input className={inputCls} style={inputStyle} value={root} onChange={(e) => setRoot(e.target.value)} placeholder="D:/projects/my-app" />
          </label>
        )}

        <div>
          <span className="mb-1 block text-xs text-[var(--color-muted)]">执行环境</span>
          <div className="flex gap-2">
            {(["local", "docker"] as const).map((b) => (
              <button key={b} type="button" onClick={() => setBackend(b)} className={clsx("flex-1 rounded-lg border px-3 py-2 text-xs transition", backend === b ? "border-[var(--color-accent)] text-[var(--color-text)]" : "border-[var(--color-border)] text-[var(--color-muted)]")}>
                {b === "local" ? "本机" : "Docker 容器"}
              </button>
            ))}
          </div>
        </div>

        {backend === "docker" && (
          <div className="space-y-2">
            <label className="block">
              <span className="mb-1 block text-xs text-[var(--color-muted)]">容器名（唯一，如 my-app-box）</span>
              <input className={inputCls} style={inputStyle} value={container} onChange={(e) => setContainer(e.target.value)} placeholder="my-app-box" />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs text-[var(--color-muted)]">镜像</span>
              <input className={inputCls} style={inputStyle} value={image} onChange={(e) => setImage(e.target.value)} placeholder="python:3.12-slim" />
            </label>
            <div>
              <span className="mb-1 block text-xs text-[var(--color-muted)]">网络</span>
              <select className={inputCls} style={inputStyle} value={network} onChange={(e) => setNetwork(e.target.value as "none" | "bridge")}>
                <option value="none">none（隔离，不能联网）</option>
                <option value="bridge">bridge（可联网装包）</option>
              </select>
            </div>
          </div>
        )}

        {err && <p className="text-xs text-[var(--color-err)]">{err}</p>}

        <div className="flex justify-end gap-2">
          <button type="button" onClick={onClose} className="rounded-lg border px-3 py-1.5 text-xs transition hover:bg-[var(--color-surface-2)]" style={{ borderColor: "var(--color-border)", color: "var(--color-muted)" }}>取消</button>
          <button type="button" disabled={save.isPending || !name.trim() || (!ws && !root.trim()) || (backend === "docker" && !container.trim())} onClick={() => save.mutate()} className="flex items-center gap-1.5 rounded-lg bg-[var(--color-accent)] px-3 py-1.5 text-xs font-medium text-white transition hover:brightness-110 disabled:opacity-50">
            {save.isPending ? <Loader2 size={13} className="animate-spin" /> : <Box size={13} />}保存
          </button>
        </div>
      </div>
    </div>
  );
}
