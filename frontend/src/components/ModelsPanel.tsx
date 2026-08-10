import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronRight,
  Eye,
  EyeOff,
  Plus,
  Server,
  Trash2,
  Wrench,
} from "lucide-react";
import clsx from "clsx";

import { AddModelForm } from "./AddModelForm";
import { api } from "../lib/api";
import type { ModelItem, ProviderOut } from "../lib/types";

/**
 * 模型配置：按模型组分组，但增删的单位是【单个模型】。
 *
 * ## 为什么不能以模型组为单位
 *
 * 原来删一个模型组会连带删掉它下面所有模型和功能位绑定 —— 用户只是
 * 想去掉一个不用的模型，结果配置全没了要重做一遍。
 *
 * 现在模型组只是分组（哪个端点、哪个 Key），删模型就只删那一个。
 * 删整个模型组仍然可以，但要单独点组标题上的删除，且提示写清后果。
 *
 * ## 启用/禁用是什么
 *
 * 控制这个模型在对话页的快捷切换菜单里出不出现。禁用不动配置 ——
 * "这个我暂时不用，但别让我重新配"是很常见的状态，而删除是不可逆的。
 */
export default function ModelsPanel() {
  const qc = useQueryClient();
  // 默认全部展开：模型少的时候折起来反而多一次点击。
  // 记住的是"被手动收起的组"，而不是"被展开的组"。
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [addingTo, setAddingTo] = useState<string | null>(null);
  const [err, setErr] = useState("");

  const providers = useQuery({
    queryKey: ["providers"],
    queryFn: api.listProviders,
  });
  const models = useQuery({
    queryKey: ["models", "all"],
    // 设置页要看到【全部】，包括禁用的 —— 否则没法重新启用
    queryFn: () => api.models(),
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["models"] });
    qc.invalidateQueries({ queryKey: ["providers"] });
    qc.invalidateQueries({ queryKey: ["bindings"] });
  };


  const toggle = useMutation({
    mutationFn: (m: ModelItem) =>
      api.patchModel(m.id, { enabled: !m.enabled }),
    onSuccess: invalidate,
    onError: (e: Error) => setErr(e.message),
  });

  const delModel = useMutation({
    mutationFn: (pk: string) => api.deleteModel(pk),
    onSuccess: invalidate,
    onError: (e: Error) => setErr(e.message),
  });

  const delProvider = useMutation({
    mutationFn: (id: string) => api.deleteProvider(id),
    onSuccess: invalidate,
    onError: (e: Error) => setErr(e.message),
  });

  const allModels = models.data?.items ?? [];
  const provs = providers.data?.items ?? [];

  return (
    <section className="space-y-4">
      <header>
        <h2 className="text-lg font-medium">模型</h2>
        <p className="mt-1 text-sm" style={{ color: "var(--color-muted)" }}>
          按模型组分组。增删的单位是单个模型 —— 删一个不影响同组的其它模型。
          <br />
          禁用的模型不出现在对话页的快捷切换菜单里，但配置保留。
        </p>
      </header>

      {err && (
        <p
          role="alert"
          className="rounded border px-3 py-2 text-sm"
          style={{ borderColor: "var(--color-err)", color: "var(--color-err)" }}
        >
          {err}
        </p>
      )}

      {provs.length === 0 ? (
        <p className="text-sm" style={{ color: "var(--color-muted)" }}>
          还没有端点。先在上方「添加端点」里加一个端点。
        </p>
      ) : (
        <ul className="space-y-3">
          {provs.map((p: ProviderOut) => {
            const mine = allModels.filter((m) => m.provider_id === p.id);
            const isCollapsed = collapsed.has(p.id);
            const onCount = mine.filter((m) => m.enabled).length;
            return (
              <li
                key={p.id}
                className="overflow-hidden rounded-lg border"
                style={{ borderColor: "var(--color-border)" }}
              >
                {/* 组标题 */}
                <div
                  className="flex items-center gap-2 px-3 py-2"
                  style={{ background: "var(--color-surface-2)" }}
                >
                  <button
                    onClick={() =>
                      setCollapsed((s) => {
                        const n = new Set(s);
                        if (n.has(p.id)) n.delete(p.id);
                        else n.add(p.id);
                        return n;
                      })
                    }
                    aria-expanded={!isCollapsed}
                    aria-label={`${isCollapsed ? "展开" : "收起"} ${p.name}`}
                    className="flex min-w-0 flex-1 items-center gap-2 text-left"
                  >
                    <ChevronRight
                      size={14}
                      className={clsx(
                        "shrink-0 transition-transform",
                        !isCollapsed && "rotate-90",
                      )}
                    />
                    <Server size={13} className="shrink-0" />
                    <span className="truncate text-sm font-medium">{p.name}</span>
                    <span
                      className="shrink-0 text-xs"
                      style={{ color: "var(--color-muted)" }}
                    >
                      {mine.length} 个模型
                      {mine.length > 0 && ` · ${onCount} 个启用`}
                    </span>
                  </button>

                  <button
                    onClick={() => {
                      setAddingTo(addingTo === p.id ? null : p.id);
                      setErr("");
                    }}
                    className="flex shrink-0 items-center gap-1 rounded px-2 py-1 text-xs"
                    style={{ background: "var(--color-accent)", color: "#fff" }}
                  >
                    <Plus size={12} />
                    加模型
                  </button>

                  <button
                    onClick={() => {
                      if (
                        confirm(
                          `删除整个模型组「${p.name}」？\n\n` +
                            `它下面 ${mine.length} 个模型和相关的功能位绑定都会删掉。\n` +
                            `只想去掉某个模型的话，用模型行上的删除按钮。`,
                        )
                      ) {
                        delProvider.mutate(p.id);
                      }
                    }}
                    aria-label={`删除模型组 ${p.name}`}
                    title="删除整个模型组（含它的所有模型）"
                    className="shrink-0 rounded p-1"
                    style={{ color: "var(--color-muted)" }}
                  >
                    <Trash2 size={13} />
                  </button>
                </div>

                {/* 端点信息 */}
                {!isCollapsed && (
                  <p
                    className="truncate border-b px-3 py-1.5 font-mono text-[11px]"
                    style={{
                      borderColor: "var(--color-border)",
                      color: "var(--color-muted)",
                    }}
                  >
                    {p.base_url} · Key ····{p.key_hint}
                  </p>
                )}

                {/* 加模型：自动拉可用列表 + 模糊搜索 + 也能手填 */}
                {addingTo === p.id && (
                  <AddModelForm
                    providerId={p.id}
                    onDone={() => {
                      setAddingTo(null);
                      invalidate();
                    }}
                    onCancel={() => setAddingTo(null)}
                  />
                )}

                {/* 模型列表 */}
                {!isCollapsed && (
                  <ul>
                    {mine.length === 0 ? (
                      <li
                        className="px-3 py-3 text-xs"
                        style={{ color: "var(--color-muted)" }}
                      >
                        这个模型组下还没有模型。点上面的「加模型」。
                      </li>
                    ) : (
                      mine.map((m) => (
                        <li
                          key={m.id}
                          className="flex items-center gap-2 border-t px-3 py-2"
                          style={{ borderColor: "var(--color-border)" }}
                        >
                          <div className="min-w-0 flex-1">
                            <p
                              className={clsx(
                                "truncate font-mono text-xs",
                                !m.enabled && "opacity-50",
                              )}
                            >
                              {m.model_id}
                            </p>
                            <p
                              className="flex items-center gap-1.5 text-[11px]"
                              style={{ color: "var(--color-muted)" }}
                            >
                              <span>{Math.round(m.context_window / 1000)}K 上下文</span>
                              {m.supports_vision === "true" && (
                                <>
                                  <span>·</span>
                                  <span className="flex items-center gap-0.5">
                                    <Eye size={10} />
                                    看图
                                  </span>
                                </>
                              )}
                              {m.supports_tools === "true" && (
                                <>
                                  <span>·</span>
                                  <span className="flex items-center gap-0.5">
                                    <Wrench size={10} />
                                    工具
                                  </span>
                                </>
                              )}
                            </p>
                          </div>

                          {/* 启用/禁用 */}
                          <button
                            onClick={() => toggle.mutate(m)}
                            disabled={toggle.isPending}
                            aria-pressed={m.enabled}
                            aria-label={`${m.enabled ? "禁用" : "启用"} ${m.model_id}`}
                            title={
                              m.enabled
                                ? "已启用：出现在对话页的切换菜单里"
                                : "已禁用：不出现在切换菜单，但配置保留"
                            }
                            className="flex shrink-0 items-center gap-1 rounded px-2 py-1 text-xs"
                            style={{
                              background: m.enabled
                                ? "var(--color-accent)"
                                : "var(--color-surface-2)",
                              color: m.enabled ? "#fff" : "var(--color-muted)",
                            }}
                          >
                            {m.enabled ? <Eye size={11} /> : <EyeOff size={11} />}
                            {m.enabled ? "启用" : "禁用"}
                          </button>

                          <button
                            onClick={() => {
                              if (confirm(`删除模型「${m.model_id}」？\n\n同组的其它模型不受影响。`)) {
                                delModel.mutate(m.id);
                              }
                            }}
                            disabled={delModel.isPending}
                            aria-label={`删除模型 ${m.model_id}`}
                            className="shrink-0 rounded p-1"
                            style={{ color: "var(--color-err)" }}
                          >
                            <Trash2 size={13} />
                          </button>
                        </li>
                      ))
                    )}
                  </ul>
                )}
              </li>
            );
          })}
        </ul>
      )}

      <p className="text-xs" style={{ color: "var(--color-muted)" }}>
        被功能位绑定的模型删不掉 —— 会提示是哪个功能位在用，先去下面换掉。
      </p>
    </section>
  );
}
