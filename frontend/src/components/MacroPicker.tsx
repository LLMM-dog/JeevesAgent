import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";

/**
 * 宏提词器。输入框以 `!` 或 `！` 开头时弹出。
 *
 * ## 为什么支持全角 `！`
 *
 * 中文输入法下打感叹号默认出来的是全角。只认半角的话中文用户按了没反应，
 * 而且完全想不到是因为标点宽度 —— 这类问题的排查成本极高，支持成本极低。
 *
 * ## 为什么宏不进系统提示词
 *
 * 宏是用户主动触发的，模型不需要"知道它存在"。常驻位很贵（技能的 L1 实测
 * 2156 字符），而个人工作流会攒到几十个宏。用户按 `!` 时自己会选。
 */
export function MacroPicker({
  query,
  onPick,
  onClose,
}: {
  /** `!` 后面已经输入的过滤词 */
  query: string;
  onPick: (body: string) => void;
  onClose: () => void;
}) {
  const [active, setActive] = useState(0);
  const listRef = useRef<HTMLUListElement>(null);

  const { data } = useQuery({ queryKey: ["macros"], queryFn: api.listMacros });

  const q = query.trim().toLowerCase();
  const items = (data?.items ?? []).filter(
    (m) =>
      !q ||
      m.name.toLowerCase().includes(q) ||
      m.description.toLowerCase().includes(q) ||
      m.keywords.some((k) => k.toLowerCase().includes(q)),
  );

  // 过滤结果变化时把选中项拉回顶部，否则 active 可能指向已被过滤掉的项
  useEffect(() => {
    setActive(0);
  }, [q, data]);

  useEffect(() => {
    const onKey = async (e: KeyboardEvent) => {
      if (items.length === 0) {
        if (e.key === "Escape") onClose();
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActive((i) => (i + 1) % items.length);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setActive((i) => (i - 1 + items.length) % items.length);
      } else if (e.key === "Tab" || e.key === "Enter") {
        // Tab 和 Enter 都确认。提词器打开时拦下 Enter ——
        // 否则用户以为在选宏，实际把 "!daily" 当消息发出去了。
        e.preventDefault();
        const picked = items[active];
        if (picked) {
          const detail = await api.getMacro(picked.name);
          onPick(detail.body);
        }
      } else if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [items, active, onPick, onClose]);

  if (!data) return null;

  return (
    <div
      className="absolute bottom-full left-0 right-0 mb-2 max-h-64 overflow-auto rounded-xl border border-[var(--color-border)] bg-[var(--color-bg)] shadow-lg"
      role="listbox"
      aria-label="宏列表"
    >
      {items.length === 0 ? (
        <div className="px-3 py-3 text-xs text-[var(--color-muted)]">
          {data.items.length === 0
            ? "还没有宏。对我说“把这个流程存成宏”，我会帮你建。"
            : `没有匹配 “${query}” 的宏`}
        </div>
      ) : (
        <ul ref={listRef} className="py-1">
          {items.map((m, i) => (
            <li
              key={m.name}
              role="option"
              aria-selected={i === active}
              onMouseEnter={() => setActive(i)}
              onClick={() => {
                void api.getMacro(m.name).then((d) => onPick(d.body));
              }}
              className={`cursor-pointer px-3 py-2 ${
                i === active ? "bg-[var(--color-surface)]" : ""
              }`}
            >
              <div className="text-xs font-medium">{m.name}</div>
              <div className="mt-0.5 line-clamp-2 text-[11px] text-[var(--color-muted)]">
                {m.description}
              </div>
            </li>
          ))}
        </ul>
      )}
      <div className="border-t border-[var(--color-border)] px-3 py-1.5 text-[10px] text-[var(--color-muted)]">
        上下键选择 · Tab 确认 · Esc 取消
      </div>
    </div>
  );
}
