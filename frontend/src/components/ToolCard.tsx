import { useState } from "react";
import {
  ChevronRight,
  FileText,
  FolderTree,
  Pencil,
  Search,
  Terminal,
  ListChecks,
  CircleAlert,
  Loader2,
  Check,
} from "lucide-react";
import clsx from "clsx";
import type { ToolCard as ToolCardData } from "@/store/chat";

const ICONS: Record<string, typeof FileText> = {
  read_file: FileText,
  write_file: Pencil,
  edit_file: Pencil,
  list_dir: FolderTree,
  glob: Search,
  grep: Search,
  run_command: Terminal,
  run_python: Terminal,
  todo_write: ListChecks,
  todo_read: ListChecks,
};

/** 每个工具的一句话摘要。摘要比原始 JSON 好读得多 */
function summarize(t: ToolCardData): string {
  const d = (t.display ?? {}) as Record<string, unknown>;
  const a = t.args ?? {};
  switch (t.tool_name) {
    case "read_file":
      return `${d.path ?? a.path ?? ""}${d.total_lines ? `（${d.total_lines} 行）` : ""}`;
    case "write_file":
      return `${d.path ?? a.path ?? ""}${d.created ? "（新建）" : "（覆盖）"}`;
    case "edit_file":
      return String(d.path ?? a.path ?? "");
    case "list_dir":
      return `${d.path ?? a.path ?? "."}${d.count !== undefined ? `（${d.count} 项）` : ""}`;
    case "glob":
      return `${a.pattern ?? ""} → ${d.count ?? 0} 个文件`;
    case "grep":
      return `${a.pattern ?? ""} → ${d.count ?? 0} 处匹配`;
    case "todo_write": {
      const st = d.stats as Record<string, number> | undefined;
      return st ? `${st.completed}/${st.total} 完成` : "已更新清单";
    }
    default: {
      const first = Object.values(a)[0];
      return typeof first === "string" ? first.slice(0, 60) : "";
    }
  }
}

export default function ToolCard({ tool }: { tool: ToolCardData }) {
  const [open, setOpen] = useState(false);
  const Icon = ICONS[tool.tool_name] ?? Terminal;
  const summary = summarize(tool);

  return (
    <div
      className={clsx(
        "rounded-lg border text-sm transition",
        tool.status === "error"
          ? "border-[var(--color-err)]/40 bg-[var(--color-err)]/8"
          : "border-[var(--color-border)] bg-[var(--color-surface)]",
      )}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
      >
        <ChevronRight
          size={14}
          aria-hidden
          className={clsx("shrink-0 transition-transform", open && "rotate-90")}
        />
        <Icon size={14} aria-hidden className="shrink-0 text-[var(--color-muted)]" />
        <span className="font-mono text-xs text-[var(--color-accent)]">
          {tool.tool_name}
        </span>
        {summary && (
          <span className="truncate text-xs text-[var(--color-muted)]">
            {summary}
          </span>
        )}
        <span className="ml-auto flex shrink-0 items-center gap-1.5">
          {tool.duration_ms !== undefined && (
            <span className="text-[11px] text-[var(--color-muted)]">
              {tool.duration_ms < 1000
                ? `${tool.duration_ms}ms`
                : `${(tool.duration_ms / 1000).toFixed(1)}s`}
            </span>
          )}
          {tool.status === "running" && (
            <Loader2
              size={13}
              aria-label="执行中"
              className="animate-spin text-[var(--color-accent)]"
            />
          )}
          {tool.status === "ok" && (
            <Check size={13} aria-label="成功" className="text-[var(--color-ok)]" />
          )}
          {tool.status === "error" && (
            <CircleAlert
              size={13}
              aria-label="失败"
              className="text-[var(--color-err)]"
            />
          )}
        </span>
      </button>

      {open && (
        <div className="space-y-2 border-t border-[var(--color-border)] px-3 py-2">
          <div>
            <p className="mb-1 text-[11px] text-[var(--color-muted)]">参数</p>
            <pre className="overflow-x-auto rounded bg-[var(--color-bg)] p-2 font-mono text-[11px] leading-relaxed">
              {JSON.stringify(tool.args, null, 2)}
            </pre>
          </div>
          {tool.content_preview && (
            <div>
              <p className="mb-1 text-[11px] text-[var(--color-muted)]">
                结果预览（完整内容已保存）
              </p>
              <pre className="max-h-64 overflow-auto rounded bg-[var(--color-bg)] p-2 font-mono text-[11px] leading-relaxed whitespace-pre-wrap">
                {tool.content_preview}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
