/**
 * 设置页「部署」tab：远程访问配置、账户安全、Tailscale 隧道（自动安装/登录）。
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  ExternalLink,
  KeyRound,
  Loader2,
  RefreshCw,
  Server,
  ShieldCheck,
  Trash2,
  UserPlus,
} from "lucide-react";
import clsx from "clsx";
import { api } from "@/lib/api";
import type { TailscaleStatus, UserItem } from "@/lib/types";
import CpolarSection from "./CpolarSection";
import { useAuth } from "@/store/auth";
import { ApiError } from "@/lib/sse";

const inputCls =
  "w-full rounded-lg border px-3 py-2 text-sm outline-none focus:border-[var(--color-accent)]";
const inputStyle = {
  borderColor: "var(--color-border)",
  background: "var(--color-bg)",
} as const;

function Card({ title, icon, children }: { title: string; icon?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="rounded-xl border p-4" style={{ borderColor: "var(--color-border)", background: "var(--color-surface)" }}>
      <h2 className="mb-3 flex items-center gap-2 text-sm font-medium" style={{ color: "var(--color-text)" }}>
        {icon}{title}
      </h2>
      {children}
    </section>
  );
}

function Badge({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs"
      style={{
        borderColor: ok ? "var(--color-accent)" : "var(--color-warn)",
        color: ok ? "var(--color-text)" : "var(--color-warn)",
      }}
    >
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: ok ? "var(--color-accent)" : "var(--color-warn)" }} />
      {label}
    </span>
  );
}

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.message : e instanceof Error ? e.message : "操作失败";
}

/** 把文本里的 http(s) 链接渲染成可点击链接，其余保持文本 */
function Linkify({ text }: { text: string }) {
  const parts = text.split(/(https?:\/\/[^\s]+)/g);
  return (
    <>
      {parts.map((p, i) =>
        /^https?:\/\//.test(p) ? (
          <a key={i} className="underline" href={p} target="_blank" rel="noreferrer">{p}</a>
        ) : (
          <span key={i}>{p}</span>
        ),
      )}
    </>
  );
}

// ─────────────────────────── 远程访问配置 ───────────────────────────

function DeployConfigSection() {
  const qc = useQueryClient();
  const authEnabled = useAuth((s) => s.authEnabled);

  const { data: deploy } = useQuery({
    queryKey: ["deploy-status"],
    queryFn: api.deployStatus,
    staleTime: 15_000,
  });
  const settings = useQuery({
    queryKey: ["deploy-settings"],
    queryFn: api.deploySettings,
  });

  // 表单值：host / port（从设置项初始化）
  const hostItem = settings.data?.items.find((i) => i.key === "app.host");
  const portItem = settings.data?.items.find((i) => i.key === "app.port");
  const [host, setHost] = useState(String(hostItem?.value ?? "127.0.0.1"));
  const [port, setPort] = useState(String(portItem?.value ?? "9000"));
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  // 开启鉴权向导（无用户时）
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");

  const saveNetwork = useMutation({
    mutationFn: () =>
      api.updateDeploySettings({ "app.host": host.trim(), "app.port": Number(port) }),
    onSuccess: () => {
      setMsg({ ok: true, text: "已保存。绑定地址/端口改动需要重启服务后生效。" });
      void qc.invalidateQueries({ queryKey: ["deploy-settings"] });
    },
    onError: (e) => setMsg({ ok: false, text: errMsg(e) }),
  });

  const enableAuth = useMutation({
    mutationFn: () => api.enableAuth(username.trim(), password),
    onSuccess: () => {
      setMsg({ ok: true, text: "鉴权已开启，已用该账号登录。" });
      void qc.invalidateQueries({ queryKey: ["deploy-settings"] });
      void qc.invalidateQueries({ queryKey: ["deploy-status"] });
      // 刷新 auth 状态（登录 cookie 已由后端下发）
      useAuth.getState().check();
    },
    onError: (e) => setMsg({ ok: false, text: errMsg(e) }),
  });

  return (
    <Card title="远程访问" icon={<Server size={15} aria-hidden />}>
      <div className="mb-3 grid gap-1.5 text-sm">
        <Row k="绑定地址" v={`${deploy?.host ?? "?"}:${deploy?.port ?? "?"}`} />
        <Row k="访问范围" v={deploy?.is_localhost ? "仅本机" : "网络可访问"} />
        <Row k="鉴权" v={authEnabled ? "已开启" : "未开启"} />
        <Row k="传输" v={deploy?.https ? "HTTPS" : "HTTP"} />
      </div>

      {!authEnabled ? (
        <div className="rounded-lg border p-3" style={{ borderColor: "var(--color-border)" }}>
          <p className="mb-2 text-sm" style={{ color: "var(--color-text)" }}>
            开启登录鉴权（远程访问前必须）
          </p>
          <div className="grid gap-2 md:grid-cols-2">
            <input className={inputCls} style={inputStyle} placeholder="管理员用户名" value={username} onChange={(e) => setUsername(e.target.value)} />
            <input className={inputCls} style={inputStyle} type="password" placeholder="管理员密码（至少 8 位）" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
          </div>
          <button
            type="button"
            disabled={enableAuth.isPending || !username.trim() || password.length < 8}
            onClick={() => enableAuth.mutate()}
            className="mt-2 flex items-center gap-1.5 rounded-lg bg-[var(--color-accent)] px-3 py-1.5 text-xs font-medium text-white transition hover:brightness-110 disabled:opacity-50"
          >
            {enableAuth.isPending ? <Loader2 size={13} className="animate-spin" /> : <ShieldCheck size={13} />}
            开启鉴权并创建管理员
          </button>
          <p className="mt-2 text-xs text-[var(--color-muted)]">
            一键完成：创建管理员账号 + 打开鉴权 + 自动登录。之后随时可在这里改密码、加用户。
          </p>
        </div>
      ) : (
        <div className="rounded-lg border p-3" style={{ borderColor: "var(--color-border)" }}>
          <p className="mb-2 text-sm" style={{ color: "var(--color-text)" }}>
            绑定地址与端口（改完需重启服务）
          </p>
          <div className="grid gap-2 md:grid-cols-2">
            <label className="block">
              <span className="mb-1 block text-xs text-[var(--color-muted)]">绑定地址</span>
              <input className={inputCls} style={inputStyle} value={host} onChange={(e) => setHost(e.target.value)} placeholder="127.0.0.1" />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs text-[var(--color-muted)]">端口</span>
              <input className={inputCls} style={inputStyle} value={port} onChange={(e) => setPort(e.target.value)} placeholder="9000" />
            </label>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={saveNetwork.isPending}
              onClick={() => saveNetwork.mutate()}
              className="flex items-center gap-1.5 rounded-lg bg-[var(--color-accent)] px-3 py-1.5 text-xs font-medium text-white transition hover:brightness-110 disabled:opacity-50"
            >
              {saveNetwork.isPending ? <Loader2 size={13} className="animate-spin" /> : <Check size={13} />}
              保存
            </button>
            <span className="text-xs text-[var(--color-muted)]">
              改成 0.0.0.0 前请确认已开启鉴权；未开鉴权绑定非本机会被拒绝启动。
            </span>
          </div>
        </div>
      )}

      {msg && (
        <p className={clsx("mt-2 text-xs", msg.ok ? "text-[var(--color-accent)]" : "text-[var(--color-err)]")}>{msg.text}</p>
      )}
    </Card>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex items-center justify-between gap-4">
      <span className="text-[var(--color-muted)]">{k}</span>
      <span className="text-right" style={{ color: "var(--color-text)" }}>{v}</span>
    </div>
  );
}

// ─────────────────────────── 账户与安全 ───────────────────────────

function AccountSection() {
  const qc = useQueryClient();
  const authEnabled = useAuth((s) => s.authEnabled);
  const isAdmin = useAuth((s) => s.isAdmin);
  const username = useAuth((s) => s.username);

  const [oldPw, setOldPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirmPw, setConfirmPw] = useState("");
  const [pwMsg, setPwMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const [newUser, setNewUser] = useState({ username: "", password: "", is_admin: false });
  const [userMsg, setUserMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [resetPw, setResetPw] = useState<Record<string, string>>({});

  const users = useQuery({
    queryKey: ["auth-users"],
    queryFn: api.listUsers,
    enabled: authEnabled && isAdmin,
  });

  const changePw = useMutation({
    mutationFn: () => api.changePassword(oldPw, newPw),
    onSuccess: () => {
      setPwMsg({ ok: true, text: "密码已修改" });
      setOldPw(""); setNewPw(""); setConfirmPw("");
    },
    onError: (e) => setPwMsg({ ok: false, text: errMsg(e) }),
  });

  const createUser = useMutation({
    mutationFn: () => api.createUser(newUser),
    onSuccess: () => {
      setNewUser({ username: "", password: "", is_admin: false });
      setUserMsg({ ok: true, text: "用户已创建" });
      void qc.invalidateQueries({ queryKey: ["auth-users"] });
    },
    onError: (e) => setUserMsg({ ok: false, text: errMsg(e) }),
  });

  const patchUser = useMutation({
    mutationFn: ({ id, body }: { id: string; body: { enabled?: boolean; password?: string } }) =>
      api.patchUser(id, body),
    onSuccess: () => {
      setResetPw({});
      void qc.invalidateQueries({ queryKey: ["auth-users"] });
    },
    onError: (e) => setUserMsg({ ok: false, text: errMsg(e) }),
  });

  const delUser = useMutation({
    mutationFn: (id: string) => api.deleteUser(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["auth-users"] }),
    onError: (e) => setUserMsg({ ok: false, text: errMsg(e) }),
  });

  if (!authEnabled) {
    return (
      <Card title="账户与安全" icon={<ShieldCheck size={15} aria-hidden />}>
        <p className="text-sm text-[var(--color-muted)]">
          鉴权未开启。开启后即可在这里管理密码与用户。
        </p>
      </Card>
    );
  }

  return (
    <Card title="账户与安全" icon={<ShieldCheck size={15} aria-hidden />}>
      <div className="grid gap-3 md:grid-cols-2">
        <div className="space-y-2">
          <h3 className="text-xs font-medium text-[var(--color-muted)]">修改密码（当前用户：{username}）</h3>
          <input className={inputCls} style={inputStyle} type="password" placeholder="原密码" value={oldPw} onChange={(e) => setOldPw(e.target.value)} autoComplete="current-password" />
          <input className={inputCls} style={inputStyle} type="password" placeholder="新密码（至少 8 位）" value={newPw} onChange={(e) => setNewPw(e.target.value)} autoComplete="new-password" />
          <input className={inputCls} style={inputStyle} type="password" placeholder="确认新密码" value={confirmPw} onChange={(e) => setConfirmPw(e.target.value)} autoComplete="new-password" />
          <button
            type="button"
            disabled={changePw.isPending || !oldPw || newPw.length < 8 || newPw !== confirmPw}
            onClick={() => changePw.mutate()}
            className="flex items-center gap-1.5 rounded-lg bg-[var(--color-accent)] px-3 py-1.5 text-xs font-medium text-white transition hover:brightness-110 disabled:opacity-50"
          >
            {changePw.isPending ? <Loader2 size={13} className="animate-spin" /> : <KeyRound size={13} />}
            修改密码
          </button>
          {pwMsg && <p className={clsx("text-xs", pwMsg.ok ? "text-[var(--color-accent)]" : "text-[var(--color-err)]")}>{pwMsg.text}</p>}
        </div>

        {isAdmin && (
          <div className="space-y-2">
            <h3 className="text-xs font-medium text-[var(--color-muted)]">新建用户</h3>
            <input className={inputCls} style={inputStyle} placeholder="用户名" value={newUser.username} onChange={(e) => setNewUser({ ...newUser, username: e.target.value })} />
            <input className={inputCls} style={inputStyle} type="password" placeholder="初始密码（至少 8 位）" value={newUser.password} onChange={(e) => setNewUser({ ...newUser, password: e.target.value })} />
            <label className="flex items-center gap-2 text-xs text-[var(--color-muted)]">
              <input type="checkbox" className="accent-[var(--color-accent)]" checked={newUser.is_admin} onChange={(e) => setNewUser({ ...newUser, is_admin: e.target.checked })} />
              设为管理员
            </label>
            <button
              type="button"
              disabled={createUser.isPending || !newUser.username || newUser.password.length < 8}
              onClick={() => createUser.mutate()}
              className="flex items-center gap-1.5 rounded-lg bg-[var(--color-accent)] px-3 py-1.5 text-xs font-medium text-white transition hover:brightness-110 disabled:opacity-50"
            >
              {createUser.isPending ? <Loader2 size={13} className="animate-spin" /> : <UserPlus size={13} />}
              创建
            </button>
            {userMsg && <p className={clsx("text-xs", userMsg.ok ? "text-[var(--color-accent)]" : "text-[var(--color-err)]")}>{userMsg.text}</p>}
          </div>
        )}
      </div>

      {isAdmin && users.data && users.data.length > 0 && (
        <div className="mt-4 space-y-2">
          <h3 className="text-xs font-medium text-[var(--color-muted)]">用户列表</h3>
          {users.data.map((u: UserItem) => (
            <div key={u.id} className="flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2" style={{ borderColor: "var(--color-border)" }}>
              <span className="text-sm" style={{ color: "var(--color-text)" }}>{u.username}</span>
              {u.is_admin && <Badge ok label="管理员" />}
              <Badge ok={u.enabled} label={u.enabled ? "启用" : "停用"} />
              <div className="ml-auto flex flex-wrap items-center gap-2">
                <input className="w-36 rounded-md border px-2 py-1 text-xs outline-none" style={inputStyle} type="password" placeholder="重置密码" value={resetPw[u.id] ?? ""} onChange={(e) => setResetPw({ ...resetPw, [u.id]: e.target.value })} />
                <button type="button" disabled={!resetPw[u.id] || resetPw[u.id].length < 8} onClick={() => patchUser.mutate({ id: u.id, body: { password: resetPw[u.id] } })} className="rounded-md border px-2 py-1 text-xs transition hover:bg-[var(--color-surface-2)] disabled:opacity-50" style={{ borderColor: "var(--color-border)", color: "var(--color-muted)" }}>重置</button>
                <button type="button" onClick={() => patchUser.mutate({ id: u.id, body: { enabled: !u.enabled } })} className="rounded-md border px-2 py-1 text-xs transition hover:bg-[var(--color-surface-2)]" style={{ borderColor: "var(--color-border)", color: "var(--color-muted)" }}>{u.enabled ? "停用" : "启用"}</button>
                <button type="button" title="删除用户" onClick={() => { if (window.confirm(`确定删除用户 ${u.username}？`)) delUser.mutate(u.id); }} className="rounded-md border px-2 py-1 text-xs transition hover:bg-[var(--color-err)]/15" style={{ borderColor: "var(--color-border)", color: "var(--color-err)" }}><Trash2 size={12} aria-hidden /></button>
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

// ─────────────────────────── Tailscale 隧道 ───────────────────────────

function TailscaleSection() {
  const qc = useQueryClient();
  const [port, setPort] = useState("9000");
  const [actionErr, setActionErr] = useState("");
  const [loginUrl, setLoginUrl] = useState("");

  const { data, refetch, isFetching, isLoading } = useQuery({
    queryKey: ["deploy-tailscale"],
    queryFn: api.tailscaleStatus,
    staleTime: 3_000,
    // 登录授权过程中要快一点刷新（检测到登录链接/刚点击登录后尤其需要）
    refetchInterval: 5_000,
  });

  const run = useMutation({
    mutationFn: (fn: () => Promise<{ ok: boolean; detail: string }>) => fn(),
    onSuccess: (r) => {
      setActionErr(r.ok ? "" : r.detail);
      void qc.invalidateQueries({ queryKey: ["deploy-tailscale"] });
    },
    onError: (e) => setActionErr(errMsg(e)),
  });

  const install = useMutation({
    mutationFn: () => api.tailscaleInstall(),
    onSuccess: (r) => {
      setActionErr(r.ok ? "" : r.detail);
      void qc.invalidateQueries({ queryKey: ["deploy-tailscale"] });
    },
    onError: (e) => setActionErr(errMsg(e)),
  });

  const login = useMutation({
    mutationFn: () => api.tailscaleLogin(),
    onSuccess: (r) => {
      setActionErr(r.ok ? "" : r.detail);
      if (r.ok && r.detail.startsWith("http")) setLoginUrl(r.detail);
      void qc.invalidateQueries({ queryKey: ["deploy-tailscale"] });
    },
    onError: (e) => setActionErr(errMsg(e)),
  });

  const portNum = Number(port) || 9000;
  const ts: TailscaleStatus | undefined = data;

  return (
    <Card title="Tailscale 隧道（远程访问）" icon={<ExternalLink size={15} aria-hidden />}>
      {isLoading || ts === undefined ? (
        // 首次检测中：不显示任何可操作按钮，避免用户误点
        <div className="flex items-center gap-2 text-sm text-[var(--color-muted)]">
          <Loader2 size={14} className="animate-spin" aria-hidden />
          正在检测 Tailscale 状态…
        </div>
      ) : !ts.installed ? (
        <div className="space-y-2 text-sm">
          <p className="text-[var(--color-muted)]">未检测到 Tailscale。点一下，自动装好：</p>
          <button
            type="button"
            disabled={install.isPending}
            onClick={() => install.mutate()}
            className="flex items-center gap-1.5 rounded-lg bg-[var(--color-accent)] px-3 py-1.5 text-xs font-medium text-white transition hover:brightness-110 disabled:opacity-50"
          >
            {install.isPending ? <Loader2 size={13} className="animate-spin" /> : <ExternalLink size={13} />}
            {install.isPending ? "安装中…" : "安装 Tailscale"}
          </button>
          <p className="text-xs text-[var(--color-muted)]">
            先检测系统已装的（有就直接用）；没有时 Windows 自动装官方版（winget，稳定），
            Linux/macOS 下载便携版到项目目录。装完页面会自动刷新。
          </p>
        </div>
      ) : !ts.logged_in ? (
        <div className="space-y-2 text-sm">
          {loginUrl || ts.login_url ? (
            <div className="rounded-lg border p-3" style={{ borderColor: "var(--color-accent)" }}>
              <p className="mb-2 text-xs text-[var(--color-muted)]">
                打开下面的链接完成授权（浏览器授权后本页会自动刷新）：
              </p>
              <a className="break-all text-sm text-[var(--color-accent)] underline" href={loginUrl || ts.login_url} target="_blank" rel="noreferrer">
                {loginUrl || ts.login_url}
              </a>
            </div>
          ) : null}
          {ts.backend_state === "daemon_failed" ? (
            <>
              <p className="text-[var(--color-warn)]">
                tailscaled 守护进程启动失败：{ts.installed_hint || "Windows 需要管理员权限（首次会弹 UAC 授权窗口）"}
              </p>
              <button
                type="button"
                disabled={run.isPending}
                onClick={() => run.mutate(() => api.tailscaleDaemon())}
                className="flex items-center gap-1.5 rounded-lg bg-[var(--color-accent)] px-3 py-1.5 text-xs font-medium text-white transition hover:brightness-110 disabled:opacity-50"
              >
                {run.isPending ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
                重试启动守护进程（弹管理员授权）
              </button>
              {ts.daemon_error && (
                <details className="mt-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] p-2">
                  <summary className="cursor-pointer text-xs text-[var(--color-muted)]">查看错误日志</summary>
                  <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all text-[11px] leading-relaxed text-[var(--color-muted)]">
                    {ts.daemon_error}
                  </pre>
                </details>
              )}
            </>
          ) : (
            <p className="text-[var(--color-muted)]">已安装，尚未登录（状态：{ts.backend_state}）。</p>
          )}
          {!loginUrl && !ts.login_url && (
            <button
              type="button"
              disabled={login.isPending}
              onClick={() => login.mutate()}
              className="flex items-center gap-1.5 rounded-lg bg-[var(--color-accent)] px-3 py-1.5 text-xs font-medium text-white transition hover:brightness-110 disabled:opacity-50"
            >
              {login.isPending ? <Loader2 size={13} className="animate-spin" /> : <ExternalLink size={13} />}
              登录 Tailscale
            </button>
          )}
        </div>
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <Badge ok label="已登录" />
            <Badge ok label={ts.bundled ? "便携版" : "系统版"} />
            <span className="text-[var(--color-muted)]">{ts.device_name || "未知设备"}</span>
            {ts.ipv4 && <span className="text-xs text-[var(--color-muted)]">{ts.ipv4}</span>}
            <button type="button" onClick={() => void refetch()} disabled={isFetching} className="flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs transition hover:bg-[var(--color-surface-2)] disabled:opacity-50" style={{ borderColor: "var(--color-border)", color: "var(--color-muted)" }}>
              <RefreshCw size={12} className={isFetching ? "animate-spin" : ""} aria-hidden />刷新
            </button>
          </div>

          <div className="grid gap-3 md:grid-cols-2">
            <div className="rounded-lg border p-3" style={{ borderColor: "var(--color-border)" }}>
              <div className="mb-2 flex items-center justify-between">
                <span className="text-sm" style={{ color: "var(--color-text)" }}>私密访问（serve）</span>
                <Badge ok={!!ts.serve?.serve_on} label={ts.serve?.serve_on ? "已开启" : "未开启"} />
              </div>
              <p className="mb-2 text-xs text-[var(--color-muted)]">仅你账号下的设备可访问，https 加密。最推荐。</p>
              {ts.serve?.serve_on ? (
                <button type="button" disabled={run.isPending} onClick={() => run.mutate(() => api.tailscaleServeStop())} className="rounded-lg border px-3 py-1.5 text-xs transition hover:bg-[var(--color-surface-2)] disabled:opacity-50" style={{ borderColor: "var(--color-border)", color: "var(--color-muted)" }}>关闭 serve</button>
              ) : (
                <button type="button" disabled={run.isPending} onClick={() => run.mutate(() => api.tailscaleServe(portNum))} className="flex items-center gap-1.5 rounded-lg bg-[var(--color-accent)] px-3 py-1.5 text-xs font-medium text-white transition hover:brightness-110 disabled:opacity-50">{run.isPending ? <Loader2 size={13} className="animate-spin" /> : <ExternalLink size={13} />}{run.isPending ? "开启中…" : "开启 serve"}</button>
              )}
              {ts.serve?.serve_on && ts.device_name && (
                <a className="mt-2 block text-xs text-[var(--color-accent)] underline" href={`https://${ts.device_name}`} target="_blank" rel="noreferrer">https://{ts.device_name}</a>
              )}
            </div>

            <div className="rounded-lg border p-3" style={{ borderColor: "var(--color-border)" }}>
              <div className="mb-2 flex items-center justify-between">
                <span className="text-sm" style={{ color: "var(--color-text)" }}>公网访问（任何设备免装）</span>
                <Badge ok={!!ts.serve?.funnel_on} label={ts.serve?.funnel_on ? "已开启" : "未开启"} />
              </div>
              <p className="mb-2 text-xs text-[var(--color-muted)]">
                其他设备<b>无需装 Tailscale</b>，浏览器直接打开链接即可远程使用。必须配合登录鉴权。
              </p>
              {ts.serve?.funnel_on ? (
                <button type="button" disabled={run.isPending} onClick={() => run.mutate(() => api.tailscaleFunnelStop())} className="rounded-lg border px-3 py-1.5 text-xs transition hover:bg-[var(--color-surface-2)] disabled:opacity-50" style={{ borderColor: "var(--color-border)", color: "var(--color-muted)" }}>关闭 funnel</button>
              ) : (
                <button type="button" disabled={run.isPending} onClick={() => run.mutate(() => api.tailscaleFunnel(portNum))} className="flex items-center gap-1.5 rounded-lg bg-[var(--color-accent)] px-3 py-1.5 text-xs font-medium text-white transition hover:brightness-110 disabled:opacity-50">{run.isPending ? <Loader2 size={13} className="animate-spin" /> : <ExternalLink size={13} />}{run.isPending ? "开启中…" : "开启 funnel"}</button>
              )}
            </div>
          </div>

          <div className="flex items-center gap-2 text-xs text-[var(--color-muted)]">
            <span>后端端口</span>
            <input className="w-20 rounded-md border px-2 py-1 text-xs outline-none" style={inputStyle} value={port} onChange={(e) => setPort(e.target.value)} />
            {actionErr && <span className="text-[var(--color-err)]"><Linkify text={actionErr} /></span>}
          </div>
        </div>
      )}
    </Card>
  );
}

export default function DeployPanel() {
  return (
    <div className="space-y-5">
      <DeployConfigSection />
      {/* 隧道方案集中在一起：cpolar（公网主推）+ Tailscale（私密备选） */}
      <CpolarSection />
      <TailscaleSection />
      <AccountSection />
    </div>
  );
}
