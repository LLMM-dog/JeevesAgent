/**
 * 编辑模型 / 编辑分组的对话框。
 *
 * - 传入 model 时：编辑模型字段 + 可下拉移动分组 + 编辑当前分组的名字/地址/Key。
 * - 只传 endpoint 时（分组头上点编辑）：只编辑分组的名字/地址/Key。
 *
 * Key 永远拿不到明文，所以 Key 输入框初始为空 —— 留空表示不改。
 */

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, X } from "lucide-react";

import { api } from "@/lib/api";
import { MODEL_TYPE_META } from "@/lib/modelMeta";
import type { EndpointOut, ModelItem, ModelType } from "@/lib/types";

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

function shortBaseUrl(baseUrl: string, fallback: string): string {
  if (!baseUrl) return fallback;
  try {
    return new URL(baseUrl).host || baseUrl;
  } catch {
    return baseUrl.replace(/^https?:\/\//, "").split("/")[0] || fallback;
  }
}

function parsePrice(v: string): number | null {
  if (v.trim() === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

export function ModelEditDialog({
  model,
  endpoint,
  endpoints,
  onClose,
}: {
  model: ModelItem | null;
  endpoint: EndpointOut;
  endpoints: EndpointOut[];
  onClose: () => void;
}) {
  const qc = useQueryClient();

  const [displayName, setDisplayName] = useState(model?.display_name ?? "");
  const [contextWindow, setContextWindow] = useState(
    String(model?.context_window ?? 128000),
  );
  const [modelType, setModelType] = useState<ModelType>(model?.model_type ?? "chat");
  const [supportsVision, setSupportsVision] = useState<"true" | "false" | "unknown">(
    model?.supports_vision ?? "unknown",
  );
  const [supportsTools, setSupportsTools] = useState<"true" | "false" | "unknown">(
    model?.supports_tools ?? "unknown",
  );
  const [priceIn, setPriceIn] = useState(
    model?.price_in_per_1m != null ? String(model.price_in_per_1m) : "",
  );
  const [priceOut, setPriceOut] = useState(
    model?.price_out_per_1m != null ? String(model.price_out_per_1m) : "",
  );
  const [enabled, setEnabled] = useState(model?.enabled ?? true);
  const [targetGroupId, setTargetGroupId] = useState(
    model ? model.group_id || model.endpoint_id : endpoint.id,
  );

  const [groupName, setGroupName] = useState(endpoint.name);
  const [groupBaseUrl, setGroupBaseUrl] = useState(endpoint.base_url);
  const [groupApiKey, setGroupApiKey] = useState("");

  const isCustomGroup = !endpoint.base_url;
  const sourceEndpoint = model
    ? endpoints.find((e) => e.id === model.endpoint_id)
    : undefined;

  const save = useMutation({
    mutationFn: async () => {
      if (model) {
        await api.patchModel(model.id, {
          display_name: displayName,
          context_window: Number(contextWindow),
          model_type: modelType,
          supports_vision: supportsVision,
          supports_tools: supportsTools,
          price_in_per_1m: parsePrice(priceIn),
          price_out_per_1m: parsePrice(priceOut),
          enabled,
          ...(targetGroupId !== (model.group_id || model.endpoint_id)
            ? { group_id: targetGroupId }
            : {}),
        });
      }
      // 分组字段改了才发。Key 留空 = 不改；自定义分组没有地址/Key。
      const endpointPatch: {
        name?: string;
        base_url?: string;
        api_key?: string;
      } = {};
      if (groupName !== endpoint.name) endpointPatch.name = groupName;
      if (endpoint.base_url && groupBaseUrl !== endpoint.base_url) {
        endpointPatch.base_url = groupBaseUrl;
      }
      if (endpoint.base_url && groupApiKey.trim() !== "") {
        endpointPatch.api_key = groupApiKey;
      }
      if (Object.keys(endpointPatch).length > 0) {
        await api.patchEndpoint(endpoint.id, endpointPatch);
      }
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["endpoints"] });
      qc.invalidateQueries({ queryKey: ["models"] });
      qc.invalidateQueries({ queryKey: ["bindings"] });
      onClose();
    },
  });

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={model ? "编辑模型" : "编辑分组"}
    >
      <div
        className="flex max-h-[90vh] w-full max-w-lg flex-col overflow-hidden rounded-xl border bg-[var(--color-surface)] shadow-2xl"
        style={{ borderColor: "var(--color-border)" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between border-b px-5 py-4" style={{ borderColor: "var(--color-border)" }}>
          <div>
            <h2 className="text-base font-medium">
              {model ? model.model_id : `编辑分组「${endpoint.name}」`}
            </h2>
            <p className="mt-0.5 text-xs" style={{ color: "var(--color-muted)" }}>
              {model
                ? "模型详情与所属分组。"
                : isCustomGroup
                  ? "自定义分组只有名称，模型调用仍走各自来源。"
                  : "分组自带地址和 Key，其下模型共用。"}
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

        <div className="flex-1 space-y-4 overflow-y-auto px-5 py-4">
          {model && (
            <>
              <div className="grid grid-cols-2 gap-3">
                <Field label="模型名（自动探测，不可更改）">
                  <input
                    value={model.model_id}
                    disabled
                    className={inputCls}
                    style={{ ...inputStyle, opacity: 0.6 }}
                  />
                </Field>
                <Field label="来源">
                  <input
                    value={
                      sourceEndpoint
                        ? shortBaseUrl(sourceEndpoint.base_url, sourceEndpoint.name)
                        : model.endpoint_name
                    }
                    disabled
                    className={inputCls}
                    style={{ ...inputStyle, opacity: 0.6 }}
                  />
                </Field>
              </div>

              <Field label="显示名称" hint="默认是模型名，可改成更友好的名字。">
                <input
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                  placeholder={model.model_id}
                  className={inputCls}
                  style={inputStyle}
                />
              </Field>

              <div className="grid grid-cols-2 gap-3">
                <Field label="上下文窗口" hint="压缩阈值按这个算。">
                  <input
                    type="number"
                    value={contextWindow}
                    onChange={(e) => setContextWindow(e.target.value)}
                    className={inputCls}
                    style={inputStyle}
                  />
                </Field>
                <Field label="模型类型">
                  <select
                    value={modelType}
                    onChange={(e) => setModelType(e.target.value as ModelType)}
                    className={inputCls}
                    style={inputStyle}
                  >
                    {Object.entries(MODEL_TYPE_META).map(([k, v]) => (
                      <option key={k} value={k}>
                        {v.label}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <Field label="视觉能力" hint="未知 = 没测过，发图前可核验。">
                  <select
                    value={supportsVision}
                    onChange={(e) => setSupportsVision(e.target.value as "true" | "false" | "unknown")}
                    className={inputCls}
                    style={inputStyle}
                  >
                    <option value="true">支持视觉</option>
                    <option value="false">不支持视觉</option>
                    <option value="unknown">未知</option>
                  </select>
                </Field>
                <Field label="工具调用" hint="未知 = 没测过。">
                  <select
                    value={supportsTools}
                    onChange={(e) => setSupportsTools(e.target.value as "true" | "false" | "unknown")}
                    className={inputCls}
                    style={inputStyle}
                  >
                    <option value="true">支持工具调用</option>
                    <option value="false">不支持工具调用</option>
                    <option value="unknown">未知</option>
                  </select>
                </Field>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <Field label="输入价（$/1M）" hint="留空 = 未配价。">
                  <input
                    type="number"
                    step="0.01"
                    value={priceIn}
                    onChange={(e) => setPriceIn(e.target.value)}
                    className={inputCls}
                    style={inputStyle}
                  />
                </Field>
                <Field label="输出价（$/1M）">
                  <input
                    type="number"
                    step="0.01"
                    value={priceOut}
                    onChange={(e) => setPriceOut(e.target.value)}
                    className={inputCls}
                    style={inputStyle}
                  />
                </Field>
              </div>

              <Field label="所属分组" hint="只改展示位置；模型调用仍走上面的来源。也可在卡片上直接拖动。">
                <select
                  value={targetGroupId}
                  onChange={(e) => setTargetGroupId(e.target.value)}
                  className={inputCls}
                  style={inputStyle}
                >
                  {endpoints.map((ep) => (
                    <option key={ep.id} value={ep.id}>
                      {ep.name}
                    </option>
                  ))}
                </select>
              </Field>

              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={enabled}
                  onChange={(e) => setEnabled(e.target.checked)}
                  className="h-4 w-4 rounded"
                />
                启用（在对话页的模型菜单里可见）
              </label>
            </>
          )}

          <div className="border-t pt-3" style={{ borderColor: "var(--color-border)" }}>
            <p className="mb-3 text-xs font-medium" style={{ color: "var(--color-muted)" }}>
              {isCustomGroup ? "自定义分组" : "分组（地址 / Key）"}
            </p>
            <div className="space-y-3">
              <Field label="分组名">
                <input
                  value={groupName}
                  onChange={(e) => setGroupName(e.target.value)}
                  className={inputCls}
                  style={inputStyle}
                />
              </Field>
              {!isCustomGroup && (
                <>
                  <Field label="地址（Base URL）">
                    <input
                      value={groupBaseUrl}
                      onChange={(e) => setGroupBaseUrl(e.target.value)}
                      className={inputCls}
                      style={inputStyle}
                    />
                  </Field>
                  <Field
                    label="API Key"
                    hint={`当前尾 4 位：${endpoint.key_hint}。留空保持不变，重新填才会更新。`}
                  >
                    <input
                      type="password"
                      value={groupApiKey}
                      onChange={(e) => setGroupApiKey(e.target.value)}
                      placeholder="留空不变"
                      className={inputCls}
                      style={inputStyle}
                    />
                  </Field>
                </>
              )}
            </div>
          </div>

          {save.isError && (
            <p role="alert" className="text-sm" style={{ color: "var(--color-err)" }}>
              {(save.error as Error).message}
            </p>
          )}
        </div>

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
            disabled={save.isPending}
            className="inline-flex items-center gap-1.5 rounded-lg px-4 py-2 text-sm text-white disabled:opacity-50"
            style={{ background: "var(--color-accent)" }}
          >
            {save.isPending && <Loader2 size={14} className="animate-spin" />}
            保存
          </button>
        </div>
      </div>
    </div>
  );
}
