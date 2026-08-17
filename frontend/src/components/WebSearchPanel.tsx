import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, EyeOff, Globe, Loader2 } from "lucide-react";
import clsx from "clsx";

import { api } from "@/lib/api";

/**
 * 联网搜索开关 + Tavily Key 管理。
 *
 * ## 密钥脱敏
 *
 * Key 输入框默认 type=password（脱敏显示圆点），点眼睛按钮才显示明文。
 * 已保存的 Key 后端只回 has_tavily_key（不返回明文/尾号），所以这里
 * 只显示「已配置」，改 Key 就是重新输入覆盖。
 *
 * ## 自动保存（debounce）
 *
 * 选后端是明确的点击，立即保存；填 Key 是连续输入，节流 1.5 秒保存 ——
 * 每敲一个字都打一次后端是浪费，还会把不完整的 Key 存进去。
 */

const OPTIONS = [
  { value: "none", label: "关闭", hint: "不注册 web_search 工具" },
  { value: "ddg", label: "DuckDuckGo", hint: "免费，不需要 API Key" },
  { value: "tavily", label: "Tavily", hint: "需要 API Key，结果为 LLM 优化过" },
] as const;

type SaveState = "idle" | "saving" | "saved" | "error";

export default function WebSearchPanel() {
  const qc = useQueryClient();
  const [key, setKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [err, setErr] = useState<string | null>(null);
  const keyTimer = useRef<number | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["websearch"],
    queryFn: api.websearch,
  });

  const save = useMutation({
    mutationFn: (args: { backend: string; key?: string }) =>
      api.setWebsearch(args.backend, args.key || undefined),
    onMutate: () => setSaveState("saving"),
    onSuccess: () => {
      setErr(null);
      setSaveState("saved");
      setKey("");
      void qc.invalidateQueries({ queryKey: ["websearch"] });
      void qc.invalidateQueries({ queryKey: ["tools"] });
      window.setTimeout(() => setSaveState((s) => (s === "saved" ? "idle" : s)), 2000);
    },
    onError: (e: Error) => {
      setErr(e.message);
      setSaveState("error");
    },
  });

  const current = data?.backend ?? "none";

  // Key 输入：节流 1.5 秒后自动保存。空串不保存（等用户继续输入）。
  const onKeyChange = (v: string) => {
    setKey(v);
    if (keyTimer.current) window.clearTimeout(keyTimer.current);
    if (!v.trim()) return;
    keyTimer.current = window.setTimeout(() => {
      save.mutate({ backend: "tavily", key: v });
    }, 1500);
  };

  // 卸载时清掉计时器，避免组件卸载后还发请求
  useEffect(
    () => () => {
      if (keyTimer.current) window.clearTimeout(keyTimer.current);
    },
    [],
  );

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
        默认关闭 —— 开启后 agent 的搜索词会发给第三方搜索引擎。配置会持久化，重启后生效。
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
                  onChange={() => save.mutate({ backend: o.value })}
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

          {/* Tavily Key：脱敏输入 + 点眼睛显示 + 自动保存 */}
          {data.tavily_available && (
            <div className="flex items-center gap-2 pt-1">
              <div className="relative min-w-0 flex-1">
                <input
                  type={showKey ? "text" : "password"}
                  value={key}
                  onChange={(e) => onKeyChange(e.target.value)}
                  placeholder={
                    current === "tavily" && data.has_tavily_key
                      ? `已配置 ${data.key_hint}（重新输入以更换）`
                      : "Tavily API Key"
                  }
                  aria-label="Tavily API Key"
                  className="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5 pr-8 text-xs outline-none focus:border-[var(--color-accent)]"
                />
                <button
                  type="button"
                  onClick={() => setShowKey((v) => !v)}
                  aria-label={showKey ? "隐藏 Key" : "显示 Key"}
                  title={showKey ? "隐藏" : "显示"}
                  className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-0.5 text-[var(--color-muted)] hover:text-[var(--color-text)]"
                >
                  {showKey ? <EyeOff size={13} /> : <Eye size={13} />}
                </button>
              </div>
            </div>
          )}

          {/* 保存状态 */}
          <div className="flex items-center gap-2 pt-0.5 text-[11px]">
            {saveState === "saving" && (
              <span className="flex items-center gap-1 text-[var(--color-muted)]">
                <Loader2 size={11} className="animate-spin" />
                保存中…
              </span>
            )}
            {saveState === "saved" && <span className="text-[var(--color-ok)]">✓ 已保存</span>}
            {saveState === "error" && <span className="text-[var(--color-err)]">✗ 保存失败</span>}
            {current === "tavily" && data.has_tavily_key && saveState === "idle" && (
              <span className="text-[var(--color-muted)]">
                已配置 API Key（{data.key_hint}）
              </span>
            )}
          </div>
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
    </section>
  );
}
