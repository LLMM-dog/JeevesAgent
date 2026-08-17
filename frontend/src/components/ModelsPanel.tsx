/**
 * 模型配置面板。
 *
 * 布局与交互：
 * - 按分组（端点）纵向排列，分组之间用线隔开；分组头带名字、地址、Key 尾号。
 * - 模型以卡片展示，卡片显示模型名 + 类型图标 + 启用开关。
 * - 卡片可拖动到别的分组（改归属），点击卡片进详情编辑。
 * - 分组头右侧的「+」展开快捷添加表单（自动拉该分组的模型列表搜索）。
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronDown,
  ChevronRight,
  CircleHelp,
  Eye,
  EyeOff,
  GripVertical,
  Inbox,
  Pencil,
  Plus,
  Server,
  Trash2,
  Wrench,
  Target,
} from "lucide-react";
import clsx from "clsx";

import { api } from "@/lib/api";
import { modelTypeMeta } from "@/lib/modelMeta";
import { PURPOSE_META } from "@/lib/purposeMeta";
import type { EndpointOut, ModelItem, Purpose } from "@/lib/types";
import { AddEndpointDialog } from "./AddEndpointDialog";
import { AddModelForm } from "./AddModelForm";
import { BindingsDialog } from "./BindingsDialog";
import { ModelEditDialog } from "./ModelEditDialog";
import VisionPanel from "./VisionPanel";

// ─────────────────────────── 模型卡片 ───────────────────────────

function ModelCard({
  model,
  dragging,
  onToggle,
  onEdit,
  onDelete,
  onDragStart,
  onDragEnd,
}: {
  model: ModelItem;
  dragging: boolean;
  onToggle: () => void;
  onEdit: () => void;
  onDelete: () => void;
  onDragStart: (e: React.DragEvent) => void;
  onDragEnd: () => void;
}) {
  const type = modelTypeMeta(model.model_type);
  const TypeIcon = type.icon;
  const name = model.display_name || model.model_id;

  return (
    <div
      draggable
      onDragStart={onDragStart}
      onDragEnd={onDragEnd}
      onClick={onEdit}
      title="点击编辑，按住拖动到别的分组"
      className={clsx(
        "group relative cursor-grab rounded-lg border p-3 transition-colors active:cursor-grabbing",
        dragging && "opacity-40",
        model.enabled
          ? "bg-[var(--color-surface)] hover:border-[var(--color-accent)]"
          : "opacity-60",
      )}
      style={{ borderColor: "var(--color-border)" }}
    >
      {/* 拖动手柄：悬浮时出现在左侧，提示可拖 */}
      <div className="pointer-events-none absolute left-1.5 top-1/2 -translate-y-1/2 opacity-0 transition-opacity group-hover:opacity-60">
        <GripVertical size={14} style={{ color: "var(--color-muted)" }} />
      </div>

      <div className="flex items-start gap-2 pl-3 pr-12">
        {/* 类型图标 */}
        <span
          className="mt-0.5 shrink-0"
          title={type.label}
          style={{ color: "var(--color-muted)" }}
        >
          <TypeIcon size={16} aria-hidden />
        </span>

        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium" style={{ color: "var(--color-text)" }}>
            {name}
          </div>
          <div className="mt-0.5 flex items-center gap-1.5 text-[11px]" style={{ color: "var(--color-muted)" }}>
            {/* 视觉能力三态：支持=眼睛，不支持=斜杠眼睛，未知=问号（后果用户自负） */}
            {model.supports_vision === "true" && (
              <span className="inline-flex items-center gap-0.5" title="支持视觉" style={{ color: "var(--color-ok)" }}>
                <Eye size={11} aria-hidden />
              </span>
            )}
            {model.supports_vision === "false" && (
              <span className="inline-flex items-center gap-0.5" title="不支持视觉">
                <EyeOff size={11} aria-hidden />
              </span>
            )}
            {model.supports_vision === "unknown" && (
              <span
                className="inline-flex items-center gap-0.5"
                title="视觉能力未知（未核验）。可去「看图能力核验」实测"
                style={{ color: "var(--color-warn)" }}
              >
                <CircleHelp size={11} aria-hidden />
              </span>
            )}
            {/* 工具调用能力：扳手 */}
            {model.supports_tools === "true" && (
              <span className="inline-flex items-center gap-0.5" title="支持工具调用">
                <Wrench size={11} aria-hidden />
              </span>
            )}
            <span>{type.label}</span>
            <span>·</span>
            <span>{Math.round(model.context_window / 1000)}K</span>
            {/* 被绑定到的功能位：用 accent 徽标显示中文名 */}
            {model.bindings.length > 0 && (
              <span
                className="inline-flex items-center gap-0.5 rounded px-1 py-0.5"
                title="该模型被配置到这些功能位"
                style={{ background: "var(--color-accent)/15", color: "var(--color-accent)" }}
              >
                {model.bindings.map((p) => PURPOSE_META[p as Purpose]?.label ?? p).join("、")}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* 启用/禁用滑块 */}
      <button
        type="button"
        role="switch"
        aria-checked={model.enabled}
        aria-label={model.enabled ? `禁用 ${name}` : `启用 ${name}`}
        title={
          model.enabled
            ? "禁用后配置保留，只是不出现在别处的模型菜单里"
            : "启用后可在别处配置模型时选择"
        }
        onClick={(e) => {
          e.stopPropagation();
          onToggle();
        }}
        className="absolute right-3 top-3"
      >
        <span
          className={clsx(
            "relative inline-flex h-5 w-9 items-center rounded-full transition-colors",
            model.enabled ? "bg-[var(--color-accent)]" : "bg-[var(--color-surface-2)]",
          )}
          style={{ boxShadow: model.enabled ? undefined : "inset 0 0 0 1px var(--color-border)" }}
        >
          <span
            className={clsx(
              "inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform",
              model.enabled ? "translate-x-[18px]" : "translate-x-[3px]",
            )}
          />
        </span>
      </button>

      {/* 删除：悬浮显示 */}
      <button
        type="button"
        aria-label={`删除 ${name}`}
        onClick={(e) => {
          e.stopPropagation();
          if (confirm(`确定删除模型「${name}」吗？同组的其它模型不受影响。`)) onDelete();
        }}
        className="absolute bottom-2 right-2 rounded p-1 opacity-0 transition-opacity hover:bg-[var(--color-surface-2)] group-hover:opacity-100"
        style={{ color: "var(--color-err)" }}
      >
        <Trash2 size={13} />
      </button>
    </div>
  );
}

// ─────────────────────────── 主面板 ───────────────────────────

export default function ModelsPanel() {
  const qc = useQueryClient();
  const [addDialog, setAddDialog] = useState<null | "model" | "group">(null);
  const [bindingsOpen, setBindingsOpen] = useState(false);
  const [quickAdd, setQuickAdd] = useState<string | null>(null);
  const [editing, setEditing] = useState<{
    model: ModelItem | null;
    endpoint: EndpointOut;
  } | null>(null);
  const [dragModelId, setDragModelId] = useState<string | null>(null);
  const [dropTarget, setDropTarget] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  const endpoints = useQuery({ queryKey: ["endpoints"], queryFn: api.listEndpoints });
  const models = useQuery({ queryKey: ["models", "all"], queryFn: () => api.models() });

  const toggleModel = useMutation({
    mutationFn: (m: ModelItem) => api.patchModel(m.id, { enabled: !m.enabled }),
    onError: (e: Error) => alert(e.message),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["models"] });
      // 禁用会自动解绑功能位，绑定列表要跟着刷
      qc.invalidateQueries({ queryKey: ["bindings"] });
    },
  });

  // 禁用被绑定的模型前先提醒：关闭会把这些功能位解绑变空。
  const handleToggle = (m: ModelItem) => {
    if (m.enabled && m.bindings.length > 0) {
      const labels = m.bindings
        .map((p) => PURPOSE_META[p as Purpose]?.label ?? p)
        .join("、");
      if (!confirm(`这个模型正被【${labels}】使用，关闭后将自动解绑这些功能位。继续？`)) {
        return;
      }
    }
    toggleModel.mutate(m);
  };

  const deleteModel = useMutation({
    mutationFn: (pk: string) => api.deleteModel(pk),
    onError: (e: Error) => alert(e.message),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["models"] });
      qc.invalidateQueries({ queryKey: ["endpoints"] });
    },
  });

  const deleteEndpoint = useMutation({
    mutationFn: (id: string) => api.deleteEndpoint(id),
    onError: (e: Error) => alert(e.message),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["models"] });
      qc.invalidateQueries({ queryKey: ["endpoints"] });
    },
  });

  const moveModel = useMutation({
    mutationFn: (args: { modelId: string; endpointId: string }) =>
      api.patchModel(args.modelId, { endpoint_id: args.endpointId }),
    onError: (e: Error) => alert(e.message),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["models"] });
      qc.invalidateQueries({ queryKey: ["endpoints"] });
    },
  });

  const allModels = models.data?.items ?? [];
  const allEndpoints = endpoints.data?.items ?? [];

  const grouped = allEndpoints.map((endpoint) => ({
    endpoint,
    models: allModels.filter((m) => m.endpoint_id === endpoint.id),
  }));

  const onDrop = (endpoint: EndpointOut) => {
    setDropTarget(null);
    if (dragModelId) {
      const m = allModels.find((x) => x.id === dragModelId);
      if (m && m.endpoint_id !== endpoint.id) {
        moveModel.mutate({ modelId: m.id, endpointId: endpoint.id });
      }
    }
    setDragModelId(null);
  };

  return (
    <div className="space-y-5">
      {/* 页面头：标题 + 说明 + 右上角两个按钮 */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-base font-medium" style={{ color: "var(--color-text)" }}>
            模型配置
          </h2>
          <p className="mt-1 max-w-xl text-sm" style={{ color: "var(--color-muted)" }}>
            按供应商分组管理模型。填地址 + Key 探测即可自动分组；卡片可拖动改分组，
            开关控制模型在别处配置时是否可见。
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <button
            type="button"
            onClick={() => setAddDialog("model")}
            className="inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-sm text-white hover:opacity-90"
            style={{ background: "var(--color-accent)" }}
          >
            <Plus size={14} />
            添加模型
          </button>
          <button
            type="button"
            onClick={() => setAddDialog("group")}
            className="inline-flex items-center gap-1.5 rounded-lg border px-3 py-2 text-sm hover:bg-[var(--color-surface-2)]"
            style={{ borderColor: "var(--color-border)" }}
          >
            <Server size={14} />
            添加分组
          </button>
          <button
            type="button"
            onClick={() => setBindingsOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-lg border px-3 py-2 text-sm hover:bg-[var(--color-surface-2)]"
            style={{ borderColor: "var(--color-border)" }}
          >
            <Target size={14} />
            功能位绑定
          </button>
        </div>
      </div>

      {/* 模型列表 */}
      {grouped.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed py-16 text-center" style={{ borderColor: "var(--color-border)" }}>
          <Inbox size={40} style={{ color: "var(--color-muted)" }} />
          <p className="mt-3 text-sm" style={{ color: "var(--color-muted)" }}>
            还没有配置模型
          </p>
          <p className="mt-1 text-xs" style={{ color: "var(--color-muted)" }}>
            点右上角「添加模型」，输入地址和 API Key 自动探测
          </p>
        </div>
      ) : (
        <div className="space-y-7">
          {grouped.map(({ endpoint, models: ms }) => (
            <section
              key={endpoint.id}
              onDragOver={(e) => {
                if (dragModelId) {
                  e.preventDefault();
                  e.dataTransfer.dropEffect = "move";
                  if (dropTarget !== endpoint.id) setDropTarget(endpoint.id);
                }
              }}
              onDrop={(e) => {
                e.preventDefault();
                onDrop(endpoint);
              }}
              className={clsx(
                "rounded-lg border-t pt-4 transition-colors",
                dropTarget === endpoint.id && "bg-[var(--color-surface-2)]",
              )}
              style={{ borderColor: "var(--color-border)" }}
            >
              {/* 分组头（线隔开） */}
              <div className="flex flex-wrap items-center justify-between gap-2 border-b pb-2" style={{ borderColor: "var(--color-border)" }}>
                <div className="flex min-w-0 items-center gap-1.5">
                  <button
                    type="button"
                    aria-expanded={!collapsed.has(endpoint.id)}
                    aria-label={collapsed.has(endpoint.id) ? `展开分组 ${endpoint.name}` : `收起分组 ${endpoint.name}`}
                    onClick={() =>
                      setCollapsed((prev) => {
                        const next = new Set(prev);
                        if (next.has(endpoint.id)) next.delete(endpoint.id);
                        else next.add(endpoint.id);
                        return next;
                      })
                    }
                    className="rounded p-0.5 hover:bg-[var(--color-surface-2)]"
                    style={{ color: "var(--color-muted)" }}
                  >
                    {collapsed.has(endpoint.id) ? <ChevronRight size={16} /> : <ChevronDown size={16} />}
                  </button>
                  <Server size={15} style={{ color: "var(--color-accent)" }} />
                  <span className="text-sm font-medium">{endpoint.name}</span>
                  <span className="text-xs" style={{ color: "var(--color-muted)" }}>
                    {ms.length} 个模型
                  </span>
                  <span className="truncate font-mono text-xs" style={{ color: "var(--color-muted)" }}>
                    {endpoint.base_url}
                  </span>
                  <span className="shrink-0 text-xs" style={{ color: "var(--color-muted)" }}>
                    ·{endpoint.key_hint}
                  </span>
                </div>

                <div className="flex shrink-0 items-center gap-1">
                  <button
                    type="button"
                    onClick={() => setQuickAdd((v) => (v === endpoint.id ? null : endpoint.id))}
                    className="inline-flex items-center gap-1 rounded-lg border px-2.5 py-1.5 text-xs hover:bg-[var(--color-surface-2)]"
                    style={{ borderColor: "var(--color-accent)", color: "var(--color-accent)" }}
                  >
                    <Plus size={13} />
                    添加模型
                  </button>
                  <button
                    type="button"
                    aria-label={`编辑分组 ${endpoint.name}`}
                    onClick={() => setEditing({ model: null, endpoint })}
                    className="rounded p-1.5 hover:bg-[var(--color-surface-2)]"
                    style={{ color: "var(--color-muted)" }}
                  >
                    <Pencil size={14} />
                  </button>
                  <button
                    type="button"
                    aria-label={`删除分组 ${endpoint.name}`}
                    onClick={() => {
                      if (
                        confirm(
                          `确定删除整个模型组「${endpoint.name}」及其所有模型吗？` +
                            `只想删一个模型请用模型行上的删除按钮。`,
                        )
                      ) {
                        deleteEndpoint.mutate(endpoint.id);
                      }
                    }}
                    className="rounded p-1.5 hover:bg-[var(--color-surface-2)]"
                    style={{ color: "var(--color-muted)" }}
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>

              {collapsed.has(endpoint.id) ? null : (
                <>
              {/* 快捷添加表单（点分组头 + 展开） */}
              {quickAdd === endpoint.id && (
                <AddModelForm
                  endpointId={endpoint.id}
                  onDone={() => {
                    setQuickAdd(null);
                    qc.invalidateQueries({ queryKey: ["models"] });
                    qc.invalidateQueries({ queryKey: ["endpoints"] });
                  }}
                  onCancel={() => setQuickAdd(null)}
                />
              )}

              {/* 卡片网格 */}
              {ms.length === 0 ? (
                <div className="rounded-lg border border-dashed px-3 py-6 text-center text-xs" style={{ borderColor: "var(--color-border)", color: "var(--color-muted)" }}>
                  该分组还没有模型。点右侧「添加模型」，或从别的分组把卡片拖进来。
                </div>
              ) : (
                <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                  {ms.map((m) => (
                    <ModelCard
                      key={m.id}
                      model={m}
                      dragging={dragModelId === m.id}
                      onToggle={() => handleToggle(m)}
                      onEdit={() => setEditing({ model: m, endpoint })}
                      onDelete={() => deleteModel.mutate(m.id)}
                      onDragStart={(e) => {
                        setDragModelId(m.id);
                        e.dataTransfer.effectAllowed = "move";
                        e.dataTransfer.setData("text/plain", m.id);
                      }}
                      onDragEnd={() => {
                        setDragModelId(null);
                        setDropTarget(null);
                      }}
                    />
                  ))}
                </div>
              )}
                </>
              )}
            </section>
          ))}
        </div>
      )}

      {/* 看图能力核验：批量核验每个模型是否支持图片输入 */}
      {grouped.length > 0 && <VisionPanel />}

      {/* 添加模型 / 添加分组 */}
      {addDialog && (
        <AddEndpointDialog
          open
          mode={addDialog}
          onClose={() => setAddDialog(null)}
        />
      )}

      {/* 功能位绑定 */}
      {bindingsOpen && (
        <BindingsDialog open onClose={() => setBindingsOpen(false)} />
      )}

      {/* 编辑模型 / 编辑分组 */}
      {editing && (
        <ModelEditDialog
          key={editing.model?.id ?? editing.endpoint.id}
          model={editing.model}
          endpoint={editing.endpoint}
          endpoints={allEndpoints}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  );
}
