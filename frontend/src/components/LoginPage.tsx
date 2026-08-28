/**
 * 登录页。
 *
 * 仅当后端开启鉴权（JEEVES_SECURITY__AUTH_ENABLED=true）且当前无有效会话时显示。
 * 提交成功后 cookie 由后端下发，前端不需要存任何凭证。
 */

import { useState } from "react";
import { KeyRound, Loader2, ShieldCheck } from "lucide-react";
import { useAuth } from "@/store/auth";
import { ApiError } from "@/lib/sse";

const inputCls =
  "w-full rounded-lg border px-3 py-2 text-sm outline-none focus:border-[var(--color-accent)]";
const inputStyle = {
  borderColor: "var(--color-border)",
  background: "var(--color-bg)",
} as const;

export default function LoginPage() {
  const login = useAuth((s) => s.login);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setErr("");
    try {
      await login(username.trim(), password);
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : "登录失败，请重试";
      setErr(msg);
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full items-center justify-center p-6">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm space-y-4 rounded-2xl border p-8 shadow-xl"
        style={{
          borderColor: "var(--color-border)",
          background: "var(--color-surface)",
        }}
      >
        <div className="flex items-center gap-3">
          <div
            className="flex h-10 w-10 items-center justify-center rounded-xl"
            style={{ background: "var(--color-accent)", color: "#0f1115" }}
          >
            <ShieldCheck size={22} />
          </div>
          <div>
            <h1 className="text-lg font-medium" style={{ color: "var(--color-text)" }}>
              Jeeves 远程访问
            </h1>
            <p className="text-xs" style={{ color: "var(--color-muted)" }}>
              登录后才能继续使用
            </p>
          </div>
        </div>

        {err && (
          <div
            role="alert"
            className="rounded-lg border px-3 py-2 text-sm"
            style={{
              borderColor: "var(--color-err)",
              color: "var(--color-err)",
              background: "color-mix(in srgb, var(--color-err) 12%, transparent)",
            }}
          >
            {err}
          </div>
        )}

        <label className="block">
          <span className="mb-1 block text-sm" style={{ color: "var(--color-text)" }}>
            用户名
          </span>
          <input
            className={inputCls}
            style={inputStyle}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
            autoComplete="username"
            placeholder="admin"
          />
        </label>

        <label className="block">
          <span className="mb-1 block text-sm" style={{ color: "var(--color-text)" }}>
            密码
          </span>
          <input
            type="password"
            className={inputCls}
            style={inputStyle}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            placeholder="••••••••"
          />
        </label>

        <button
          type="submit"
          disabled={busy || !username.trim() || !password}
          className="flex w-full items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition disabled:opacity-50"
          style={{ background: "var(--color-accent)", color: "#0f1115" }}
        >
          {busy ? <Loader2 size={16} className="animate-spin" /> : <KeyRound size={16} />}
          {busy ? "登录中…" : "登录"}
        </button>

        <p className="text-xs leading-relaxed" style={{ color: "var(--color-muted)" }}>
          连续输错会触发限流（默认 15 分钟内最多 10 次）。首次部署时若未在
          .env 配置管理员密码，初始密码会打印在服务启动日志里。
        </p>
        <p className="text-center text-[11px]" style={{ color: "var(--color-muted)" }}>
          v{__APP_VERSION__}
        </p>
      </form>
    </div>
  );
}

