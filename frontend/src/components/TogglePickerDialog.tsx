/**
 * 通用「勾选启用」选择器子窗口。
 *
 * 技能和 MCP 服务器共用：从一组候选中勾选要启用到当前智能体的项。
 * 取消勾选 = 从智能体移除，不是真的删除（真删除在各自的设置页）。
 *
 * 子窗口必须显示 name + detail（简略信息），让用户能筛选 ——
 * 只列名字的话用户不知道这个技能是干什么的。
 */

import { useState } from "react";
import { Check, Search, X } from "lucide-react";

export interface PickerItem {
  id: string;
  name: string;
  /** 简略信息：技能是描述，MCP 是工具数/状态 */
  detail: string;
}

export function TogglePickerDialog({
  title,
  hint,
  items,
  selected,
  onConfirm,
  onClose,
}: {
  title: string;
  hint?: string;
  items: PickerItem[];
  selected: Set<string>;
  onConfirm: (next: Set<string>) => void;
  onClose: () => void;
}) {
  const [draft, setDraft] = useState<Set<string>>(() => new Set(selected));
  const [q, setQ] = useState("");

  const query = q.trim().toLowerCase();
  const filtered = items.filter((it) => {
    if (!query) return true;
    return (
      it.name.toLowerCase().includes(query) ||
      it.detail.toLowerCase().includes(query)
    );
  });

  const toggle = (id: string) => {
    setDraft((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const allChecked = items.length > 0 && filtered.every((it) => draft.has(it.id));

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div
        className="flex max-h-[85vh] w-full max-w-md flex-col overflow-hidden rounded-xl border bg-[var(--color-surface)] shadow-2xl"
        style={{ borderColor: "var(--color-border)" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between border-b px-5 py-4" style={{ borderColor: "var(--color-border)" }}>
          <div>
            <h2 className="text-base font-medium">{title}</h2>
            {hint && (
              <p className="mt-0.5 text-xs" style={{ color: "var(--color-muted)" }}>
                {hint}
              </p>
            )}
          </div>
          <button type="button" onClick={onClose} aria-label="关闭" className="rounded p-1 hover:bg-[var(--color-surface-2)]">
            <X size={16} style={{ color: "var(--color-muted)" }} />
          </button>
        </div>

        <div className="border-b px-5 py-2" style={{ borderColor: "var(--color-border)" }}>
          <div className="relative">
            <Search size={12} className="absolute left-2 top-1/2 -translate-y-1/2" style={{ color: "var(--color-muted)" }} />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="搜索名称或描述…"
              autoFocus
              className="w-full rounded-md border py-1.5 pl-7 pr-2 text-sm outline-none focus:border-[var(--color-accent)]"
              style={{ borderColor: "var(--color-border)", background: "var(--color-bg)" }}
            />
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
          {filtered.length === 0 ? (
            <p className="px-2 py-6 text-center text-xs" style={{ color: "var(--color-muted)" }}>
              {items.length === 0 ? "没有可用的项" : "没有匹配的项"}
            </p>
          ) : (
            <ul className="space-y-0.5">
              {filtered.map((it) => {
                const on = draft.has(it.id);
                return (
                  <li key={it.id}>
                    <button
                      type="button"
                      onClick={() => toggle(it.id)}
                      className="flex w-full items-start gap-2 rounded px-2 py-1.5 text-left hover:bg-[var(--color-surface-2)]"
                    >
                      <span
                        className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border"
                        style={{
                          borderColor: "var(--color-border)",
                          background: on ? "var(--color-accent)" : "transparent",
                        }}
                      >
                        {on && <Check size={11} className="text-white" />}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm">{it.name}</span>
                        {it.detail && (
                          <span className="block truncate text-xs" style={{ color: "var(--color-muted)" }}>
                            {it.detail}
                          </span>
                        )}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        <div className="flex items-center justify-between border-t px-5 py-3" style={{ borderColor: "var(--color-border)" }}>
          <button
            type="button"
            onClick={() =>
              setDraft(allChecked ? new Set() : new Set(items.map((it) => it.id)))
            }
            className="text-xs hover:underline"
            style={{ color: "var(--color-muted)" }}
          >
            {allChecked ? "清空" : "全选"}
          </button>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg border px-3 py-1.5 text-sm hover:bg-[var(--color-surface-2)]"
              style={{ borderColor: "var(--color-border)" }}
            >
              取消
            </button>
            <button
              type="button"
              onClick={() => onConfirm(draft)}
              className="rounded-lg px-3 py-1.5 text-sm text-white"
              style={{ background: "var(--color-accent)" }}
            >
              确定（{draft.size}）
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
