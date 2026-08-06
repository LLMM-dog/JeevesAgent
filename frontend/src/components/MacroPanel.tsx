import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Pencil, Plus, RotateCw, Trash2, X } from "lucide-react";
import { useState } from "react";

import { api } from "@/lib/api";

/**
 * 宏管理。
 *
 * ## 为什么之前没有这个面板
 *
 * 宏一直只能读：有列表接口、有提词器、有 `!宏` 引用展开，但没有任何
 * 创建入口。MacroPicker 的空态写着「对我说'把这个流程存成宏'，我会帮你建」
 * —— 而 macros/ 不在文件工具的白名单里，模型实际写不进去。
 *
 * 现在两条路都通：这个面板，以及模型的 manage_asset 工具。
 * 两者走同一个后端 authoring 模块，格式和校验完全一致。
 */

type Draft = {
  name: string;
  description: string;
  body: string;
  keywords: string;
  /** 编辑已有的宏时为原名；新建时为空 */
  original: string;
};

const EMPTY: Draft = {
  name: "",
  description: "",
  body: "",
  keywords: "",
  original: "",
};

export default function MacroPanel() {
  const qc = useQueryClient();
  const [draft, setDraft] = useState<Draft | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["macros"],
    queryFn: api.listMacros,
  });

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["macros"] });
    // 提词器读的是同一个 key，所以它也会跟着更新
  };

  const save = useMutation({
    mutationFn: (d: Draft) =>
      api.upsertMacro({
        name: d.name,
        description: d.description,
        body: d.body,
        keywords: d.keywords
          .split(/[,，\s]+/)
          .map((s) => s.trim())
          .filter(Boolean),
        // 编辑已有的才允许覆盖 —— 新建时撞名要报错，
        // 否则会悄悄冲掉一个同名的宏
        overwrite: !!d.original,
      }),
    onSuccess: (r) => {
      setDraft(null);
      setErr(null);
      setNote(r.created ? `已新建「${r.name}」` : `已更新「${r.name}」`);
      refresh();
    },
    onError: (e: Error) => setErr(e.message),
  });

  const del = useMutation({
    mutationFn: (name: string) => api.deleteMacro(name),
    onSuccess: () => {
      setNote("已删除");
      refresh();
    },
    onError: (e: Error) => setErr(e.message),
  });

  const reload = useMutation({
    mutationFn: () => api.reloadMacros(),
    onSuccess: (r) => {
      setNote(`重扫完成，${r.count} 个宏`);
      refresh();
    },
  });

  const openEdit = async (name: string) => {
    setErr(null);
    try {
      // 【必须取 source 而不是列表里的数据】。列表只有 name/description，
      // 没有正文 —— 直接编辑会把正文清空。
      const s = await api.getMacroSource(name);
      setDraft({
        name: s.name,
        description: s.description,
        body: s.body,
        keywords: (s.keywords ?? []).join(", "),
        original: s.name,
      });
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const items = data?.items ?? [];

  return (
    <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <div className="mb-3 flex items-center gap-2">
        <h2 className="text-sm font-medium">宏</h2>
        <span className="text-xs text-[var(--color-muted)]">
          {items.length} 个
        </span>
        <button
          type="button"
          onClick={() => {
            setDraft({ ...EMPTY });
            setErr(null);
          }}
          className="ml-auto flex items-center gap-1 rounded-lg border border-[var(--color-border)] px-2 py-1 text-xs hover:bg-[var(--color-bg)]"
        >
          <Plus size={12} aria-hidden />
          新建
        </button>
        <button
          type="button"
          onClick={() => reload.mutate()}
          disabled={reload.isPending}
          className="flex items-center gap-1 rounded-lg border border-[var(--color-border)] px-2 py-1 text-xs hover:bg-[var(--color-bg)]"
        >
          {reload.isPending ? (
            <Loader2 size={12} className="animate-spin" aria-hidden />
          ) : (
            <RotateCw size={12} aria-hidden />
          )}
          重扫
        </button>
      </div>

      <p className="mb-3 text-xs text-[var(--color-muted)]">
        宏是可复用的流程模板。在输入框打 <code>!</code> 可以引用它。
        也可以直接对我说「把这个流程存成宏」，我会帮你建。
      </p>

      {err && (
        <div className="mb-3 rounded-lg bg-[var(--color-err)]/10 px-3 py-2 text-xs text-[var(--color-err)]">
          {err}
        </div>
      )}
      {note && (
        <div className="mb-3 rounded-lg bg-[var(--color-ok)]/10 px-3 py-2 text-xs text-[var(--color-ok)]">
          {note}
        </div>
      )}

      {draft && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate(draft);
          }}
          className="mb-3 space-y-2 rounded-lg border border-[var(--color-accent)]/40 bg-[var(--color-bg)] p-3"
        >
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium">
              {draft.original ? `编辑「${draft.original}」` : "新建宏"}
            </span>
            <button
              type="button"
              onClick={() => setDraft(null)}
              aria-label="取消"
              className="ml-auto text-[var(--color-muted)] hover:text-[var(--color-fg)]"
            >
              <X size={14} aria-hidden />
            </button>
          </div>

          <label className="block text-xs">
            <span className="text-[var(--color-muted)]">名字</span>
            <input
              value={draft.name}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
              // 编辑时不让改名 —— 改名等于"删旧建新"，而那会让
              // 已经引用这个宏的地方失效。想改名就删了重建。
              disabled={!!draft.original}
              required
              placeholder="部署流程"
              className="mt-1 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1 disabled:opacity-60"
            />
          </label>

          <label className="block text-xs">
            <span className="text-[var(--color-muted)]">
              什么时候用它（会常驻上下文，写触发条件）
            </span>
            <input
              value={draft.description}
              onChange={(e) =>
                setDraft({ ...draft, description: e.target.value })
              }
              required
              placeholder="当用户说「发版」「部署到生产」时使用。"
              className="mt-1 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1"
            />
          </label>

          <label className="block text-xs">
            <span className="text-[var(--color-muted)]">关键词（可选，逗号分隔）</span>
            <input
              value={draft.keywords}
              onChange={(e) => setDraft({ ...draft, keywords: e.target.value })}
              placeholder="部署, 发版, deploy"
              className="mt-1 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1"
            />
          </label>

          <label className="block text-xs">
            <span className="text-[var(--color-muted)]">
              正文（Markdown）。用 <code>${"{MACRO_DIR}"}</code> 引用宏所在目录
            </span>
            <textarea
              value={draft.body}
              onChange={(e) => setDraft({ ...draft, body: e.target.value })}
              required
              rows={10}
              placeholder={"# 部署流程\n\n1. 跑测试\n2. 打 tag\n3. 推镜像"}
              className="mt-1 w-full resize-y rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1 font-mono"
            />
          </label>

          <div className="flex items-center gap-2">
            <button
              type="submit"
              disabled={save.isPending}
              className="rounded-lg bg-[var(--color-accent)] px-3 py-1 text-xs text-white disabled:opacity-60"
            >
              {save.isPending ? "保存中…" : "保存"}
            </button>
            <button
              type="button"
              onClick={() => setDraft(null)}
              className="rounded-lg border border-[var(--color-border)] px-3 py-1 text-xs"
            >
              取消
            </button>
          </div>
        </form>
      )}

      {isLoading ? (
        <div className="flex items-center gap-1.5 text-xs text-[var(--color-muted)]">
          <Loader2 size={12} className="animate-spin" aria-hidden />
          加载中…
        </div>
      ) : items.length === 0 ? (
        <div className="rounded-lg border border-dashed border-[var(--color-border)] px-3 py-6 text-center text-xs text-[var(--color-muted)]">
          还没有宏。点「新建」，或者对我说「把这个流程存成宏」。
        </div>
      ) : (
        <ul className="space-y-1">
          {items.map((m) => (
            <li
              key={m.name}
              className="flex items-start gap-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-2"
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <span className="font-mono text-xs">{m.name}</span>
                  {m.keywords?.length > 0 && (
                    <span className="text-[10px] text-[var(--color-muted)]">
                      {m.keywords.join(" · ")}
                    </span>
                  )}
                </div>
                <p className="mt-0.5 text-[11px] text-[var(--color-muted)]">
                  {m.description}
                </p>
              </div>
              <button
                type="button"
                onClick={() => void openEdit(m.name)}
                aria-label={`编辑 ${m.name}`}
                className="shrink-0 rounded p-1 text-[var(--color-muted)] hover:bg-[var(--color-surface)] hover:text-[var(--color-fg)]"
              >
                <Pencil size={12} aria-hidden />
              </button>
              <button
                type="button"
                onClick={() => {
                  // confirm 是必须的 —— 删除是不可逆的，
                  // 而这个按钮紧挨着编辑按钮
                  if (confirm(`删除宏「${m.name}」？这不可恢复。`)) {
                    del.mutate(m.name);
                  }
                }}
                aria-label={`删除 ${m.name}`}
                className="shrink-0 rounded p-1 text-[var(--color-muted)] hover:bg-[var(--color-err)]/10 hover:text-[var(--color-err)]"
              >
                <Trash2 size={12} aria-hidden />
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
