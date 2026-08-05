import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Bot,
  ChevronRight,
  Cpu,
  Loader2,
  Scissors,
  TriangleAlert,
  Wrench,
} from "lucide-react";
import { useState } from "react";

import { api } from "@/lib/api";
import type { TraceSpan } from "@/lib/types";

/**
 * 执行树面板。
 *
 * ## 为什么值得做
 *
 * 常见实现没有可查的执行树 —— 排查"这一步为什么慢/为什么失败"
 * 只能翻日志。而日志是扁平的，一次带子代理的执行会交错在一起，
 * 根本看不出嵌套关系。
 *
 * 落库之后每一步的耗时、token、输入输出都能点开看。
 */

const KIND_ICON = {
  llm: Cpu,
  tool: Wrench,
  agent: Bot,
  compaction: Scissors,
} as const;

function fmtDur(ms: number | null): string {
  if (ms == null) return "";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function fmtTok(n: number): string {
  if (!n) return "";
  return n > 1000 ? `${(n / 1000).toFixed(1)}K` : String(n);
}

function SpanRow({ span, maxMs }: { span: TraceSpan; maxMs: number }) {
  const [open, setOpen] = useState(false);
  const Icon = KIND_ICON[span.kind] ?? Wrench;
  const bad = span.status !== "ok";
  const hasDetail = span.input_preview || span.output_preview || span.error;

  // 耗时条。绝对宽度按本次执行最慢的一步归一化 ——
  // 按固定刻度的话快的步骤全挤成一条线，看不出差异。
  const pct = maxMs > 0 && span.duration_ms ? (span.duration_ms / maxMs) * 100 : 0;

  return (
    <li>
      <div
        className={`group flex items-center gap-1.5 rounded px-1 py-0.5 text-[11px] ${
          hasDetail ? "cursor-pointer hover:bg-[var(--color-surface)]" : ""
        }`}
        onClick={() => hasDetail && setOpen((v) => !v)}
      >
        {hasDetail ? (
          <ChevronRight
            size={10}
            className={`shrink-0 text-[var(--color-muted)] transition-transform ${
              open ? "rotate-90" : ""
            }`}
            aria-hidden
          />
        ) : (
          <span className="w-[10px] shrink-0" />
        )}
        <Icon
          size={11}
          className={`shrink-0 ${
            bad ? "text-[var(--color-err)]" : "text-[var(--color-muted)]"
          }`}
          aria-hidden
        />
        <span className="shrink-0 font-mono">{span.name}</span>
        {span.agent_name && span.agent_name !== "main" && (
          <span className="shrink-0 rounded bg-[var(--color-accent)]/15 px-1 text-[9px] text-[var(--color-accent)]">
            {span.agent_name}
          </span>
        )}

        {/* 耗时条 */}
        <span className="mx-1 h-1 min-w-0 flex-1 overflow-hidden rounded-full bg-[var(--color-border)]/50">
          <span
            className="block h-full rounded-full bg-[var(--color-accent)]/50"
            style={{ width: `${pct}%` }}
          />
        </span>

        <span className="shrink-0 tabular-nums text-[var(--color-muted)]">
          {fmtDur(span.duration_ms)}
        </span>
        {span.total_tokens > 0 && (
          <span className="shrink-0 tabular-nums text-[10px] text-[var(--color-muted)]">
            {fmtTok(span.total_tokens)}tok
          </span>
        )}
        {/* 成本只在配过单价时显示。
            没配价时显示 $0.00 会让人以为免费 —— 那是"不知道"，不是零。 */}
        {span.has_price && span.cost_usd > 0 && (
          <span className="shrink-0 tabular-nums text-[10px] text-[var(--color-muted)]">
            ${span.cost_usd.toFixed(5)}
          </span>
        )}
      </div>

      {open && hasDetail && (
        <div className="ml-4 mb-1 space-y-1 border-l border-[var(--color-border)] pl-2">
          {span.error && (
            <div className="text-[10px] text-[var(--color-err)]">{span.error}</div>
          )}
          {span.input_preview && (
            <Detail
              label="输入"
              text={span.input_preview}
              truncated={span.input_truncated}
              bytes={span.input_bytes}
            />
          )}
          {span.output_preview && (
            <Detail
              label="输出"
              text={span.output_preview}
              truncated={span.output_truncated}
              bytes={span.output_bytes}
            />
          )}
        </div>
      )}

      {span.children.length > 0 && (
        <ul className="ml-3 border-l border-[var(--color-border)] pl-1">
          {span.children.map((c) => (
            <SpanRow key={c.span_id} span={c} maxMs={maxMs} />
          ))}
        </ul>
      )}
    </li>
  );
}

function Detail({
  label,
  text,
  truncated,
  bytes,
}: {
  label: string;
  text: string;
  truncated: boolean;
  bytes: number;
}) {
  return (
    <div>
      <div className="flex items-center gap-1 text-[9px] text-[var(--color-muted)]">
        {label}
        {/* 被截断这件事本身要显示出来 —— 否则读的人无法判断
            "这就是全部"还是"还有更多" */}
        {truncated && (
          <span className="text-[var(--color-warn)]">
            已截断，原始 {(bytes / 1024).toFixed(1)}KB
          </span>
        )}
      </div>
      <pre className="max-h-32 overflow-auto whitespace-pre-wrap break-all rounded bg-[var(--color-bg)] p-1 font-mono text-[10px] text-[var(--color-muted)]">
        {text}
      </pre>
    </div>
  );
}

function collectMax(spans: TraceSpan[]): number {
  let m = 0;
  for (const s of spans) {
    if (s.duration_ms && s.duration_ms > m) m = s.duration_ms;
    const cm = collectMax(s.children);
    if (cm > m) m = cm;
  }
  return m;
}

function TraceDetail({ runId }: { runId: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ["trace", runId],
    queryFn: () => api.getTrace(runId),
  });

  if (isLoading) return <div className="text-xs text-[var(--color-muted)]">加载中…</div>;
  if (!data) return null;

  const maxMs = collectMax(data.spans);
  const totals = data.span_totals;

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-[var(--color-muted)]">
        <span>{data.turns} 轮</span>
        <span>{fmtDur(data.duration_ms)}</span>
        <span>{fmtTok(totals.total_tokens)} tok</span>
        {data.stop_reason !== "final" && (
          <span className="text-[var(--color-warn)]">{data.stop_reason}</span>
        )}
      </div>

      {/* 按智能体拆分。"委派花了多少"是判断委派值不值的唯一依据 ——
          常见实现答不出这个数 */}
      {totals.by_agent.length > 1 && (
        <div className="flex flex-wrap gap-2 text-[10px]">
          {totals.by_agent.map((a) => (
            <span
              key={a.agent_name}
              className="rounded bg-[var(--color-bg)] px-1.5 py-0.5 text-[var(--color-muted)]"
            >
              {a.agent_name} {fmtTok(a.total_tokens)}tok / {a.llm_calls} 次
            </span>
          ))}
        </div>
      )}

      <ul>
        {data.spans.map((s) => (
          <SpanRow key={s.span_id} span={s} maxMs={maxMs} />
        ))}
      </ul>
    </div>
  );
}

export default function TracePanel() {
  const qc = useQueryClient();
  const [openRun, setOpenRun] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const { data: stats } = useQuery({
    queryKey: ["traceStats"],
    queryFn: api.traceStats,
  });
  const { data: runs, isLoading } = useQuery({
    queryKey: ["traces"],
    queryFn: () => api.listTraces(),
  });

  const cleanup = useMutation({
    mutationFn: (days: number) => api.cleanupTraces(days),
    onSuccess: (r) => {
      setNote(`已清理 ${r.runs} 条执行、${r.spans} 条 span`);
      void qc.invalidateQueries({ queryKey: ["traces"] });
      void qc.invalidateQueries({ queryKey: ["traceStats"] });
    },
  });

  return (
    <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <div className="mb-3 flex items-center gap-2">
        <h2 className="text-sm font-medium">执行记录</h2>
        {stats && (
          <span className="text-xs text-[var(--color-muted)]">
            {stats.runs} 次执行 · {stats.spans} 条 span
          </span>
        )}
        <button
          type="button"
          onClick={() => cleanup.mutate(stats?.retain_days ?? 14)}
          disabled={cleanup.isPending}
          className="ml-auto rounded-lg border border-[var(--color-border)] px-2 py-1 text-xs hover:bg-[var(--color-bg)]"
        >
          {cleanup.isPending ? "清理中…" : `清理 ${stats?.retain_days ?? 14} 天前`}
        </button>
      </div>

      <p className="mb-3 text-xs text-[var(--color-muted)]">
        每一步的耗时、token 和输入输出。span 表增长约是消息表的 5~10 倍，
        默认保留 {stats?.retain_days ?? 14} 天。
      </p>

      {/* 写入器丢弃/失败数必须暴露 —— 静默丢弃会让人以为追踪是完整的 */}
      {stats?.writer && (stats.writer.dropped > 0 || stats.writer.failed > 0) && (
        <div
          role="alert"
          className="mb-3 flex items-start gap-2 rounded-lg bg-[var(--color-warn)]/10 px-3 py-2 text-xs text-[var(--color-warn)]"
        >
          <TriangleAlert size={13} className="mt-0.5 shrink-0" aria-hidden />
          <div>
            追踪数据不完整：丢弃 {stats.writer.dropped} 条、失败{" "}
            {stats.writer.failed} 条。
            {stats.writer.recent_errors.length > 0 && (
              <div className="mt-0.5 font-mono text-[10px] opacity-80">
                {stats.writer.recent_errors[0]}
              </div>
            )}
          </div>
        </div>
      )}

      {note && (
        <div className="mb-3 rounded-lg bg-[var(--color-ok)]/10 px-3 py-2 text-xs text-[var(--color-ok)]">
          {note}
        </div>
      )}

      {isLoading ? (
        <div className="flex items-center gap-1.5 text-xs text-[var(--color-muted)]">
          <Loader2 size={12} className="animate-spin" aria-hidden />
          加载中…
        </div>
      ) : !runs || runs.items.length === 0 ? (
        <div className="rounded-lg border border-dashed border-[var(--color-border)] px-3 py-6 text-center text-xs text-[var(--color-muted)]">
          还没有执行记录。发一条消息后这里会出现。
        </div>
      ) : (
        <ul className="space-y-1">
          {runs.items.map((r) => {
            const open = openRun === r.run_id;
            const bad = r.status === "error" || r.stop_reason !== "final";
            return (
              <li
                key={r.run_id}
                className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)]"
              >
                <button
                  type="button"
                  onClick={() => setOpenRun(open ? null : r.run_id)}
                  aria-expanded={open}
                  className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left"
                >
                  <ChevronRight
                    size={11}
                    className={`shrink-0 text-[var(--color-muted)] transition-transform ${
                      open ? "rotate-90" : ""
                    }`}
                    aria-hidden
                  />
                  <span className="shrink-0 font-mono text-[10px] text-[var(--color-muted)]">
                    {r.run_id.slice(-8)}
                  </span>
                  <span className="shrink-0 text-[11px] text-[var(--color-muted)]">
                    {new Date(r.started_at).toLocaleTimeString("zh-CN")}
                  </span>
                  <span className="min-w-0 flex-1" />
                  {bad && (
                    <span className="shrink-0 text-[10px] text-[var(--color-warn)]">
                      {r.stop_reason || r.status}
                    </span>
                  )}
                  <span className="shrink-0 tabular-nums text-[10px] text-[var(--color-muted)]">
                    {r.turns} 轮 · {fmtDur(r.duration_ms)} · {fmtTok(r.total_tokens)}tok
                  </span>
                </button>
                {open && (
                  <div className="border-t border-[var(--color-border)] px-2.5 py-2">
                    <TraceDetail runId={r.run_id} />
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
