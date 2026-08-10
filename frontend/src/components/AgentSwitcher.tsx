import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Check, ChevronUp } from "lucide-react";
import clsx from "clsx";

import { api } from "../lib/api";
import { useChatStore } from "../store/chat";
import type { AgentItem } from "../lib/types";

/**
 * 对话页的智能体快捷切换。
 *
 * ## 替代 ModelSwitcher
 *
 * 智能体替代模型选择：选中一个智能体后，对话请求会带上 agent_id，
 * 后端据此决定使用哪个模型、system_prompt 和权限。
 *
 * ## 为什么只列可见的
 *
 * hidden=true 的智能体不在此菜单出现 —— 那是"还在配置中，先隐藏"，
 * 而不是禁用/启用（智能体没有启用字段，只有 hidden）。
 */
export function AgentSwitcher({
  sessionId,
  agentId,
}: {
  sessionId: string;
  agentId: string;
}) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [err, setErr] = useState("");

  const { data } = useQuery({
    queryKey: ["agents", "visible"],
    queryFn: () => api.agents.list(false),
    enabled: open,
  });

  // 【必须走 store 的 setter】。
  //
  // agentId 存在 zustand 里，而 invalidateQueries 只影响 react-query
  // 的缓存 —— 两套状态互不相干。之前只 invalidate 的结果是：
  // 请求发出去了、库里也改了，但按钮上的文字还是旧的，
  // 要刷新页面才对。用户看到的就是"切了没反应"。
  const setAgentId = useChatStore((s) => s.setAgentId);

  const pick = useMutation({
    mutationFn: (id: string) => setAgentId(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["session", sessionId] });
      setOpen(false);
    },
    onError: (e: Error) => setErr(e.message),
  });

  const items = data ?? [];
  const current = items.find((a: AgentItem) => a.id === agentId);

  // 折叠时的标签。没选过就显示"选择智能体"
  const label = current ? current.name : "选择智能体";

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex max-w-48 items-center gap-1.5 rounded px-2 py-1 text-xs"
        style={{
          background: "var(--color-surface-2)",
          color: agentId ? "var(--color-fg)" : "var(--color-muted)",
        }}
        title={
          current
            ? `${current.name}${current.description ? ` · ${current.description}` : ""}`
            : "这个对话不指定智能体，直接用功能位绑定的模型"
        }
        aria-expanded={open}
      >
        <Bot size={12} />
        <span className="truncate">{label}</span>
        <ChevronUp
          size={12}
          className={clsx("shrink-0 transition-transform", !open && "rotate-180")}
        />
      </button>

      {open && (
        <div
          className="absolute left-0 z-50 mt-1 max-h-80 w-72 overflow-y-auto rounded-lg border shadow-lg"
          style={{
            borderColor: "var(--color-border)",
            background: "var(--color-surface)",
          }}
          role="listbox"
          aria-label="选择智能体"
        >
          {/* 不指定智能体 */}
          <button
            onClick={() => pick.mutate("")}
            role="option"
            aria-selected={!agentId}
            className="flex w-full items-start gap-2 border-b px-3 py-2 text-left text-xs hover:opacity-80"
            style={{ borderColor: "var(--color-border)" }}
          >
            <div className="w-3.5 shrink-0 pt-0.5">
              {!agentId && <Check size={12} style={{ color: "var(--color-accent)" }} />}
            </div>
            <div className="min-w-0">
              <div>直接对话</div>
              <div className="truncate" style={{ color: "var(--color-muted)" }}>
                不指定智能体，直接用功能位绑定的模型
              </div>
            </div>
          </button>

          {err && (
            <p
              role="alert"
              className="border-b px-3 py-2 text-xs"
              style={{ borderColor: "var(--color-border)", color: "var(--color-err)" }}
            >
              {err}
            </p>
          )}

          {items.length === 0 ? (
            <p className="px-3 py-3 text-xs" style={{ color: "var(--color-muted)" }}>
              没有可见的智能体。去设置页创建或把隐藏的智能体设为可见。
            </p>
          ) : (
            items.map((a: AgentItem) => (
              <button
                key={a.id}
                onClick={() => pick.mutate(a.id)}
                role="option"
                aria-selected={a.id === agentId}
                disabled={pick.isPending}
                className="flex w-full items-start gap-2 px-3 py-2 text-left text-xs hover:opacity-80 disabled:opacity-50"
              >
                <div className="w-3.5 shrink-0 pt-0.5">
                  {a.id === agentId && (
                    <Check size={12} style={{ color: "var(--color-accent)" }} />
                  )}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5 truncate">
                    {a.avatar ? (
                      <span className="text-sm leading-none">{a.avatar}</span>
                    ) : null}
                    <span className="truncate font-medium">{a.name}</span>
                  </div>
                  {a.description && (
                    <div
                      className="truncate"
                      style={{ color: "var(--color-muted)" }}
                    >
                      {a.description}
                    </div>
                  )}
                </div>
              </button>
            ))
          )}

          <p
            className="border-t px-3 py-2 text-xs"
            style={{
              borderColor: "var(--color-border)",
              color: "var(--color-muted)",
            }}
          >
            智能体决定使用的模型、系统提示词和工具权限。选择后会替代默认模型。
          </p>
        </div>
      )}
    </div>
  );
}
