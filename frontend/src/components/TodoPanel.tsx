import { useState } from "react";
import { Check, Circle, CircleDot, PanelRightClose, PanelRightOpen, X } from "lucide-react";
import clsx from "clsx";
import { useChatStore } from "@/store/chat";
import type { TodoItem } from "@/lib/types";

const ICON = {
  completed: Check,
  in_progress: CircleDot,
  pending: Circle,
  cancelled: X,
} as const;

function Row({ item }: { item: TodoItem }) {
  const Icon = ICON[item.status];
  return (
    <li className="flex items-start gap-2 py-1">
      <Icon
        size={13}
        aria-hidden
        className={clsx(
          "mt-0.5 shrink-0",
          item.status === "completed" && "text-[var(--color-ok)]",
          item.status === "in_progress" && "text-[var(--color-accent)]",
          item.status === "pending" && "text-[var(--color-muted)]",
          item.status === "cancelled" && "text-[var(--color-muted)]",
        )}
      />
      <span
        className={clsx(
          "text-xs leading-relaxed",
          item.status === "completed" && "text-[var(--color-muted)] line-through",
          item.status === "cancelled" && "text-[var(--color-muted)] line-through",
          item.status === "in_progress" && "text-[var(--color-text)]",
          item.status === "pending" && "text-[var(--color-muted)]",
        )}
      >
        {item.content}
      </span>
    </li>
  );
}

export default function TodoPanel() {
  const todos = useChatStore((s) => s.todos);
  const stats = useChatStore((s) => s.todoStats);
  const [collapsed, setCollapsed] = useState(false);

  if (todos.length === 0) return null;

  // 收起态：右侧一条窄栏 + 展开按钮
  if (collapsed) {
    return (
      <aside className="flex w-8 shrink-0 flex-col items-center border-l border-[var(--color-border)] bg-[var(--color-surface)] py-3">
        <button
          type="button"
          onClick={() => setCollapsed(false)}
          title="展开任务清单"
          aria-label="展开任务清单"
          className="rounded p-1 text-[var(--color-muted)] transition hover:bg-[var(--color-surface-2)] hover:text-[var(--color-text)]"
        >
          <PanelRightOpen size={16} aria-hidden />
        </button>
      </aside>
    );
  }

  const pct = stats && stats.total > 0 ? (stats.completed / stats.total) * 100 : 0;

  return (
    <aside className="w-64 shrink-0 overflow-y-auto border-l border-[var(--color-border)] bg-[var(--color-surface)] p-3">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-xs font-medium text-[var(--color-muted)]">任务清单</h2>
        <div className="flex items-center gap-2">
          {stats && (
            <span className="text-[11px] text-[var(--color-muted)]">
              {stats.completed}/{stats.total}
            </span>
          )}
          <button
            type="button"
            onClick={() => setCollapsed(true)}
            title="收起任务清单"
            aria-label="收起任务清单"
            className="rounded p-0.5 text-[var(--color-muted)] transition hover:text-[var(--color-text)]"
          >
            <PanelRightClose size={14} aria-hidden />
          </button>
        </div>
      </div>
      <div className="mb-3 h-1 overflow-hidden rounded-full bg-[var(--color-border)]">
        <div
          className="h-full rounded-full bg-[var(--color-ok)] transition-all"
          style={{ width: `${pct}%` }}
        />
      </div>
      <ul>
        {todos.map((t) => (
          <Row key={t.id} item={t} />
        ))}
      </ul>
    </aside>
  );
}
