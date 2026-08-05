import { Bot, ChevronRight, Loader2, TriangleAlert } from "lucide-react";
import { useState } from "react";

import type { AgentCard } from "@/store/chat";

/**
 * 子智能体卡片。默认折叠，点开看它内部的工具调用。
 *
 * ## 为什么默认折叠
 *
 * 委派的意义就是"这些细节你不用管"。默认展开的话，界面上仍然是一大堆
 * 文件读取记录，和不委派时一样吵 —— 那委派在体验上就没有收益。
 *
 * 但要能展开：调试子智能体时必须能看到它到底读了什么。
 */
export function SubAgentCards({ agents }: { agents: AgentCard[] }) {
  if (agents.length === 0) return null;
  return (
    <div className="space-y-1.5">
      {agents.map((a) => (
        <SubAgentCard key={a.span_id || a.agent_name} agent={a} />
      ))}
    </div>
  );
}

function SubAgentCard({ agent }: { agent: AgentCard }) {
  const [open, setOpen] = useState(false);
  const done = agent.status !== "running";

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)]/60">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left"
      >
        <ChevronRight
          size={12}
          className={`shrink-0 text-[var(--color-muted)] transition-transform ${
            open ? "rotate-90" : ""
          }`}
          aria-hidden
        />
        {agent.status === "running" ? (
          <Loader2
            size={12}
            className="shrink-0 animate-spin text-[var(--color-accent)]"
            aria-hidden
          />
        ) : agent.status === "error" ? (
          <TriangleAlert size={12} className="shrink-0 text-[var(--color-err)]" aria-hidden />
        ) : (
          <Bot size={12} className="shrink-0 text-[var(--color-muted)]" aria-hidden />
        )}

        <span className="shrink-0 text-xs font-medium">{agent.agent_name}</span>

        {/* 任务摘要。子智能体的价值是"我不用看细节"，
            所以这里只给一行，让用户知道派它去干什么就够了 */}
        <span className="min-w-0 flex-1 truncate text-[11px] text-[var(--color-muted)]">
          {agent.task}
        </span>

        {agent.tools.length > 0 && (
          <span className="shrink-0 text-[10px] text-[var(--color-muted)]">
            {agent.tools.length} 次调用
          </span>
        )}
        {/* 委派的成本必须可见 —— 否则用户不知道这次委派值不值。
            少见实现做了 token 聚合 */}
        {agent.tokens != null && agent.tokens > 0 && (
          <span className="shrink-0 text-[10px] text-[var(--color-muted)]">
            {/* 单位写全。"5.4Ktok" 挤在一起、缩写没人认得，
                也看不出是不是 5.4 千个 token。
                一万以下不缩写 —— "1.2K token" 比 "1200 token" 难读。 */}
            {agent.tokens >= 10000
              ? `${(agent.tokens / 1000).toFixed(1)}K token`
              : `${agent.tokens.toLocaleString()} token`}
          </span>
        )}
        {!done && (
          <span className="shrink-0 text-[10px] text-[var(--color-accent)]">
            进行中
          </span>
        )}
      </button>

      {open && (
        <div className="border-t border-[var(--color-border)] px-2.5 py-1.5">
          {agent.tools.length === 0 ? (
            <div className="text-[11px] text-[var(--color-muted)]">
              还没有工具调用
            </div>
          ) : (
            <ul className="space-y-1">
              {agent.tools.map((t) => (
                <li
                  key={t.call_id}
                  className="flex items-center gap-1.5 text-[11px]"
                >
                  <span
                    className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                      t.status === "running"
                        ? "bg-[var(--color-accent)]"
                        : t.status === "error"
                          ? "bg-[var(--color-err)]"
                          : "bg-[var(--color-ok)]"
                    }`}
                    aria-hidden
                  />
                  <span className="font-mono">{t.tool_name}</span>
                  {t.duration_ms != null && (
                    <span className="text-[10px] text-[var(--color-muted)]">
                      {t.duration_ms}ms
                    </span>
                  )}
                  {t.status === "error" && t.content_preview && (
                    <span className="min-w-0 flex-1 truncate text-[10px] text-[var(--color-err)]">
                      {t.content_preview}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
