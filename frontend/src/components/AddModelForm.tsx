import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { AlertTriangle, Check, RefreshCw, Search } from "lucide-react";
import clsx from "clsx";

import { api } from "../lib/api";

/**
 * 往已有端点下加模型。
 *
 * ## 为什么要自动拉列表
 *
 * 原来只有一个输入框，要用户自己敲模型 ID。而模型 ID 是
 * `deepseek-v4-pro`、`gpt-4o-mini-2024-07-18` 这类东西 —— 记不住、
 * 打错一个字符就是 404，而错误要等到真正对话时才出现。
 *
 * 端点和 Key 都已经存过了，直接拉一次 /v1/models 就有准确的列表。
 *
 * ## 为什么保留手填
 *
 * 有些端点不实现 /v1/models，或者返回的列表不全。拉不到时必须还能
 * 手填，否则这个端点就没法加模型了。
 */
export function AddModelForm({
  providerId,
  onDone,
  onCancel,
}: {
  providerId: string;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<string | null>(null);
  const [manualWindow, setManualWindow] = useState("");
  const [err, setErr] = useState("");

  const probe = useQuery({
    queryKey: ["availableModels", providerId],
    queryFn: () => api.availableModels(providerId),
    // 只在展开表单时拉一次。每次输入都拉的话会打爆上游
    staleTime: 60_000,
    retry: false,
  });

  const add = useMutation({
    mutationFn: (args: { modelId: string; window?: number }) =>
      api.addModel({
        provider_id: providerId,
        model_id: args.modelId,
        ...(args.window ? { context_window: args.window } : {}),
      }),
    onSuccess: () => {
      setQuery("");
      setPicked(null);
      setErr("");
      onDone();
    },
    onError: (e: Error) => setErr(e.message),
  });

  const items = probe.data?.items ?? [];

  // 模糊匹配：拆成字符序列按顺序匹配，不要求连续。
  // 打 "v4p" 能命中 "deepseek-v4-pro" —— 精确 includes 做不到这个。
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((m) => {
      const s = m.model_id.toLowerCase();
      let i = 0;
      for (const ch of q) {
        i = s.indexOf(ch, i);
        if (i === -1) return false;
        i += 1;
      }
      return true;
    });
  }, [items, query]);

  // 拉不到列表，或者输入的东西列表里没有 —— 都要能直接提交
  const canSubmitRaw =
    query.trim().length > 0 &&
    !filtered.some((m) => m.model_id === query.trim());

  return (
    <div
      className="space-y-2 border-b px-3 py-2"
      style={{ borderColor: "var(--color-border)" }}
    >
      <div className="flex items-center gap-2">
        <div className="relative min-w-0 flex-1">
          <Search
            size={12}
            className="absolute left-2 top-1/2 -translate-y-1/2"
            style={{ color: "var(--color-muted)" }}
          />
          <input
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setPicked(null);
              setErr("");
            }}
            placeholder={
              probe.isLoading
                ? "正在拉取可用模型…"
                : items.length > 0
                  ? "搜索或直接输入模型 ID"
                  : "输入模型 ID"
            }
            aria-label="模型 ID"
            autoFocus
            className="w-full rounded border py-1 pl-7 pr-2 text-xs"
            style={{
              borderColor: "var(--color-border)",
              background: "var(--color-bg)",
            }}
            onKeyDown={(e) => {
              if (e.key === "Escape") onCancel();
              if (e.key === "Enter") {
                const target = picked ?? (filtered.length === 1 ? filtered[0].model_id : query.trim());
                if (target) add.mutate({ modelId: target });
              }
            }}
          />
        </div>
        <input
          value={manualWindow}
          onChange={(e) => setManualWindow(e.target.value)}
          placeholder="窗口"
          aria-label="上下文窗口（留空则自动）"
          title="上下文窗口。留空的话用探测到的值，或按模型名推断"
          className="w-20 rounded border px-2 py-1 text-xs"
          style={{
            borderColor: "var(--color-border)",
            background: "var(--color-bg)",
          }}
        />
        <button
          onClick={() => probe.refetch()}
          disabled={probe.isFetching}
          aria-label="重新拉取模型列表"
          title="重新拉取"
          className="shrink-0 rounded p-1"
          style={{ color: "var(--color-muted)" }}
        >
          <RefreshCw size={12} className={clsx(probe.isFetching && "animate-spin")} />
        </button>
        <button
          onClick={onCancel}
          className="shrink-0 rounded px-2 py-1 text-xs"
          style={{ color: "var(--color-muted)" }}
        >
          取消
        </button>
      </div>

      {/* 拉取失败：说清还能手填，否则用户以为这条路断了 */}
      {probe.isError && (
        <p
          className="flex items-start gap-1 text-xs"
          style={{ color: "var(--color-warn)" }}
        >
          <AlertTriangle size={11} className="mt-0.5 shrink-0" />
          <span>
            拉不到模型列表（{(probe.error as Error).message}）。
            直接在上面输入模型 ID 也可以。
          </span>
        </p>
      )}

      {/* 候选列表 */}
      {items.length > 0 && (
        <ul
          className="max-h-48 overflow-y-auto rounded border"
          style={{ borderColor: "var(--color-border)" }}
          role="listbox"
          aria-label="可用模型"
        >
          {filtered.length === 0 ? (
            <li className="px-2 py-2 text-xs" style={{ color: "var(--color-muted)" }}>
              列表里没有匹配的。直接按 Enter 用你输入的名字。
            </li>
          ) : (
            filtered.map((m) => (
              <li key={m.model_id}>
                <button
                  role="option"
                  aria-selected={picked === m.model_id}
                  disabled={m.already_added}
                  onClick={() => {
                    setPicked(m.model_id);
                    setQuery(m.model_id);
                  }}
                  onDoubleClick={() =>
                    add.mutate({
                      modelId: m.model_id,
                      window: Number(manualWindow) || m.context_window,
                    })
                  }
                  className="flex w-full items-center gap-2 px-2 py-1.5 text-left text-xs disabled:opacity-40"
                  style={{
                    background:
                      picked === m.model_id ? "var(--color-surface-2)" : "transparent",
                  }}
                >
                  <span className="w-3 shrink-0">
                    {picked === m.model_id && (
                      <Check size={11} style={{ color: "var(--color-accent)" }} />
                    )}
                  </span>
                  <span className="min-w-0 flex-1 truncate font-mono">
                    {m.model_id}
                  </span>
                  <span className="shrink-0" style={{ color: "var(--color-muted)" }}>
                    {Math.round(m.context_window / 1000)}K
                  </span>
                  {m.already_added && (
                    <span className="shrink-0" style={{ color: "var(--color-muted)" }}>
                      已添加
                    </span>
                  )}
                  {m.looks_non_chat && !m.already_added && (
                    <span
                      className="shrink-0"
                      style={{ color: "var(--color-muted)" }}
                      title="看起来不是对话模型（embedding / rerank 之类）"
                    >
                      非对话
                    </span>
                  )}
                </button>
              </li>
            ))
          )}
        </ul>
      )}

      {err && (
        <p role="alert" className="text-xs" style={{ color: "var(--color-err)" }}>
          {err}
        </p>
      )}

      <div className="flex items-center gap-2">
        <button
          onClick={() => {
            const target = picked ?? query.trim();
            if (target) {
              const hit = items.find((m) => m.model_id === target);
              add.mutate({
                modelId: target,
                window: Number(manualWindow) || hit?.context_window,
              });
            }
          }}
          disabled={!(picked ?? query.trim()) || add.isPending}
          className="rounded px-3 py-1 text-xs disabled:opacity-50"
          style={{ background: "var(--color-accent)", color: "#fff" }}
        >
          添加
        </button>
        <span className="text-xs" style={{ color: "var(--color-muted)" }}>
          {picked
            ? `将添加 ${picked}`
            : canSubmitRaw
              ? `将按你输入的名字添加`
              : "点一个模型，或直接输入名字"}
        </span>
      </div>
    </div>
  );
}
