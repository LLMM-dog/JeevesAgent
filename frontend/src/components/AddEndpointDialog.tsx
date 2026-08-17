/**
 * 添加模型 / 添加分组的对话框。
 *
 * 用户只填地址 + API Key，点"探测"拉模型列表勾选，保存时后端按
 * 地址自动推断分组名（suggested_name），同名或同地址同 Key 会并入已有分组。
 * "添加分组"和"添加模型"走同一个对话框：差别只在标题和引导文案，
 * 分组模式下可以不勾任何模型，只建一个空分组。
 */

import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Check,
  Loader2,
  Search,
  Server,
  X,
} from "lucide-react";
import clsx from "clsx";

import { api } from "@/lib/api";
import type { ProbeResponse } from "@/lib/types";

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm" style={{ color: "var(--color-text)" }}>
        {label}
      </span>
      {children}
      {hint && (
        <span className="mt-1 block text-xs" style={{ color: "var(--color-muted)" }}>
          {hint}
        </span>
      )}
    </label>
  );
}

const inputCls =
  "w-full rounded-lg border px-3 py-2 text-sm outline-none focus:border-[var(--color-accent)]";
const inputStyle = {
  borderColor: "var(--color-border)",
  background: "var(--color-bg)",
} as const;

export function AddEndpointDialog({
  open,
  mode,
  onClose,
}: {
  open: boolean;
  mode: "model" | "group";
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [probeResult, setProbeResult] = useState<ProbeResponse | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [visionPrefs, setVisionPrefs] = useState<
    Record<string, "true" | "false" | "unknown">
  >({});
  const [err, setErr] = useState("");

  const probe = useMutation({
    mutationFn: () => api.probe(baseUrl.trim(), apiKey.trim()),
    onSuccess: (r) => {
      setProbeResult(r);
      // 对话模型默认勾选，嵌入/语音/生图等默认不勾 —— 用户加的是"对话用"的模型。
      // 想加嵌入模型自己点一下就行。
      setSelected(
        new Set(
          r.models.filter((m) => !m.looks_non_chat).map((m) => m.model_id),
        ),
      );
      setErr("");
    },
    onError: (e: Error) => setErr(e.message),
  });

  const save = useMutation({
    mutationFn: () =>
      api.createEndpoint({
        name: probeResult?.suggested_name ?? "",
        base_url: baseUrl.trim(),
        api_key: apiKey.trim(),
        models: probeResult?.models
          .filter((m) => selected.has(m.model_id))
          .map((m) => ({
            model_id: m.model_id,
            context_window: m.context_window,
            model_type: m.model_type,
            supports_vision: visionPrefs[m.model_id] ?? "unknown",
          })) ?? [],
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["endpoints"] });
      qc.invalidateQueries({ queryKey: ["models"] });
      reset();
      onClose();
    },
    onError: (e: Error) => setErr(e.message),
  });

  const reset = () => {
    setBaseUrl("");
    setApiKey("");
    setProbeResult(null);
    setSelected(new Set());
    setVisionPrefs({});
    setErr("");
  };

  const pickedCount = useMemo(
    () =>
      probeResult
        ? probeResult.models.filter((m) => selected.has(m.model_id)).length
        : 0,
    [probeResult, selected],
  );

  if (!open) return null;

  const canSave = baseUrl.trim().length > 0 && apiKey.trim().length > 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={mode === "model" ? "添加模型" : "添加分组"}
    >
      <div
        className="flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl border bg-[var(--color-surface)] shadow-2xl"
        style={{ borderColor: "var(--color-border)" }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* 头部 */}
        <div className="flex items-start justify-between border-b px-5 py-4" style={{ borderColor: "var(--color-border)" }}>
          <div>
            <h2 className="text-base font-medium">
              {mode === "model" ? "添加模型" : "添加分组"}
            </h2>
            <p className="mt-0.5 text-xs" style={{ color: "var(--color-muted)" }}>
              输入地址和 API Key，自动探测模型并按供应商分组。同名或同地址会并入已有分组。
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="rounded p-1 hover:bg-[var(--color-surface-2)]"
          >
            <X size={16} style={{ color: "var(--color-muted)" }} />
          </button>
        </div>

        {/* 表单 */}
        <div className="flex-1 space-y-4 overflow-y-auto px-5 py-4">
          <Field
            label="地址（Base URL）"
            hint="例如 https://api.deepseek.com。会规范化（补 /v1、去尾斜杠）。"
          >
            <input
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://api.example.com"
              className={inputCls}
              style={inputStyle}
              autoFocus
            />
          </Field>

          <Field label="API Key" hint="只存密文，界面只回显尾 4 位。">
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="sk-..."
              className={inputCls}
              style={inputStyle}
            />
          </Field>

          {!probeResult && (
            <button
              type="button"
              onClick={() => probe.mutate()}
              disabled={!canSave || probe.isPending}
              className="inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-sm text-white disabled:opacity-50"
              style={{ background: "var(--color-accent)" }}
            >
              {probe.isPending ? (
                <Loader2 size={14} className="animate-spin" />
              ) : (
                <Search size={14} />
              )}
              探测模型列表
            </button>
          )}

          {err && (
            <p role="alert" className="flex items-start gap-1.5 text-sm" style={{ color: "var(--color-err)" }}>
              <AlertTriangle size={14} className="mt-0.5 shrink-0" />
              {err}
            </p>
          )}

          {/* 探测结果 */}
          {probeResult && (
            <div className="space-y-2">
              <div className="flex items-center gap-2 rounded-lg border px-3 py-2 text-xs" style={{ borderColor: "var(--color-border)", background: "var(--color-bg)" }}>
                <Server size={14} style={{ color: "var(--color-accent)" }} />
                <span style={{ color: "var(--color-muted)" }}>将归入分组</span>
                <span className="font-medium">{probeResult.suggested_name}</span>
                <span className="truncate font-mono" style={{ color: "var(--color-muted)" }}>
                  {probeResult.normalized_base_url}
                </span>
              </div>

              <div className="flex items-center justify-between text-xs" style={{ color: "var(--color-muted)" }}>
                <span>已选 {pickedCount} / {probeResult.models.length} 个模型</span>
                <div className="flex gap-3">
                  <button type="button" onClick={() => setSelected(new Set(probeResult.models.map((m) => m.model_id)))}>全选</button>
                  <button type="button" onClick={() => setSelected(new Set())}>清空</button>
                </div>
              </div>

              <ul className="max-h-64 space-y-1 overflow-y-auto rounded-lg border p-1" style={{ borderColor: "var(--color-border)" }}>
                {probeResult.models.map((m) => {
                  const on = selected.has(m.model_id);
                  return (
                    <li key={m.model_id}>
                      <div
                        className={clsx(
                          "flex w-full items-center gap-2 rounded px-2 py-1.5 text-xs",
                          on && "bg-[var(--color-surface-2)]",
                        )}
                      >
                        <button
                          type="button"
                          onClick={() => {
                            setSelected((prev) => {
                              const next = new Set(prev);
                              if (next.has(m.model_id)) next.delete(m.model_id);
                              else next.add(m.model_id);
                              return next;
                            });
                          }}
                          className="flex min-w-0 flex-1 items-center gap-2 text-left"
                        >
                          <span
                            className="flex h-4 w-4 shrink-0 items-center justify-center rounded border"
                            style={{ borderColor: "var(--color-border)", background: on ? "var(--color-accent)" : "transparent" }}
                          >
                            {on && <Check size={11} className="text-white" />}
                          </span>
                          <span className="min-w-0 flex-1 truncate font-mono">{m.model_id}</span>
                          <span className="shrink-0" style={{ color: "var(--color-muted)" }}>
                            {Math.round(m.context_window / 1000)}K
                          </span>
                          {m.looks_non_chat && (
                            <span className="shrink-0" style={{ color: "var(--color-muted)" }}>
                              非对话
                            </span>
                          )}
                        </button>
                        {/* 视觉能力三态：探测不返回，默认未知，用户可手动指定 */}
                        <select
                          value={visionPrefs[m.model_id] ?? "unknown"}
                          onChange={(e) =>
                            setVisionPrefs((prev) => ({
                              ...prev,
                              [m.model_id]: e.target.value as "true" | "false" | "unknown",
                            }))
                          }
                          onClick={(e) => e.stopPropagation()}
                          title="视觉能力（探测不到，默认未知，可手动指定）"
                          className="shrink-0 rounded border px-1 py-0.5 text-[11px] outline-none"
                          style={{ borderColor: "var(--color-border)", background: "var(--color-bg)" }}
                        >
                          <option value="unknown">视觉 ?</option>
                          <option value="true">视觉 ✓</option>
                          <option value="false">视觉 ✗</option>
                        </select>
                      </div>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
        </div>

        {/* 底部按钮 */}
        <div className="flex items-center justify-end gap-3 border-t px-5 py-4" style={{ borderColor: "var(--color-border)" }}>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border px-4 py-2 text-sm hover:bg-[var(--color-surface-2)]"
            style={{ borderColor: "var(--color-border)" }}
          >
            取消
          </button>
          <button
            type="button"
            onClick={() => save.mutate()}
            disabled={!canSave || save.isPending}
            className="inline-flex items-center gap-1.5 rounded-lg px-4 py-2 text-sm text-white disabled:opacity-50"
            style={{ background: "var(--color-accent)" }}
          >
            {save.isPending && <Loader2 size={14} className="animate-spin" />}
            {mode === "group" ? "创建分组" : `添加${pickedCount > 0 ? ` ${pickedCount} 个模型` : ""}`}
          </button>
        </div>
      </div>
    </div>
  );
}
