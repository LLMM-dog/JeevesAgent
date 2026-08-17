/**
 * 智能体管理面板。
 *
 * 卡片右上角是「启用」滑块（控制是否在对话页显示，即 hidden 的反义）。
 * 新建和编辑都走子窗口（AgentEditorDialog），不在当前页内联展开。
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Plus, Trash2 } from "lucide-react";
import clsx from "clsx";

import { api } from "@/lib/api";
import type { AgentItem } from "@/lib/types";
import { AgentEditorDialog } from "./AgentEditorDialog";

export default function AgentsPanel() {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<{ agent: AgentItem | null } | null>(null);
  const [err, setErr] = useState("");

  const { data: agents } = useQuery({
    queryKey: ["agents", "all"],
    queryFn: () => api.agents.list(),
  });

  const { data: modelsData } = useQuery({
    queryKey: ["models", "all"],
    queryFn: () => api.models(),
  });

  const { data: skillsData } = useQuery({
    queryKey: ["skills"],
    queryFn: () => api.listSkills(),
  });

  const { data: mcpData } = useQuery({
    queryKey: ["mcp", "servers"],
    queryFn: () => api.mcpServers(),
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["agents"], exact: false });
  };

  const toggleEnabled = useMutation({
    mutationFn: (a: AgentItem) => api.agents.update(a.id, { hidden: !a.hidden }),
    onSuccess: invalidate,
    onError: (e: Error) => setErr(e.message),
  });

  const delAgent = useMutation({
    mutationFn: (id: string) => api.agents.delete(id),
    onSuccess: invalidate,
    onError: (e: Error) => setErr(e.message),
  });

  const allAgents = agents ?? [];
  const allModels = modelsData?.items ?? [];
  const allSkills = (skillsData?.items ?? []).map((s) => ({
    name: s.name,
    description: s.description,
  }));
  const allMcp = (mcpData?.items ?? []).map((m) => ({
    server_id: m.server_id,
    status: m.status,
    tool_count: m.tool_count,
  }));

  return (
    <section className="space-y-4">
      <header className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-base font-medium">智能体</h2>
          <p className="mt-1 text-sm" style={{ color: "var(--color-muted)" }}>
            每个智能体绑定不同的模型、系统提示词、工具权限、技能与 MCP。
            启用后才会出现在对话页的切换菜单里。
          </p>
        </div>
        <button
          type="button"
          onClick={() => setEditing({ agent: null })}
          className="flex shrink-0 items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm text-white transition hover:opacity-90"
          style={{ background: "var(--color-accent)" }}
        >
          <Plus size={14} />
          新建智能体
        </button>
      </header>

      {err && (
        <p role="alert" className="rounded border px-3 py-2 text-sm" style={{ borderColor: "var(--color-err)", color: "var(--color-err)" }}>
          {err}
        </p>
      )}

      {allAgents.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed py-14 text-center" style={{ borderColor: "var(--color-border)" }}>
          <p className="text-sm" style={{ color: "var(--color-muted)" }}>还没有智能体</p>
          <p className="mt-1 text-xs" style={{ color: "var(--color-muted)" }}>点右上角「新建智能体」创建第一个</p>
        </div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {allAgents.map((a: AgentItem) => (
            <div
              key={a.id}
              className={clsx(
                "flex flex-col gap-2 rounded-lg border p-3 transition-colors",
                a.hidden ? "opacity-60" : "bg-[var(--color-surface)]",
              )}
              style={{ borderColor: "var(--color-border)" }}
            >
              <div className="flex items-start gap-2">
                <button
                  type="button"
                  onClick={() => setEditing({ agent: a })}
                  className="min-w-0 flex-1 text-left"
                >
                  <div className="flex items-center gap-1.5">
                    {a.avatar && <span className="text-lg leading-none">{a.avatar}</span>}
                    <span className="truncate text-sm font-medium">{a.name}</span>
                    {a.is_default && (
                      <span className="shrink-0 rounded bg-[var(--color-surface-2)] px-1.5 py-0.5 text-[10px]" style={{ color: "var(--color-muted)" }}>
                        默认
                      </span>
                    )}
                  </div>
                  {a.description && (
                    <p className="mt-0.5 truncate text-xs" style={{ color: "var(--color-muted)" }}>
                      {a.description}
                    </p>
                  )}
                </button>

                {/* 启用滑块（右上角）。启用 = 在对话页显示 */}
                <button
                  type="button"
                  role="switch"
                  aria-checked={!a.hidden}
                  aria-label={a.hidden ? `启用 ${a.name}` : `禁用 ${a.name}`}
                  title={a.hidden ? "已禁用：不出现在对话页切换菜单" : "已启用：在对话页切换菜单里可选"}
                  onClick={() => toggleEnabled.mutate(a)}
                  disabled={toggleEnabled.isPending}
                  className="shrink-0 pt-0.5"
                >
                  <span
                    className={clsx(
                      "relative inline-flex h-5 w-9 items-center rounded-full transition-colors",
                      !a.hidden ? "bg-[var(--color-accent)]" : "bg-[var(--color-surface-2)]",
                    )}
                    style={{ boxShadow: a.hidden ? "inset 0 0 0 1px var(--color-border)" : undefined }}
                  >
                    <span
                      className={clsx(
                        "inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform",
                        !a.hidden ? "translate-x-[18px]" : "translate-x-[3px]",
                      )}
                    />
                  </span>
                </button>
              </div>

              {/* 底部：编辑 + 删除 */}
              <div className="flex items-center justify-end gap-1">
                <button
                  type="button"
                  onClick={() => setEditing({ agent: a })}
                  className="inline-flex items-center gap-1 rounded px-2 py-1 text-xs hover:bg-[var(--color-surface-2)]"
                  style={{ color: "var(--color-muted)" }}
                >
                  <Pencil size={12} />
                  编辑
                </button>
                <button
                  type="button"
                  onClick={() => {
                    if (
                      confirm(
                        `删除智能体「${a.name}」？不可恢复。` +
                          (a.is_default ? "\n它是默认智能体，删除后需重新指定。" : ""),
                      )
                    ) {
                      delAgent.mutate(a.id);
                    }
                  }}
                  disabled={delAgent.isPending}
                  className="rounded p-1.5 hover:bg-[var(--color-surface-2)]"
                  style={{ color: "var(--color-muted)" }}
                >
                  <Trash2 size={13} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {editing && (
        <AgentEditorDialog
          key={editing.agent?.id ?? "new"}
          agent={editing.agent}
          models={allModels}
          skills={allSkills}
          mcpServers={allMcp}
          onDone={() => setEditing(null)}
          onClose={() => setEditing(null)}
        />
      )}
    </section>
  );
}
