import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Globe, Loader2 } from "lucide-react";
import { useState } from "react";
import clsx from "clsx";

import { api } from "@/lib/api";

/**
 * 联网搜索开关。
 *
 * ## 为什么需要这个界面
 *
 * 联网搜索默认关闭 —— 它会把用户的查询词发给第三方搜索引擎，
 * 这种有外部副作用的能力必须显式同意，不能因为装了个包就默认开。
 *
 * 但"必须改 .env 再重启"对使用者门槛太高：clone 下来想开个联网搜索，
 * 要去翻文档找环境变量名、改文件、重启进程。
 *
 * 所以这里做成运行时开关。改完立刻重建工具，不需要重启。
 *
 * ## 为什么要说"重启后失效"
 *
 * 这个开关改的是运行时的 settings 对象，重启回落 .env 的值。
 * 不说清楚的话用户重启后发现又关了，会以为是 bug。
 */

const OPTIONS = [
  {
    value: "none",
    label: "关闭",
    hint: "不注册 web_search 工具",
  },
  {
    value: "ddg",
    label: "DuckDuckGo",
    hint: "免费，不需要 API Key",
  },
  {
    value: "tavily",
    label: "Tavily",
    hint: "需要 API Key，结果为 LLM 优化过",
  },
] as const;

export default function WebSearchPanel() {
  const qc = useQueryClient();
  const [key, setKey] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["websearch"],
    queryFn: api.websearch,
  });

  const save = useMutation({
    mutationFn: (backend: string) =>
      api.setWebsearch(backend, backend === "tavily" ? key || undefined : undefined),
    onSuccess: (r) => {
      setErr(null);
      setNote(r.persist_hint);
      setKey("");
      void qc.invalidateQueries({ queryKey: ["websearch"] });
      // 工具列表变了，聊天页的 #工具 选择器要跟着刷
      void qc.invalidateQueries({ queryKey: ["tools"] });
    },
    onError: (e: Error) => {
      setErr(e.message);
      setNote(null);
    },
  });

  const current = data?.backend ?? "none";

  return (
    <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <h2 className="mb-1 flex items-center gap-2 text-sm font-medium">
        <Globe size={14} aria-hidden />
        联网搜索
        {data?.registered && (
          <span className="rounded bg-[var(--color-ok)]/15 px-1.5 py-0.5 text-[11px] text-[var(--color-ok)]">
            已启用
          </span>
        )}
      </h2>
      <p className="mb-3 text-xs text-[var(--color-muted)]">
        默认关闭 —— 开启后 agent 的搜索词会发给第三方搜索引擎。
      </p>

      {isLoading && (
        <p className="flex items-center gap-2 text-xs text-[var(--color-muted)]">
          <Loader2 size={12} className="animate-spin" aria-hidden />
          读取中…
        </p>
      )}

      {data && (
        <div className="space-y-2">
          {OPTIONS.map((o) => {
            // 依赖没装时不能选 —— 选了也起不来，
            // 而错误只会在模型调用工具时才出现
            const missing =
              (o.value === "ddg" && !data.ddg_available) ||
              (o.value === "tavily" && !data.tavily_available);
            const active = current === o.value;
            return (
              <label
                key={o.value}
                className={clsx(
                  "flex cursor-pointer items-start gap-2.5 rounded-lg border p-2.5 transition",
                  active
                    ? "border-[var(--color-accent)] bg-[var(--color-accent)]/5"
                    : "border-[var(--color-border)] hover:bg-[var(--color-surface-2)]/60",
                  missing && "cursor-not-allowed opacity-50",
                )}
              >
                <input
                  type="radio"
                  name="websearch-backend"
                  value={o.value}
                  checked={active}
                  disabled={missing || save.isPending}
                  onChange={() => save.mutate(o.value)}
                  className="mt-0.5"
                />
                <div className="min-w-0 flex-1">
                  <p className="text-sm">
                    {o.label}
                    {missing && (
                      <span className="ml-2 text-[11px] text-[var(--color-warn)]">
                        依赖未安装（uv sync --extra search）
                      </span>
                    )}
                  </p>
                  <p className="text-[11px] text-[var(--color-muted)]">{o.hint}</p>
                </div>
              </label>
            );
          })}

          {/* Tavily 的 key 输入。只在选它时出现 ——
              一直显示的话用户会以为不填就不能用联网搜索 */}
          {current !== "tavily" && data.tavily_available && (
            <div className="flex items-center gap-2 pt-1">
              <input
                type="password"
                value={key}
                onChange={(e) => setKey(e.target.value)}
                placeholder="Tavily API Key（选 Tavily 才需要）"
                aria-label="Tavily API Key"
                className="min-w-0 flex-1 rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5 text-xs"
              />
              <button
                type="button"
                disabled={!key.trim() || save.isPending}
                onClick={() => save.mutate("tavily")}
                className="shrink-0 rounded-md bg-[var(--color-accent)] px-2.5 py-1.5 text-xs text-white transition hover:opacity-90 disabled:opacity-40"
              >
                用 Tavily
              </button>
            </div>
          )}
          {current === "tavily" && data.has_tavily_key && (
            <p className="pt-1 text-[11px] text-[var(--color-muted)]">
              已配置 API Key
            </p>
          )}
        </div>
      )}

      {err && (
        <div
          role="alert"
          className="mt-3 rounded-md bg-[var(--color-err)]/10 px-2.5 py-1.5 text-[11px] text-[var(--color-err)]"
        >
          {err}
        </div>
      )}
      {note && !err && (
        <div className="mt-3 rounded-md bg-[var(--color-warn)]/10 px-2.5 py-1.5 text-[11px] text-[var(--color-warn)]">
          {note}
        </div>
      )}
    </section>
  );
}
