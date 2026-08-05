import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Lock, Plus, Trash2 } from "lucide-react";
import clsx from "clsx";

import { api } from "../lib/api";
import type { WhitelistItem } from "../lib/types";

/**
 * 路径白名单管理。
 *
 * ## 为什么这个面板必须存在
 *
 * 文件工具受路径白名单约束，但之前没有任何界面能看到里面有什么 ——
 * 用户遇到"路径不在白名单内"时，既不知道当前允许了哪些目录，
 * 也没有地方去加。只能去改数据库。
 *
 * ## 这里显示的是全局条目
 *
 * 会话级条目（为某个对话授权的目录）在对话页的工作目录选择器里管理，
 * 因为那才是它们产生的地方。这里混进会话级条目会让人以为
 * 改动影响所有对话。
 */
export default function WhitelistPanel() {
  const qc = useQueryClient();
  const [path, setPath] = useState("");
  const [canWrite, setCanWrite] = useState(false);
  const [err, setErr] = useState("");

  const { data, isLoading } = useQuery({
    queryKey: ["whitelist"],
    queryFn: () => api.whitelist(),
  });

  const add = useMutation({
    mutationFn: () => api.addWhitelist({ path: path.trim(), can_write: canWrite }),
    onSuccess: () => {
      setPath("");
      setCanWrite(false);
      setErr("");
      qc.invalidateQueries({ queryKey: ["whitelist"] });
    },
    onError: (e: Error) => setErr(e.message),
  });

  const toggleWrite = useMutation({
    mutationFn: (it: WhitelistItem) =>
      api.patchWhitelist(it.id, { can_write: !it.can_write }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["whitelist"] }),
    onError: (e: Error) => setErr(e.message),
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteWhitelist(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["whitelist"] }),
    onError: (e: Error) => setErr(e.message),
  });

  const items = data?.items ?? [];

  return (
    <section className="space-y-4">
      <header>
        <h2 className="text-lg font-medium">文件访问</h2>
        <p className="mt-1 text-sm" style={{ color: "var(--color-muted)" }}>
          agent 的文件工具只能读写这里列出的目录。
          <br />
          <span style={{ color: "var(--color-warn)" }}>
            注意：run_shell 执行命令不受这个白名单约束
          </span>
          —— 它能做的事没有上界，所以默认需要你逐条确认。
        </p>
      </header>

      {/* 添加 */}
      <div
        className="rounded-lg border p-3"
        style={{
          borderColor: "var(--color-border)",
          background: "var(--color-surface)",
        }}
      >
        <div className="flex flex-wrap items-center gap-2">
          <input
            value={path}
            onChange={(e) => setPath(e.target.value)}
            placeholder="绝对路径，如 D:\\proj\\my-app"
            aria-label="要添加的目录路径"
            className="min-w-0 flex-1 rounded border px-2 py-1.5 text-sm"
            style={{
              borderColor: "var(--color-border)",
              background: "var(--color-bg)",
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && path.trim()) add.mutate();
            }}
          />
          <label className="flex items-center gap-1.5 text-sm">
            <input
              type="checkbox"
              checked={canWrite}
              onChange={(e) => setCanWrite(e.target.checked)}
            />
            允许写入
          </label>
          <button
            onClick={() => add.mutate()}
            disabled={!path.trim() || add.isPending}
            className="flex items-center gap-1 rounded px-3 py-1.5 text-sm disabled:opacity-50"
            style={{ background: "var(--color-accent)", color: "#fff" }}
          >
            <Plus size={14} />
            添加
          </button>
        </div>
        <p className="mt-2 text-xs" style={{ color: "var(--color-muted)" }}>
          默认只读。给写权限前想清楚：agent 可以覆盖这个目录下的任何文件。
        </p>
      </div>

      {err && (
        <p
          role="alert"
          className="rounded border px-3 py-2 text-sm"
          style={{ borderColor: "var(--color-err)", color: "var(--color-err)" }}
        >
          {err}
        </p>
      )}

      {/* 列表 */}
      {isLoading ? (
        <p className="text-sm" style={{ color: "var(--color-muted)" }}>
          加载中…
        </p>
      ) : items.length === 0 ? (
        <p className="text-sm" style={{ color: "var(--color-muted)" }}>
          还没有任何条目。agent 现在不能读写任何文件。
        </p>
      ) : (
        <ul className="space-y-2">
          {items.map((it) => (
            <li
              key={it.id}
              className="flex items-center gap-3 rounded-lg border p-3"
              style={{
                borderColor: "var(--color-border)",
                background: "var(--color-surface)",
              }}
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <code
                    className={clsx("truncate text-sm", !it.exists && "line-through")}
                    title={it.path}
                  >
                    {it.path}
                  </code>
                  {it.builtin && (
                    <span
                      className="flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-xs"
                      style={{
                        background: "var(--color-surface-2)",
                        color: "var(--color-muted)",
                      }}
                    >
                      <Lock size={10} />
                      内置
                    </span>
                  )}
                </div>
                {it.note && (
                  <p className="mt-0.5 text-xs" style={{ color: "var(--color-muted)" }}>
                    {it.note}
                  </p>
                )}
                {/* 目录不存在时必须显式提示。
                    否则工具报"不在白名单内"，方向完全错。 */}
                {!it.exists && (
                  <p
                    className="mt-0.5 flex items-center gap-1 text-xs"
                    style={{ color: "var(--color-warn)" }}
                  >
                    <AlertTriangle size={11} />
                    这个目录不存在了
                  </p>
                )}
              </div>

              <button
                onClick={() => toggleWrite.mutate(it)}
                disabled={it.builtin || toggleWrite.isPending}
                aria-label={`切换 ${it.path} 的写权限`}
                className="shrink-0 rounded px-2 py-1 text-xs disabled:cursor-not-allowed disabled:opacity-40"
                style={{
                  background: it.can_write
                    ? "var(--color-warn)"
                    : "var(--color-surface-2)",
                  color: it.can_write ? "#000" : "var(--color-muted)",
                }}
                title={it.builtin ? "内置条目权限不可改" : undefined}
              >
                {it.can_write ? "可读写" : "只读"}
              </button>

              <button
                onClick={() => remove.mutate(it.id)}
                disabled={it.builtin || remove.isPending}
                aria-label={`删除 ${it.path}`}
                className="shrink-0 rounded p-1.5 disabled:cursor-not-allowed disabled:opacity-40"
                style={{ color: "var(--color-err)" }}
                title={it.builtin ? "内置条目不可删除" : undefined}
              >
                <Trash2 size={14} />
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
