/**
 * 功能位绑定（子页面）。
 *
 * 每个功能位决定"干这件事用哪个模型"。选择即保存（PUT /bindings upsert）。
 * 只做全局默认绑定（agent_name=""），per-agent 绑定暂不做。
 *
 * 看图位特殊：模型要核验过看图能力（supports_vision）才能开视觉模式，
 * 所以选中未核验模型时给警示 + 核验按钮。
 */

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Loader2, X } from "lucide-react";

import { api } from "@/lib/api";
import { filterModelsForPurpose, PURPOSE_META, PURPOSE_ORDER } from "@/lib/purposeMeta";
import type { ModelItem, Purpose } from "@/lib/types";

/**
 * 下拉框里每个模型的显示文本。
 *
 * 看图位加前缀标识视觉验证状态：✓ 已验证可看图，？未核验。
 * 原生 option 不支持着色，用符号比用颜色可靠（跨平台一致）。
 */
function modelOptionLabel(m: ModelItem, purpose: Purpose): string {
  const name = m.display_name || m.model_id;
  const disabled = m.enabled ? "" : "（已禁用）";
  if (purpose !== "vision") return `${name}${disabled}`;
  const mark = m.supports_vision === "true" ? "✓ " : "？";
  return `${mark}${name}${disabled}`;
}

export function BindingsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const [saving, setSaving] = useState<Purpose | null>(null);
  const [visionDetail, setVisionDetail] = useState("");

  const bindings = useQuery({ queryKey: ["bindings"], queryFn: api.listBindings });
  const models = useQuery({ queryKey: ["models", "all"], queryFn: () => api.models() });

  const setBinding = useMutation({
    mutationFn: (args: { purpose: Purpose; modelPk: string }) =>
      api.setBinding(args.purpose, args.modelPk),
    onMutate: (v) => setSaving(v.purpose),
    onSettled: () => setSaving(null),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["bindings"] }),
    onError: (e: Error) => alert(e.message),
  });

  const verify = useMutation({
    mutationFn: (pk: string) => api.verifyVision(pk),
    onSuccess: (r) => {
      setVisionDetail(r.detail);
      qc.invalidateQueries({ queryKey: ["models"] });
    },
    onError: (e: Error) => alert(e.message),
  });

  const allModels = models.data?.items ?? [];

  // 全局绑定（agent_name=""）。per-agent 绑定暂不展示。
  const bindingMap = useMemo(() => {
    const m = new Map<string, string>();
    for (const b of bindings.data?.items ?? []) {
      if (b.agent_name === "") m.set(b.purpose, b.model_pk);
    }
    return m;
  }, [bindings.data]);

  // 按分组（端点）组织下拉候选
  const groups = useMemo(() => {
    const g = new Map<string, { name: string; models: ModelItem[] }>();
    for (const m of allModels) {
      const key = m.endpoint_id;
      if (!g.has(key)) g.set(key, { name: m.endpoint_name || "未命名", models: [] });
      g.get(key)!.models.push(m);
    }
    return [...g.values()];
  }, [allModels]);

  const modelById = useMemo(
    () => new Map(allModels.map((m) => [m.id, m])),
    [allModels],
  );

  const visionBound = bindingMap.get("vision") ?? "";
  const visionModel = modelById.get(visionBound);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="功能位绑定"
    >
      <div
        className="flex max-h-[90vh] w-full max-w-lg flex-col overflow-hidden rounded-xl border bg-[var(--color-surface)] shadow-2xl"
        style={{ borderColor: "var(--color-border)" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between border-b px-5 py-4" style={{ borderColor: "var(--color-border)" }}>
          <div>
            <h2 className="text-base font-medium">功能位绑定</h2>
            <p className="mt-0.5 text-xs" style={{ color: "var(--color-muted)" }}>
              每个功能位决定「干这件事用哪个模型」。标题/压缩/记忆建议配便宜模型。
            </p>
          </div>
          <button type="button" onClick={onClose} aria-label="关闭" className="rounded p-1 hover:bg-[var(--color-surface-2)]">
            <X size={16} style={{ color: "var(--color-muted)" }} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-2">
          {PURPOSE_ORDER.map((purpose) => {
            const meta = PURPOSE_META[purpose];
            const Icon = meta.icon;
            const bound = bindingMap.get(purpose) ?? "";
            const busy = saving === purpose;

            // 按功能位过滤候选分组（跳过空组）
            const candidates = groups
              .map((g) => ({
                name: g.name,
                models: filterModelsForPurpose(g.models, purpose),
              }))
              .filter((g) => g.models.length > 0);

            // 当前绑定的模型若被过滤掉，单独列出，避免 select 值失效变空白
            const boundModel = modelById.get(bound);
            const boundHidden =
              boundModel != null &&
              !candidates.some((g) => g.models.some((m) => m.id === bound));

            return (
              <div key={purpose} className="border-b py-3 last:border-b-0" style={{ borderColor: "var(--color-border)" }}>
                <div className="flex items-center gap-3">
                  <Icon size={16} className="shrink-0" style={{ color: "var(--color-accent)" }} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 text-sm">
                      {meta.label}
                      {busy && <Loader2 size={12} className="animate-spin" style={{ color: "var(--color-muted)" }} />}
                    </div>
                    <div className="text-xs" style={{ color: "var(--color-muted)" }}>
                      {meta.hint}
                    </div>
                  </div>

                  <select
                    value={bound}
                    onChange={(e) => setBinding.mutate({ purpose, modelPk: e.target.value })}
                    disabled={allModels.length === 0}
                    className="w-52 shrink-0 rounded-lg border px-2 py-1.5 text-sm outline-none disabled:opacity-50"
                    style={{ borderColor: "var(--color-border)", background: "var(--color-bg)" }}
                    aria-label={meta.label}
                  >
                    {bound === "" && (
                      <option value="" disabled>
                        {meta.noChatFallback ? "未绑定（不可用）" : "未绑定（回落默认）"}
                      </option>
                    )}
                    {boundHidden && boundModel && (
                      <optgroup label="当前绑定">
                        <option value={bound}>
                          {boundModel.display_name || boundModel.model_id}（类型不匹配）
                        </option>
                      </optgroup>
                    )}
                    {candidates.map((g) => (
                      <optgroup key={g.name} label={g.name}>
                        {g.models.map((m) => (
                          <option key={m.id} value={m.id}>
                            {modelOptionLabel(m, purpose)}
                          </option>
                        ))}
                      </optgroup>
                    ))}
                  </select>
                </div>

                {/* 看图位：下拉框符号图例。原生 option 不能着色，
                    用符号区分验证状态，图例在这里解释符号含义。 */}
                {purpose === "vision" && (
                  <div className="mt-1 text-right text-[10px]" style={{ color: "var(--color-muted)" }}>
                    ✓ 已核验可看图　？未核验
                  </div>
                )}

                {/* 看图位：未核验模型的警示 + 核验按钮 */}
                {purpose === "vision" && visionModel && visionModel.supports_vision === "unknown" && (
                  <div className="mt-2 flex items-center gap-2 rounded-lg border px-2.5 py-1.5 text-xs" style={{ borderColor: "var(--color-border)", background: "var(--color-bg)" }}>
                    <AlertTriangle size={12} className="shrink-0" style={{ color: "var(--color-warn)" }} />
                    <span className="min-w-0 flex-1" style={{ color: "var(--color-muted)" }}>
                      {visionModel.model_id} 未核验看图能力，开视觉模式会被拦。
                      {visionDetail && <span> {visionDetail}</span>}
                    </span>
                    <button
                      type="button"
                      onClick={() => {
                        setVisionDetail("");
                        verify.mutate(visionModel.id);
                      }}
                      disabled={verify.isPending}
                      className="shrink-0 rounded border px-2 py-0.5 text-xs hover:bg-[var(--color-surface-2)] disabled:opacity-50"
                      style={{ borderColor: "var(--color-border)", color: "var(--color-accent)" }}
                    >
                      {verify.isPending ? "核验中…" : "核验"}
                    </button>
                  </div>
                )}
              </div>
            );
          })}
        </div>

        <div className="border-t px-5 py-3" style={{ borderColor: "var(--color-border)" }}>
          <p className="text-xs" style={{ color: "var(--color-muted)" }}>
            选择即保存。全部功能位也可在下方「看图能力核验」列表里批量核验模型。
          </p>
        </div>
      </div>
    </div>
  );
}
