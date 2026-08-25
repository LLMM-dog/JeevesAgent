/**
 * cpolar 公网隧道（主推方案）：大陆服务器，访问方免装直接浏览器打开。
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, KeyRound, Loader2, RefreshCw, StopCircle } from "lucide-react";
import clsx from "clsx";
import { api } from "@/lib/api";
import { ApiError } from "@/lib/sse";

const inputCls =
  "w-full rounded-lg border px-3 py-2 text-sm outline-none focus:border-[var(--color-accent)]";
const inputStyle = { borderColor: "var(--color-border)", background: "var(--color-bg)" } as const;

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.message : e instanceof Error ? e.message : "操作失败";
}

export default function CpolarSection() {
  const qc = useQueryClient();
  const [token, setToken] = useState("");
  const [port, setPort] = useState("9000");
  const [url, setUrl] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const { data, refetch, isFetching, isLoading } = useQuery({
    queryKey: ["deploy-cpolar"],
    queryFn: api.cpolarStatus,
    staleTime: 3_000,
    refetchInterval: 5_000,
  });

  const install = useMutation({
    mutationFn: () => api.cpolarInstall(),
    onSuccess: (r) => {
      setMsg({ ok: r.ok, text: r.detail });
      void qc.invalidateQueries({ queryKey: ["deploy-cpolar"] });
    },
    onError: (e) => setMsg({ ok: false, text: errMsg(e) }),
  });

  const saveToken = useMutation({
    mutationFn: () => api.cpolarAuthtoken(token.trim()),
    onSuccess: (r) => {
      setMsg({ ok: r.ok, text: r.ok ? "token 已保存" : r.detail });
      void qc.invalidateQueries({ queryKey: ["deploy-cpolar"] });
    },
    onError: (e) => setMsg({ ok: false, text: errMsg(e) }),
  });

  const start = useMutation({
    mutationFn: () => api.cpolarStart(Number(port) || 9000),
    onSuccess: (r) => {
      if (r.ok && r.detail.startsWith("http")) {
        setUrl(r.detail);
        setMsg({ ok: true, text: "公网隧道已开启" });
      } else {
        setMsg({ ok: r.ok, text: r.detail || "开启失败" });
      }
      void qc.invalidateQueries({ queryKey: ["deploy-cpolar"] });
    },
    onError: (e) => setMsg({ ok: false, text: errMsg(e) }),
  });

  const stop = useMutation({
    mutationFn: () => api.cpolarStop(),
    onSuccess: () => {
      setUrl("");
      setMsg({ ok: true, text: "隧道已停止" });
      void qc.invalidateQueries({ queryKey: ["deploy-cpolar"] });
    },
    onError: (e) => setMsg({ ok: false, text: errMsg(e) }),
  });

  return (
    <section className="rounded-xl border p-4" style={{ borderColor: "var(--color-border)", background: "var(--color-surface)" }}>
      <h2 className="mb-1 flex items-center gap-2 text-sm font-medium" style={{ color: "var(--color-text)" }}>
        <ExternalLink size={15} aria-hidden />公网访问（推荐，cpolar）
      </h2>
      <p className="mb-3 text-xs text-[var(--color-muted)]">
        国内服务器、速度快。其他设备<b>免装任何东西</b>，浏览器直接打开公网链接即可使用。
      </p>

      {isLoading || data === undefined ? (
        <div className="flex items-center gap-2 text-sm text-[var(--color-muted)]">
          <Loader2 size={14} className="animate-spin" aria-hidden />正在检测 cpolar…
        </div>
      ) : !data.installed ? (
        <div className="space-y-2 text-sm">
          <button type="button" disabled={install.isPending} onClick={() => install.mutate()} className="flex items-center gap-1.5 rounded-lg bg-[var(--color-accent)] px-3 py-1.5 text-xs font-medium text-white transition hover:brightness-110 disabled:opacity-50">
            {install.isPending ? <Loader2 size={13} className="animate-spin" /> : <ExternalLink size={13} />}
            {install.isPending ? "下载中…" : "一键安装 cpolar"}
          </button>
          <p className="text-xs text-[var(--color-muted)]">下载到项目 .cpolar/ 目录，随项目走、删除即干净。</p>
        </div>
      ) : (data.running || url) && (data.url || url) ? (
        <div className="space-y-3">
          <div className="rounded-lg border p-3" style={{ borderColor: "var(--color-accent)" }}>
            <p className="mb-2 text-xs text-[var(--color-muted)]">公网访问地址（任何设备浏览器打开）：</p>
            <a className="break-all text-sm font-medium text-[var(--color-accent)] underline" href={data.url || url} target="_blank" rel="noreferrer">{data.url || url}</a>
          </div>
          <div className="flex items-center gap-2">
            <button type="button" disabled={stop.isPending} onClick={() => stop.mutate()} className="flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs transition hover:bg-[var(--color-surface-2)] disabled:opacity-50" style={{ borderColor: "var(--color-border)", color: "var(--color-muted)" }}>
              <StopCircle size={13} aria-hidden />停止隧道
            </button>
            <button type="button" disabled={isFetching} onClick={() => void refetch()} className="flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs transition hover:bg-[var(--color-surface-2)] disabled:opacity-50" style={{ borderColor: "var(--color-border)", color: "var(--color-muted)" }}>
              <RefreshCw size={12} className={isFetching ? "animate-spin" : ""} aria-hidden />刷新
            </button>
          </div>
        </div>
      ) : (
        <div className="space-y-2">
          {data.authtoken_configured ? (
            <p className="text-xs text-[var(--color-accent)]">✓ token 已保存（长期有效，无需重复输入）</p>
          ) : (
            <>
              <label className="block">
                <span className="mb-1 block text-xs text-[var(--color-muted)]">cpolar authtoken（去 cpolar.com 注册后，控制台里复制，只需填一次）</span>
                <input className={inputCls} style={inputStyle} value={token} onChange={(e) => setToken(e.target.value)} placeholder="粘贴你的 authtoken" />
              </label>
              <button type="button" disabled={saveToken.isPending || token.trim().length < 8} onClick={() => saveToken.mutate()} className="flex items-center gap-1.5 rounded-lg bg-[var(--color-accent)] px-3 py-1.5 text-xs font-medium text-white transition hover:brightness-110 disabled:opacity-50">
                {saveToken.isPending ? <Loader2 size={13} className="animate-spin" /> : <KeyRound size={13} />}保存 token
              </button>
            </>
          )}
          <div className="flex items-center gap-2 pt-1">
            <span className="text-xs text-[var(--color-muted)]">后端端口</span>
            <input className="w-20 rounded-md border px-2 py-1 text-xs outline-none" style={inputStyle} value={port} onChange={(e) => setPort(e.target.value)} />
            <button type="button" disabled={start.isPending} onClick={() => start.mutate()} className="flex items-center gap-1.5 rounded-lg bg-[var(--color-accent)] px-3 py-1.5 text-xs font-medium text-white transition hover:brightness-110 disabled:opacity-50">
              {start.isPending ? <Loader2 size={13} className="animate-spin" /> : <ExternalLink size={13} />}开启公网隧道
            </button>
          </div>
        </div>
      )}

      {msg && (
        <p className={clsx("mt-2 break-all text-xs", msg.ok ? "text-[var(--color-accent)]" : "text-[var(--color-err)]")}>{msg.text}</p>
      )}
    </section>
  );
}
