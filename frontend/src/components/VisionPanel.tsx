import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleHelp, Eye, EyeOff, Loader2 } from "lucide-react";
import { useState } from "react";

import { api } from "@/lib/api";

/**
 * 视觉能力核验面板。
 *
 * ## 为什么必须有这个界面
 *
 * `supports_vision` 是三态（true / false / unknown），默认 unknown。
 * 核验要发一次真实的多模态请求 —— 花钱、要几秒 —— 所以不能对每个模型
 * 自动跑，必须由用户显式触发。
 *
 * 而未核验的模型开视觉模式会被后端拦下（返回 400），提示里让用户
 * "去设置页核验" —— 就是这里。
 */

const STATE_META = {
  true: { label: "支持", cls: "text-[var(--color-ok)]", icon: Eye },
  false: { label: "不支持", cls: "text-[var(--color-err)]", icon: EyeOff },
  unknown: { label: "未核验", cls: "text-[var(--color-muted)]", icon: CircleHelp },
} as const;

export default function VisionPanel() {
  const qc = useQueryClient();
  const [results, setResults] = useState<Record<string, string>>({});

  const { data: providers } = useQuery({
    queryKey: ["endpoints"],
    queryFn: api.listEndpoints,
  });
  const { data: models, isLoading } = useQuery({
    queryKey: ["models", "all"],
    queryFn: () => api.models(),
  });

  const verify = useMutation({
    mutationFn: (pk: string) => api.verifyVision(pk),
    onSuccess: (r) => {
      setResults((prev) => ({ ...prev, [r.model_pk]: r.detail }));
      void qc.invalidateQueries({ queryKey: ["models"] });
    },
    onError: (e: Error, pk) => {
      setResults((prev) => ({ ...prev, [pk]: e.message }));
    },
  });

  const providerName = new Map(
    (providers?.items ?? []).map((p) => [p.id, p.name]),
  );

  return (
    <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <h2 className="mb-1 text-sm font-medium">图片输入能力</h2>
      <p className="mb-3 text-xs text-[var(--color-muted)]">
        模型列表接口不返回"支不支持图片"，模型名也看不出来 ——
        只能发一次真实的多模态请求来确认。核验用 1x1 的图，成本极低。
        通过后才能在对话框里开启视觉模式。
      </p>

      {isLoading ? (
        <div className="flex items-center gap-1.5 text-xs text-[var(--color-muted)]">
          <Loader2 size={12} className="animate-spin" aria-hidden />
          加载中…
        </div>
      ) : !models || models.items.length === 0 ? (
        <div className="rounded-lg border border-dashed border-[var(--color-border)] px-3 py-5 text-center text-xs text-[var(--color-muted)]">
          还没有模型。先在上面添加端点。
        </div>
      ) : (
        <ul className="space-y-1">
          {models.items.map((m) => {
            const meta = STATE_META[m.supports_vision] ?? STATE_META.unknown;
            const Icon = meta.icon;
            const busy = verify.isPending && verify.variables === m.id;
            const detail = results[m.id];
            return (
              <li
                key={m.id}
                className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5"
              >
                <div className="flex items-center gap-2">
                  <Icon size={12} className={`shrink-0 ${meta.cls}`} aria-hidden />
                  <span className="min-w-0 flex-1 truncate text-xs">
                    <span className="text-[var(--color-muted)]">
                      {providerName.get(m.endpoint_id) ?? "?"} /{" "}
                    </span>
                    {m.model_id}
                  </span>
                  <span className={`shrink-0 text-[10px] ${meta.cls}`}>
                    {meta.label}
                  </span>
                  <button
                    type="button"
                    onClick={() => verify.mutate(m.id)}
                    disabled={verify.isPending}
                    className="shrink-0 rounded border border-[var(--color-border)] px-1.5 py-0.5 text-[10px] hover:bg-[var(--color-surface)] disabled:opacity-50"
                  >
                    {busy ? "核验中…" : m.supports_vision === "unknown" ? "核验" : "重新核验"}
                  </button>
                </div>
                {/* 上游原话。失败原因有好几种（真不支持 / 中转站不转发 /
                    key 无权限 / 模型名错），修复动作完全不同 */}
                {detail && (
                  <p className="mt-1 break-words text-[10px] text-[var(--color-muted)]">
                    {detail}
                  </p>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
