import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronUp, Cpu } from "lucide-react";
import clsx from "clsx";

import { api } from "../lib/api";
import { useChatStore } from "../store/chat";
import type { ModelItem } from "../lib/types";

/**
 * 对话页的模型快捷切换。
 *
 * ## 为什么切换只影响这个对话
 *
 * 改功能位绑定是全局的。用户在一个对话里换成便宜模型，不该让所有
 * 对话都跟着换 —— 那种"我只想这次省点钱"的意图很常见。
 *
 * 所以存在 session.model_pk 上，空串表示跟随全局绑定。
 *
 * ## 为什么只列已启用的
 *
 * 配过但暂时不用的模型会把菜单堆满。禁用是"别在菜单里出现，
 * 但别让我重新配"—— 删除才是真的移除。
 */
export function ModelSwitcher({
  sessionId,
  modelPk,
}: {
  sessionId: string;
  modelPk: string;
}) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [err, setErr] = useState("");

  const { data } = useQuery({
    queryKey: ["models", "enabled"],
    queryFn: () => api.models({ enabledOnly: true }),
    enabled: open,
  });

  // 绑定信息用来显示"跟随默认"具体是哪个模型 ——
  // 只写"默认"的话用户不知道那是什么
  const { data: bindings } = useQuery({
    queryKey: ["bindings"],
    queryFn: api.listBindings,
    enabled: open,
  });

  // 【必须走 store 的 setter】。
  //
  // modelPk 存在 zustand 里，而 invalidateQueries 只影响 react-query
  // 的缓存 —— 两套状态互不相干。之前只 invalidate 的结果是：
  // 请求发出去了、库里也改了，但按钮上的文字还是旧的，
  // 要刷新页面才对。用户看到的就是"切了没反应"。
  const setWorkModel = useChatStore((s) => s.setWorkModel);

  const pick = useMutation({
    mutationFn: (pk: string) => setWorkModel(pk),
    onSuccess: () => {
      // 会话详情的缓存也刷一下 —— 别处可能读它
      qc.invalidateQueries({ queryKey: ["session", sessionId] });
      setOpen(false);
    },
    onError: (e: Error) => setErr(e.message),
  });

  const items = data?.items ?? [];
  const current = items.find((m) => m.id === modelPk);
  const chatBinding = bindings?.items.find((b) => b.purpose === "chat");

  // 折叠时的标签。没选过就显示"默认模型"
  const label = current
    ? current.display_name || current.model_id
    : "默认模型";

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex max-w-48 items-center gap-1.5 rounded px-2 py-1 text-xs"
        style={{
          background: "var(--color-surface-2)",
          color: modelPk ? "var(--color-fg)" : "var(--color-muted)",
        }}
        title={
          current
            ? `${current.provider_name} / ${current.model_id}`
            : "这个对话用功能位绑定的默认模型"
        }
        aria-expanded={open}
      >
        <Cpu size={12} />
        <span className="truncate">{label}</span>
        <ChevronUp
          size={12}
          className={clsx("shrink-0 transition-transform", !open && "rotate-180")}
        />
      </button>

      {open && (
        <div
          className="absolute left-0 z-50 mt-1 max-h-80 w-72 overflow-y-auto rounded-lg border shadow-lg"
          style={{
            borderColor: "var(--color-border)",
            background: "var(--color-surface)",
          }}
          role="listbox"
          aria-label="选择模型"
        >
          {/* 跟随默认 */}
          <button
            onClick={() => pick.mutate("")}
            role="option"
            aria-selected={!modelPk}
            className="flex w-full items-start gap-2 border-b px-3 py-2 text-left text-xs hover:opacity-80"
            style={{ borderColor: "var(--color-border)" }}
          >
            <div className="w-3.5 shrink-0 pt-0.5">
              {!modelPk && <Check size={12} style={{ color: "var(--color-accent)" }} />}
            </div>
            <div className="min-w-0">
              <div>跟随默认</div>
              <div className="truncate" style={{ color: "var(--color-muted)" }}>
                {chatBinding
                  ? `现在是 ${chatBinding.model_id ?? ""}`
                  : "在设置页的功能位绑定里配置"}
              </div>
            </div>
          </button>

          {err && (
            <p
              role="alert"
              className="border-b px-3 py-2 text-xs"
              style={{ borderColor: "var(--color-border)", color: "var(--color-err)" }}
            >
              {err}
            </p>
          )}

          {items.length === 0 ? (
            <p className="px-3 py-3 text-xs" style={{ color: "var(--color-muted)" }}>
              没有已启用的模型。去设置页添加，或把禁用的启用。
            </p>
          ) : (
            items.map((m: ModelItem) => (
              <button
                key={m.id}
                onClick={() => pick.mutate(m.id)}
                role="option"
                aria-selected={m.id === modelPk}
                disabled={pick.isPending}
                className="flex w-full items-start gap-2 px-3 py-2 text-left text-xs hover:opacity-80 disabled:opacity-50"
              >
                <div className="w-3.5 shrink-0 pt-0.5">
                  {m.id === modelPk && (
                    <Check size={12} style={{ color: "var(--color-accent)" }} />
                  )}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="truncate">{m.display_name || m.model_id}</div>
                  <div
                    className="flex items-center gap-1.5 truncate"
                    style={{ color: "var(--color-muted)" }}
                  >
                    <span>{m.provider_name}</span>
                    <span>·</span>
                    <span>{Math.round(m.context_window / 1000)}K</span>
                    {m.supports_vision === "true" && <span>· 看图</span>}
                  </div>
                </div>
              </button>
            ))
          )}

          <p
            className="border-t px-3 py-2 text-xs"
            style={{
              borderColor: "var(--color-border)",
              color: "var(--color-muted)",
            }}
          >
            只影响这个对话。标题生成和上下文压缩仍用各自绑定的模型。
          </p>
        </div>
      )}
    </div>
  );
}
