import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RotateCcw, Save } from "lucide-react";
import clsx from "clsx";

import { api } from "../lib/api";
import type { PersonaFile } from "../lib/types";

/**
 * 人格与个人偏好。
 *
 * ## 为什么要在界面里编辑
 *
 * SOUL.md（性格）和 USER.md（关于你）直接进系统提示词，是影响输出
 * 最明显的两个文件。但它们躺在 personas/ 目录下 —— 用户得先知道有
 * 这两个文件，再用编辑器打开。绝大多数人不会去找。
 *
 * ## 存盘即生效
 *
 * prompts.py 的 _read 没有缓存。加缓存的话改完要重启才生效，
 * 而用户会以为"改了没用"。
 */
export default function PersonaPanel() {
  const qc = useQueryClient();
  const [active, setActive] = useState("soul");
  const [draft, setDraft] = useState("");
  const [dirty, setDirty] = useState(false);
  const [msg, setMsg] = useState("");

  const { data, isLoading } = useQuery({
    queryKey: ["personas"],
    queryFn: api.personas,
  });

  const items: PersonaFile[] = data?.items ?? [];
  const cur = items.find((x) => x.key === active);

  // 切换文件时载入内容。用 filename 做依赖而不是 content ——
  // content 变化会在保存后触发重载，把用户正在编辑的内容冲掉
  useEffect(() => {
    if (cur) {
      setDraft(cur.content);
      setDirty(false);
      setMsg("");
    }
  }, [cur?.filename, cur]);

  const save = useMutation({
    mutationFn: () => api.savePersona(active, draft),
    onSuccess: () => {
      setDirty(false);
      setMsg("已保存，下一轮对话生效");
      qc.invalidateQueries({ queryKey: ["personas"] });
    },
    onError: (e: Error) => setMsg(e.message),
  });

  const reset = useMutation({
    mutationFn: () => api.resetPersona(active),
    onSuccess: (f) => {
      setDraft(f.content);
      setDirty(false);
      setMsg("已恢复示例内容");
      qc.invalidateQueries({ queryKey: ["personas"] });
    },
    onError: (e: Error) => setMsg(e.message),
  });

  return (
    <section className="space-y-4">
      <header>
        <h2 className="text-lg font-medium">人格与偏好</h2>
        <p className="mt-1 text-sm" style={{ color: "var(--color-muted)" }}>
          这三个文件每轮对话都会进系统提示词。改完存盘就生效，不用重启。
        </p>
      </header>

      {/* 文件选择 */}
      <div
        className="flex gap-1 overflow-x-auto border-b"
        style={{ borderColor: "var(--color-border)" }}
        role="tablist"
        aria-label="人格文件"
      >
        {items.map((f) => (
          <button
            key={f.key}
            role="tab"
            aria-selected={f.key === active}
            onClick={() => {
              if (dirty && !confirm("有未保存的修改，切换会丢掉。继续？")) return;
              setActive(f.key);
            }}
            className={clsx(
              "shrink-0 border-b-2 px-3 py-2 text-sm",
              f.key === active ? "font-medium" : "opacity-60",
            )}
            style={{
              borderColor: f.key === active ? "var(--color-accent)" : "transparent",
            }}
          >
            {f.label}
            <span className="ml-1.5 text-xs" style={{ color: "var(--color-muted)" }}>
              {f.filename}
            </span>
          </button>
        ))}
      </div>

      {isLoading ? (
        <p className="text-sm" style={{ color: "var(--color-muted)" }}>
          加载中…
        </p>
      ) : (
        <>
          {cur && (
            <p className="text-sm" style={{ color: "var(--color-muted)" }}>
              {cur.hint}
            </p>
          )}

          <textarea
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value);
              setDirty(true);
              setMsg("");
            }}
            spellCheck={false}
            aria-label={cur ? `编辑 ${cur.filename}` : "编辑人格文件"}
            className="h-80 w-full resize-y rounded-lg border p-3 font-mono text-xs leading-relaxed"
            style={{
              borderColor: "var(--color-border)",
              background: "var(--color-bg)",
            }}
          />

          <div className="flex items-center gap-2">
            <button
              onClick={() => save.mutate()}
              disabled={!dirty || save.isPending}
              className="flex items-center gap-1.5 rounded px-3 py-1.5 text-sm disabled:opacity-50"
              style={{ background: "var(--color-accent)", color: "#fff" }}
            >
              <Save size={14} />
              保存
            </button>
            <button
              onClick={() => {
                if (confirm("用示例内容覆盖当前内容？")) reset.mutate();
              }}
              disabled={reset.isPending}
              className="flex items-center gap-1.5 rounded px-3 py-1.5 text-sm disabled:opacity-50"
              style={{
                background: "var(--color-surface-2)",
                color: "var(--color-muted)",
              }}
              title="从 .example.md 恢复"
            >
              <RotateCcw size={14} />
              恢复示例
            </button>
            {dirty && (
              <span className="text-xs" style={{ color: "var(--color-warn)" }}>
                有未保存的修改
              </span>
            )}
            {msg && (
              <span
                role="status"
                className="text-xs"
                style={{
                  color: msg.startsWith("已") ? "var(--color-ok)" : "var(--color-err)",
                }}
              >
                {msg}
              </span>
            )}
          </div>

          <p className="text-xs" style={{ color: "var(--color-muted)" }}>
            写得越长，每轮占的上下文越多。行为规则（AGENTS.md）改坏了会明显影响可用性，
            拿不准就先用「恢复示例」看原来是什么。
          </p>
        </>
      )}
    </section>
  );
}
