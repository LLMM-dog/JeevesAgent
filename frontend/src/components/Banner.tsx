import { X } from "lucide-react";
import clsx from "clsx";
import { useChatStore } from "@/store/chat";

export default function Banner() {
  const banner = useChatStore((s) => s.banner);
  const dismiss = useChatStore((s) => s.dismissBanner);
  if (!banner) return null;

  const tone =
    banner.kind === "error"
      ? "border-[var(--color-err)]/40 bg-[var(--color-err)]/10 text-[var(--color-err)]"
      : banner.kind === "warn"
        ? "border-[var(--color-warn)]/40 bg-[var(--color-warn)]/10 text-[var(--color-warn)]"
        : "border-[var(--color-border)] bg-[var(--color-surface-2)] text-[var(--color-muted)]";

  return (
    <div role="alert" className={clsx("mx-4 mt-3 rounded-lg border px-3 py-2 text-sm", tone)}>
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <p className="font-medium">{banner.message}</p>
          {/* hint 是"下一步该做什么"，比错误本身更有用，必须显示 */}
          {banner.hint && (
            <p className="mt-0.5 text-xs opacity-80">{banner.hint}</p>
          )}
          {banner.code && (
            <p className="mt-0.5 font-mono text-[11px] opacity-50">{banner.code}</p>
          )}
        </div>
        <button
          type="button"
          onClick={dismiss}
          aria-label="关闭提示"
          className="shrink-0 rounded p-0.5 opacity-60 transition hover:opacity-100"
        >
          <X size={14} aria-hidden />
        </button>
      </div>
    </div>
  );
}