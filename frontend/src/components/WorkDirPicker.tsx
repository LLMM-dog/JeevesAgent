import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronUp, Folder, FolderOpen, X } from "lucide-react";
import clsx from "clsx";

import { api } from "../lib/api";
import { useChatStore } from "../store/chat";
import type { BrowseEntry } from "../lib/types";

/**
 * 工作目录选择器。
 *
 * ## 为什么新会话默认没有工作目录
 *
 * 自动指向某个目录，意味着用户在不知情的情况下让 agent 能读写那里。
 * 要它干活，先告诉它在哪干 —— 这个动作本身就是授权。
 *
 * ## 为什么需要目录浏览而不是让用户输入路径
 *
 * Windows 路径又长又容易打错（反斜杠、盘符、空格），打错了得到的是
 * "目录不存在"，试两次就放弃了。所以给一个能点的浏览器，
 * 同时保留手动输入 —— 有明确目标时打路径更快。
 */
export function WorkDirPicker({
  sessionId,
  workDir,
}: {
  sessionId: string;
  workDir: string;
}) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [cur, setCur] = useState("");
  const [manual, setManual] = useState("");
  const [err, setErr] = useState("");

  // 打开时从当前工作目录开始浏览，没设过就从常用起点开始
  useEffect(() => {
    if (open) {
      setCur(workDir || "");
      setManual(workDir || "");
      setErr("");
    }
  }, [open, workDir]);

  const { data, isLoading } = useQuery({
    queryKey: ["browse", cur],
    queryFn: () => api.browse(cur || undefined),
    enabled: open,
  });

  // 【必须走 store 的 setter】。workDir 存在 zustand 里，
  // 而 invalidateQueries 只影响 react-query 的缓存 —— 两套状态互不相干。
  // 只 invalidate 的结果是库里改了但按钮上的文字还是旧的。
  const setWorkDirInStore = useChatStore((s) => s.setWorkDir);

  const save = useMutation({
    mutationFn: (dir: string) => setWorkDirInStore(dir),
    onSuccess: () => {
      // 会话详情和白名单都要刷 —— 设工作目录会顺手加一条白名单
      qc.invalidateQueries({ queryKey: ["session", sessionId] });
      qc.invalidateQueries({ queryKey: ["whitelist"] });
      setOpen(false);
      setErr("");
    },
    onError: (e: Error) => setErr(e.message),
  });

  // 折叠时直接显示当前目录，用户不用点开就知道。
  //
  // 优先保留【尾部】：路径的信息量集中在末尾几段（项目名、子目录），
  // 而开头往往是 C:\Users\某某\Documents 这类所有路径都一样的前缀。
  // 从头截断的话十个目录看起来全都一样。
  const shortDir = (() => {
    if (!workDir) return null;
    const MAX = 34;
    if (workDir.length <= MAX) return workDir;
    // 按分隔符切，从后往前塞，塞不下就停
    const parts = workDir.split(/[\\/]/).filter(Boolean);
    let out = "";
    for (let i = parts.length - 1; i >= 0; i--) {
      const next = out ? parts[i] + "\\" + out : parts[i];
      if (next.length + 1 > MAX) break;
      out = next;
    }
    // 单个目录名本身就超长时，硬截尾部
    if (!out) out = workDir.slice(-MAX);
    return "…\\" + out;
  })();

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex max-w-full items-center gap-1.5 rounded px-2 py-1 text-xs"
        style={{
          background: "var(--color-surface-2)",
          color: workDir ? "var(--color-fg)" : "var(--color-muted)",
        }}
        title={workDir || "这个对话还没设置工作目录"}
        aria-expanded={open}
      >
        {workDir ? <FolderOpen size={12} /> : <Folder size={12} />}
        <span className="truncate">
          {shortDir ? `当前：${shortDir}` : "选择工作目录"}
        </span>
        <ChevronUp
          size={12}
          className={clsx("shrink-0 transition-transform", !open && "rotate-180")}
        />
      </button>

      {open && (
        <div
          className="absolute bottom-full left-0 z-50 mb-1 flex max-h-96 w-96 flex-col rounded-lg border shadow-lg"
          style={{
            borderColor: "var(--color-border)",
            background: "var(--color-surface)",
          }}
        >
          <div
            className="flex items-center justify-between border-b px-3 py-2"
            style={{ borderColor: "var(--color-border)" }}
          >
            <span className="text-sm font-medium">这个对话的工作目录</span>
            <button onClick={() => setOpen(false)} aria-label="关闭">
              <X size={14} />
            </button>
          </div>

          {/* 手动输入。有明确目标时比点击快 */}
          <div className="flex gap-1.5 px-3 py-2">
            <input
              value={manual}
              onChange={(e) => setManual(e.target.value)}
              placeholder="直接输入路径"
              aria-label="工作目录路径"
              className="min-w-0 flex-1 rounded border px-2 py-1 text-xs"
              style={{
                borderColor: "var(--color-border)",
                background: "var(--color-bg)",
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && manual.trim()) save.mutate(manual.trim());
              }}
            />
            <button
              onClick={() => save.mutate(manual.trim())}
              disabled={!manual.trim() || save.isPending}
              className="shrink-0 rounded px-2 py-1 text-xs disabled:opacity-50"
              style={{ background: "var(--color-accent)", color: "#fff" }}
            >
              用这个
            </button>
          </div>

          {err && (
            <p
              role="alert"
              className="mx-3 mb-2 rounded px-2 py-1 text-xs"
              style={{ color: "var(--color-err)" }}
            >
              {err}
            </p>
          )}

          {/* 浏览 */}
          <div className="min-h-0 flex-1 overflow-y-auto px-1 pb-2">
            {isLoading ? (
              <p className="px-2 py-2 text-xs" style={{ color: "var(--color-muted)" }}>
                加载中…
              </p>
            ) : !cur ? (
              <ul>
                {data?.roots.map((r: BrowseEntry) => (
                  <li key={r.path}>
                    <button
                      onClick={() => setCur(r.path)}
                      className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:opacity-80"
                      style={{ background: "transparent" }}
                    >
                      <Folder size={12} style={{ color: "var(--color-accent)" }} />
                      <span className="truncate">{r.name}</span>
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <>
                <div
                  className="mb-1 truncate px-2 py-1 text-xs"
                  style={{ color: "var(--color-muted)" }}
                  title={data?.path}
                >
                  {data?.path}
                </div>
                <ul>
                  {/* 上一级。没有的话用户进到深层目录就出不来了 */}
                  <li>
                    <button
                      onClick={() => setCur(data?.parent ?? "")}
                      className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:opacity-80"
                    >
                      <span style={{ color: "var(--color-muted)" }}>..</span>
                      <span style={{ color: "var(--color-muted)" }}>上一级</span>
                    </button>
                  </li>
                  {data?.entries.map((e: BrowseEntry) => (
                    <li key={e.path} className="flex items-center gap-1">
                      <button
                        onClick={() => setCur(e.path)}
                        className="flex min-w-0 flex-1 items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:opacity-80"
                      >
                        <Folder size={12} style={{ color: "var(--color-accent)" }} />
                        <span className="truncate">{e.name}</span>
                      </button>
                      <button
                        onClick={() => save.mutate(e.path)}
                        disabled={save.isPending}
                        className="shrink-0 rounded px-1.5 py-0.5 text-xs disabled:opacity-50"
                        style={{ color: "var(--color-accent)" }}
                        aria-label={`选择 ${e.name}`}
                      >
                        选这个
                      </button>
                    </li>
                  ))}
                  {data?.entries.length === 0 && (
                    <li
                      className="px-2 py-1.5 text-xs"
                      style={{ color: "var(--color-muted)" }}
                    >
                      没有子目录
                    </li>
                  )}
                </ul>
              </>
            )}
          </div>

          <div
            className="flex items-center justify-between border-t px-3 py-2"
            style={{ borderColor: "var(--color-border)" }}
          >
            <button
              onClick={() => cur && save.mutate(cur)}
              disabled={!cur || save.isPending}
              className="rounded px-2 py-1 text-xs disabled:opacity-50"
              style={{ background: "var(--color-accent)", color: "#fff" }}
            >
              用当前目录
            </button>
            {workDir && (
              <button
                onClick={() => save.mutate("")}
                disabled={save.isPending}
                className="text-xs"
                style={{ color: "var(--color-err)" }}
              >
                清除
              </button>
            )}
          </div>

          <p
            className="border-t px-3 py-2 text-xs"
            style={{ borderColor: "var(--color-border)", color: "var(--color-muted)" }}
          >
            选定后会自动给这个目录读写权限，只对当前对话生效。
          </p>
        </div>
      )}
    </div>
  );
}
