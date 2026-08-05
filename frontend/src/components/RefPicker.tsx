import { useQuery } from "@tanstack/react-query";
import { useChatStore } from "@/store/chat";
import { File, Folder, Sparkles, Wrench } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";

/**
 * 引用提词器。输入 `@` 弹文件、`@@` 弹技能、`#` 弹工具。
 *
 * ## 为什么 Enter 也要确认
 *
 * 提词器只认 Tab，它的 Enter 分支
 * 注释写着"仅当面板未打开时"但**代码里没有这个判断**——
 * 于是打 `@`、弹出候选、按 Enter，消息直接发出去了，候选项没选上。
 *
 * Enter 是候选面板的默认确认键（所有 IDE 都是）。只支持 Tab 的话
 * 用户第一次用一定会误发消息。
 *
 * ## 为什么用 capture 阶段监听
 *
 * 要在 textarea 的 onKeyDown 之前拦下 Enter。冒泡阶段的话
 * textarea 已经处理完并触发发送了。
 */

export type RefKind = "file" | "skill" | "tool";

export interface RefCandidate {
  name: string;
  path?: string;
  detail: string;
}

const KIND_META: Record<
  RefKind,
  { label: string; icon: typeof File; empty: string }
> = {
  file: { label: "文件", icon: File, empty: "没有匹配的文件" },
  skill: { label: "技能", icon: Sparkles, empty: "没有匹配的技能" },
  tool: { label: "工具", icon: Wrench, empty: "没有匹配的工具" },
};

export function RefPicker({
  kind,
  query,
  onPick,
  onClose,
}: {
  kind: RefKind;
  /** 触发符后面已输入的过滤词 */
  query: string;
  onPick: (item: RefCandidate) => void;
  onClose: () => void;
}) {
  // 文件候选的搜索范围来自会话的工作目录，所以要带上 session_id。
  // 不带的话后端不知道搜哪里，只能返回空 —— 那正是之前
  // "@ 永远显示没有匹配的文件" 的原因。
  const sessionId = useChatStore((s) => s.sessionId);
  const [active, setActive] = useState(0);
  const listRef = useRef<HTMLUListElement>(null);

  // 文件候选必须走后端搜索 —— 文件可能上万个，前端拉全量不现实。
  // 技能/工具是几十个量级，但为了接口统一也走同一个端点。
  const { data, isError } = useQuery({
    queryKey: ["refCandidates", kind, query, sessionId],
    queryFn: () => api.refCandidates(kind, query, sessionId ?? undefined),
    // 打字时频繁触发，短暂缓存避免每个字符都打一次库
    staleTime: 3000,
  });

  const items = data?.items ?? [];

  useEffect(() => {
    setActive(0);
  }, [query, kind, data]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
        return;
      }
      if (items.length === 0) return;

      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActive((i) => (i + 1) % items.length);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setActive((i) => (i - 1 + items.length) % items.length);
      } else if (e.key === "Tab" || e.key === "Enter") {
        // Enter 和 Tab 都确认。
        //
        // 【必须拦下 Enter】—— 不拦的话用户以为在选候选项，
        // 实际把 "@main" 当消息发出去了。这正是 bug。
        e.preventDefault();
        const picked = items[active];
        if (picked) onPick(picked);
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [items, active, onPick, onClose]);

  const meta = KIND_META[kind];
  const Icon = meta.icon;

  // 加载失败要显示出来。
  //
  // 三个 load 函数失败都只 console.error（:459/:468/:477），
  // 提词器静默失效 —— 用户打 @ 没反应，分不清"没这功能"和"加载挂了"。
  if (isError) {
    return (
      <div className="absolute bottom-full left-0 right-0 mb-2 rounded-xl border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 text-xs text-[var(--color-err)] shadow-lg">
        {meta.label}候选加载失败，检查后端是否在运行
      </div>
    );
  }

  if (!data) return null;

  return (
    <div
      className="absolute bottom-full left-0 right-0 mb-2 max-h-64 overflow-auto rounded-xl border border-[var(--color-border)] bg-[var(--color-bg)] shadow-lg"
      role="listbox"
      aria-label={`${meta.label}候选`}
    >
      <div className="flex items-center gap-1 border-b border-[var(--color-border)] px-3 py-1 text-[10px] text-[var(--color-muted)]">
        <Icon size={10} aria-hidden />
        {meta.label}
        <span className="ml-auto">↑↓ 选择 · Enter/Tab 确认 · Esc 取消</span>
      </div>
      {items.length === 0 ? (
        <div className="px-3 py-3 text-xs text-[var(--color-muted)]">
          {/* 区分"没设工作目录"和"目录里真的没有匹配"。
              一律显示"没有匹配的文件"的话，用户不知道要先去设工作目录 ——
              那正是这个提示存在的原因。 */}
          {data?.reason === "no_work_dir"
            ? (data.hint ?? "这个对话还没设置工作目录")
            : query ? `没有匹配 “${query}” 的${meta.label}` : meta.empty}
        </div>
      ) : (
        <ul ref={listRef} className="py-1">
          {items.map((it, i) => (
            <li
              key={it.path ?? it.name}
              role="option"
              aria-selected={i === active}
              onMouseEnter={() => setActive(i)}
              onClick={() => onPick(it)}
              className={`cursor-pointer px-3 py-1.5 ${
                i === active ? "bg-[var(--color-surface)]" : ""
              }`}
            >
              <div className="flex items-center gap-1.5">
                {kind === "file" && it.path?.endsWith("/") ? (
                  <Folder size={11} className="shrink-0 text-[var(--color-muted)]" aria-hidden />
                ) : (
                  <Icon size={11} className="shrink-0 text-[var(--color-muted)]" aria-hidden />
                )}
                <span className="truncate text-xs">{it.name}</span>
              </div>
              {it.detail && it.detail !== it.name && (
                <div className="truncate pl-[18px] text-[10px] text-[var(--color-muted)]">
                  {it.detail}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
