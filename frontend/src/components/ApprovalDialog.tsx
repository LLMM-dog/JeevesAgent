import { AlertTriangle, Check, X } from "lucide-react";
import { useEffect, useState } from "react";

import { useChatStore } from "../store/chat";

/**
 * 工具调用审批框。
 *
 * ## 为什么必须显示完整参数
 *
 * 只显示"要执行 run_shell，是否允许"等于让用户盲签 —— 而盲签几次之后
 * 用户就会习惯性点通过，审批也就失去了意义。命令原文必须在眼前。
 *
 * ## 为什么显示倒计时
 *
 * 超时视为拒绝。不显示剩余时间的话，用户走开一会回来发现操作被拒了，
 * 会以为是 bug。看到倒计时至少知道发生了什么。
 */
export function ApprovalDialog() {
  const approval = useChatStore((s) => s.approval);
  const respond = useChatStore((s) => s.respondApproval);
  const [remaining, setRemaining] = useState(0);

  useEffect(() => {
    if (!approval) return;
    const tick = () => {
      const left = Math.max(0, Math.ceil((approval.timeout_at - Date.now()) / 1000));
      setRemaining(left);
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [approval]);

  // Esc 视为拒绝。危险操作的默认选择必须是"不执行"——
  // 用户随手按 Esc 想关掉弹窗时，不能因此执行了命令。
  useEffect(() => {
    if (!approval) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        void respond(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [approval, respond]);

  if (!approval) return null;

  const { tool_name, args } = approval;
  // 命令类工具把命令原文单独拎出来显示 —— 那是用户真正要看的东西。
  const primary =
    typeof args.command === "string"
      ? (args.command as string)
      : typeof args.code === "string"
        ? (args.code as string)
        : null;
  const rest = Object.entries(args).filter(
    ([k]) => k !== "command" && k !== "code",
  );

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="approval-title"
    >
      <div className="w-full max-w-2xl overflow-hidden rounded-xl border border-[var(--color-border)] bg-[var(--color-bg)] shadow-xl">
        <div className="flex items-center gap-2 border-b border-[var(--color-border)] px-4 py-3">
          <AlertTriangle size={16} aria-hidden className="text-amber-500" />
          <h2 id="approval-title" className="text-sm font-medium">
            需要确认：{tool_name}
          </h2>
          <span
            className="ml-auto text-xs text-[var(--color-muted)]"
            aria-live="polite"
          >
            {remaining > 0 ? `${remaining} 秒后自动拒绝` : "即将超时"}
          </span>
        </div>

        <div className="max-h-[50vh] overflow-auto px-4 py-3">
          {primary !== null && (
            <pre className="mb-3 overflow-x-auto whitespace-pre-wrap break-all rounded-lg bg-[var(--color-surface)] px-3 py-2 text-xs">
              <code>{primary}</code>
            </pre>
          )}
          {rest.length > 0 && (
            <dl className="space-y-1 text-xs">
              {rest.map(([k, v]) => (
                <div key={k} className="flex gap-2">
                  <dt className="shrink-0 text-[var(--color-muted)]">{k}</dt>
                  <dd className="break-all">{JSON.stringify(v)}</dd>
                </div>
              ))}
            </dl>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-[var(--color-border)] px-4 py-3">
          <button
            type="button"
            onClick={() => void respond(false)}
            className="flex items-center gap-1.5 rounded-lg border border-[var(--color-border)] px-3 py-1.5 text-xs hover:bg-[var(--color-surface)]"
          >
            <X size={13} aria-hidden />
            拒绝
            <kbd className="ml-1 text-[10px] text-[var(--color-muted)]">Esc</kbd>
          </button>
          <button
            type="button"
            onClick={() => void respond(true)}
            // 不设 autoFocus。危险操作的确认按钮不该能被回车直接触发 ——
            // 用户在输入框敲完回车的惯性会直接批准掉下一个弹窗。
            className="flex items-center gap-1.5 rounded-lg bg-[var(--color-accent)] px-3 py-1.5 text-xs text-white hover:opacity-90"
          >
            <Check size={13} aria-hidden />
            允许执行
          </button>
        </div>
      </div>
    </div>
  );
}
