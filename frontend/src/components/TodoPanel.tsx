import { Check, Circle, CircleDot, X } from "lucide-react";
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

  if (todos.length === 0) return null;

  const pct = stats && stats.total > 0 ? (stats.completed / stats.total) * 100 : 0;

  return (
    <aside className="w-64 shrink-0 overflow-y-auto border-l border-[var(--color-border)] bg-[var(--color-surface)] p-3">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-xs font-medium text-[var(--color-muted)]">任务清单</h2>
        {stats && (
          <span className="text-[11px] text-[var(--color-muted)]">
            {stats.completed}/{stats.total}
          </span>
        )}
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
