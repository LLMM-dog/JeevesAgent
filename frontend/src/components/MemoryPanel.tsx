import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  Brain,
  ChevronRight,
  History,
  Loader2,
  Plus,
  RotateCcw,
  Search,
  User,
  Wrench,
} from "lucide-react";
import { useState } from "react";

import { api } from "@/lib/api";
import type { MemoryOut } from "@/lib/types";

/**
 * 长期记忆面板。
 *
 * ## 为什么用户必须能看到并编辑全部记忆
 *
 * 记忆是模型自动提炼的，一定会记错。记错了而用户看不见、改不了，
 * 那这个错误会在之后每一轮对话里持续生效 —— 这是记忆功能最大的风险。
 *
 * 少见实现做了记忆管理界面
 *（`MemoryGrid.vue` / `MemoryPanel.vue`）。有的实现是让人直接编辑
 * `AGENTS.md` 文件，没有记忆功能。
 *
 * ## 为什么要显示变更历史
 *
 * "AI 为什么以为我喜欢 X" 只能靠 history 回答。它记下了每次改动的
 * 原因和改之前的值。
 */

const SOURCE_META: Record<string, { label: string; icon: typeof User }> = {
  manual: { label: "你添加的", icon: User },
  tool: { label: "AI 主动记的", icon: Brain },
  auto: { label: "自动提炼", icon: Wrench },
};

function fmtTime(ms: number): string {
  return new Date(ms).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function MemoryRow({ m }: { m: MemoryOut }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(m.content);
  const [showHistory, setShowHistory] = useState(false);

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["memories"] });
  };

  const save = useMutation({
    mutationFn: () => api.updateMemory(m.id, { content: draft }),
    onSuccess: () => {
      setEditing(false);
      refresh();
    },
  });
  const archive = useMutation({
    mutationFn: () => api.archiveMemory(m.id),
    onSuccess: refresh,
  });
  const restore = useMutation({
    mutationFn: () => api.restoreMemory(m.id),
    onSuccess: refresh,
  });

  const meta = SOURCE_META[m.source] ?? SOURCE_META.auto;
  const Icon = meta.icon;

  return (
    <li
      className={`rounded-lg border border-[var(--color-border)] px-2.5 py-1.5 ${
        m.archived ? "opacity-50" : "bg-[var(--color-bg)]"
      }`}
    >
      <div className="flex items-start gap-2">
        <Icon
          size={12}
          className="mt-1 shrink-0 text-[var(--color-muted)]"
          aria-hidden
        />
        <div className="min-w-0 flex-1">
          {editing ? (
            <div className="space-y-1">
              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                rows={2}
                className="w-full resize-none rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-1.5 py-1 text-xs outline-none focus:border-[var(--color-accent)]"
              />
              <div className="flex gap-1.5">
                <button
                  type="button"
                  onClick={() => save.mutate()}
                  disabled={save.isPending || !draft.trim()}
                  className="rounded bg-[var(--color-accent)] px-2 py-0.5 text-[11px] text-white disabled:opacity-50"
                >
                  保存
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setDraft(m.content);
                    setEditing(false);
                  }}
                  className="rounded border border-[var(--color-border)] px-2 py-0.5 text-[11px]"
                >
                  取消
                </button>
              </div>
            </div>
          ) : (
            <p className="break-words text-xs">{m.content}</p>
          )}

          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] text-[var(--color-muted)]">
            <span className="rounded bg-[var(--color-surface)] px-1">
              {m.theme}
            </span>
            <span>{meta.label}</span>
            {/* 置信度低的要显眼 —— 它们是记忆污染的主要来源 */}
            {m.confidence < 0.7 && (
              <span className="text-[var(--color-warn)]">待确认</span>
            )}
            {m.hit > 0 && <span>用过 {m.hit} 次</span>}
            <span>{fmtTime(m.updated_at)}</span>
            {m.history.length > 0 && (
              <button
                type="button"
                onClick={() => setShowHistory((v) => !v)}
                className="flex items-center gap-0.5 hover:text-[var(--color-fg)]"
              >
                <History size={9} aria-hidden />
                改过 {m.history.length} 次
              </button>
            )}
          </div>

          {/* 变更历史。"AI 为什么以为我喜欢 X" 只能靠它回答 */}
          {showHistory && (
            <ul className="mt-1 space-y-0.5 border-l border-[var(--color-border)] pl-2">
              {[...m.history].reverse().map((h, i) => (
                <li key={i} className="text-[10px] text-[var(--color-muted)]">
                  <span className="text-[var(--color-fg)]">{h.op}</span>
                  {" · "}
                  {h.reason}
                  {typeof h.before?.content === "string" && (
                    <div className="truncate line-through opacity-60">
                      {h.before.content}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="flex shrink-0 gap-1">
          {!m.archived && !editing && (
            <>
              <button
                type="button"
                onClick={() => setEditing(true)}
                className="rounded p-1 text-[var(--color-muted)] hover:bg-[var(--color-surface)]"
                title="编辑"
                aria-label="编辑"
              >
                <Wrench size={11} aria-hidden />
              </button>
              <button
                type="button"
                onClick={() => archive.mutate()}
                disabled={archive.isPending}
                className="rounded p-1 text-[var(--color-muted)] hover:bg-[var(--color-surface)]"
                title="归档（可恢复）"
                aria-label="归档"
              >
                <Archive size={11} aria-hidden />
              </button>
            </>
          )}
          {m.archived && (
            <button
              type="button"
              onClick={() => restore.mutate()}
              disabled={restore.isPending}
              className="rounded p-1 text-[var(--color-muted)] hover:bg-[var(--color-surface)]"
              title="恢复"
              aria-label="恢复"
            >
              <RotateCcw size={11} aria-hidden />
            </button>
          )}
        </div>
      </div>
    </li>
  );
}

export default function MemoryPanel() {
  const qc = useQueryClient();
  const [showArchived, setShowArchived] = useState(false);
  const [theme, setTheme] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [newContent, setNewContent] = useState("");
  const [newTheme, setNewTheme] = useState("");
  const [probe, setProbe] = useState("");
  const [probeResult, setProbeResult] = useState<string | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["memories", showArchived, theme],
    queryFn: () =>
      api.listMemories({
        includeArchived: showArchived,
        theme: theme ?? undefined,
      }),
  });

  const add = useMutation({
    mutationFn: () => api.createMemory(newContent, newTheme || "其他"),
    onSuccess: () => {
      setNewContent("");
      setNewTheme("");
      setAdding(false);
      void qc.invalidateQueries({ queryKey: ["memories"] });
    },
  });

  const search = useMutation({
    mutationFn: () => api.searchMemories(probe),
    onSuccess: (r) => {
      setProbeResult(
        r.items.length === 0
          ? "没有召回任何记忆"
          : r.items
              .map((x) => `${x.score.toFixed(3)}  [${x.theme}] ${x.content}`)
              .join("\n"),
      );
    },
  });

  return (
    <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <div className="mb-3 flex items-center gap-2">
        <h2 className="text-sm font-medium">长期记忆</h2>
        {data && (
          <span className="text-xs text-[var(--color-muted)]">
            {data.items.filter((m) => !m.archived).length} 条
          </span>
        )}
        <button
          type="button"
          onClick={() => setAdding((v) => !v)}
          className="ml-auto flex items-center gap-1 rounded-lg border border-[var(--color-border)] px-2 py-1 text-xs hover:bg-[var(--color-bg)]"
        >
          <Plus size={11} aria-hidden />
          手动添加
        </button>
      </div>

      <p className="mb-3 text-xs text-[var(--color-muted)]">
        跨会话生效。每轮对话开始时相关记忆会自动注入 —— 记错了就在这里改或归档，
        否则错误会在之后每一轮持续生效。
      </p>

      {adding && (
        <div className="mb-3 space-y-1.5 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] p-2">
          <textarea
            value={newContent}
            onChange={(e) => setNewContent(e.target.value)}
            placeholder="一句话说清一件事，比如「后端统一用 FastAPI，不用 Flask」"
            rows={2}
            className="w-full resize-none rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-1.5 py-1 text-xs outline-none focus:border-[var(--color-accent)]"
          />
          <div className="flex gap-1.5">
            <input
              value={newTheme}
              onChange={(e) => setNewTheme(e.target.value)}
              placeholder="主题（如 技术偏好）"
              list="memory-themes"
              className="min-w-0 flex-1 rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-1.5 py-1 text-xs outline-none focus:border-[var(--color-accent)]"
            />
            <datalist id="memory-themes">
              {data?.themes.map((t) => (
                <option key={t.theme} value={t.theme} />
              ))}
            </datalist>
            <button
              type="button"
              onClick={() => add.mutate()}
              disabled={add.isPending || !newContent.trim()}
              className="rounded bg-[var(--color-accent)] px-2.5 py-1 text-xs text-white disabled:opacity-50"
            >
              添加
            </button>
          </div>
        </div>
      )}

      {/* 召回探针。没有它的话召回质量只能靠猜 ——
          "为什么这条没被召回"是最常见的疑问 */}
      <div className="mb-3 flex gap-1.5">
        <div className="relative min-w-0 flex-1">
          <Search
            size={11}
            className="absolute left-2 top-1/2 -translate-y-1/2 text-[var(--color-muted)]"
            aria-hidden
          />
          <input
            value={probe}
            onChange={(e) => setProbe(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && probe.trim()) search.mutate();
            }}
            placeholder="试一句话，看会召回哪些记忆"
            className="w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] py-1 pl-7 pr-2 text-xs outline-none focus:border-[var(--color-accent)]"
          />
        </div>
        <button
          type="button"
          onClick={() => search.mutate()}
          disabled={search.isPending || !probe.trim()}
          className="rounded-lg border border-[var(--color-border)] px-2 py-1 text-xs hover:bg-[var(--color-bg)] disabled:opacity-50"
        >
          试召回
        </button>
      </div>

      {probeResult && (
        <pre className="mb-3 max-h-32 overflow-auto whitespace-pre-wrap rounded-lg bg-[var(--color-bg)] p-2 font-mono text-[10px] text-[var(--color-muted)]">
          {probeResult}
        </pre>
      )}

      {/* 主题过滤 */}
      {data && data.themes.length > 1 && (
        <div className="mb-2 flex flex-wrap gap-1">
          <button
            type="button"
            onClick={() => setTheme(null)}
            className={`rounded px-1.5 py-0.5 text-[10px] ${
              theme === null
                ? "bg-[var(--color-accent)] text-white"
                : "bg-[var(--color-bg)] text-[var(--color-muted)]"
            }`}
          >
            全部
          </button>
          {data.themes.map((t) => (
            <button
              key={t.theme}
              type="button"
              onClick={() => setTheme(t.theme)}
              className={`rounded px-1.5 py-0.5 text-[10px] ${
                theme === t.theme
                  ? "bg-[var(--color-accent)] text-white"
                  : "bg-[var(--color-bg)] text-[var(--color-muted)]"
              }`}
            >
              {t.theme} {t.count}
            </button>
          ))}
        </div>
      )}

      <label className="mb-2 flex cursor-pointer items-center gap-1.5 text-[11px] text-[var(--color-muted)]">
        <input
          type="checkbox"
          checked={showArchived}
          onChange={(e) => setShowArchived(e.target.checked)}
          className="accent-[var(--color-accent)]"
        />
        显示已归档（归档不是真删，可恢复）
      </label>

      {isLoading ? (
        <div className="flex items-center gap-1.5 text-xs text-[var(--color-muted)]">
          <Loader2 size={12} className="animate-spin" aria-hidden />
          加载中…
        </div>
      ) : !data || data.items.length === 0 ? (
        <div className="rounded-lg border border-dashed border-[var(--color-border)] px-3 py-6 text-center text-xs text-[var(--color-muted)]">
          还没有记忆。聊天中让它「记住」某件事，或者在上面手动添加。
        </div>
      ) : (
        <ul className="space-y-1.5">
          {data.items.map((m) => (
            <MemoryRow key={m.id} m={m} />
          ))}
        </ul>
      )}
    </section>
  );
}

/** 聊天流里的"已召回记忆"提示。用户要能看到 AI 用了哪些记忆 */
export function RecalledMemoriesBadge({
  items,
}: {
  items: { memory_id: string; theme: string; content: string }[];
}) {
  const [open, setOpen] = useState(false);
  if (items.length === 0) return null;
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)]/60">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-1.5 px-2.5 py-1 text-left text-[11px] text-[var(--color-muted)]"
      >
        <ChevronRight
          size={10}
          className={`shrink-0 transition-transform ${open ? "rotate-90" : ""}`}
          aria-hidden
        />
        <Brain size={11} className="shrink-0" aria-hidden />
        用到了 {items.length} 条记忆
      </button>
      {open && (
        <ul className="space-y-0.5 border-t border-[var(--color-border)] px-2.5 py-1">
          {items.map((m) => (
            <li key={m.memory_id} className="text-[10px] text-[var(--color-muted)]">
              [{m.theme}] {m.content}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
