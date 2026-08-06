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
import { useChatStore } from "@/store/chat";

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

/**
 * token 数。
 *
 * ## 单位由这个函数自己带上
 *
 * 原来它只返回数字，各处调用方自己拼 "tok"—— 结果是 "5.4Ktok"：
 * 挤在一起、缩写没人认得、还看不出是不是 5.4 千个 token。
 *
 * 单位跟着值走就不会再出现这种拼接错误，也不会有的地方带空格、
 * 有的地方不带。
 *
 * ## 为什么阈值是 10000 而不是 1000
 *
 * 1000 以上就缩写的话，"1.2K token" 反而比 "1200 token" 难读 ——
 * 精确到个位的四位数没有阅读负担。到五位数才值得缩写。
 */
function fmtTok(n: number): string {
  if (!n) return "";
  if (n < 10000) return `${n.toLocaleString()} token`;
  return `${(n / 1000).toFixed(1)}K token`;
}

function SpanRow({
  span,
  maxMs,
  runStart,
  runSpan,
}: {
  span: TraceSpan;
  maxMs: number;
  /** 整个 run 的起始时刻，用来算这一步在时间轴上的位置 */
  runStart: number;
  /** 整个 run 的总时长，用来归一化 */
  runSpan: number;
}) {
  // 【每一行自己管展开状态】。
  //
  // 用父级的单个 openSpanId 的话同时只能展开一条 —— 而对比两个工具
  // 调用的输入输出恰恰是最常见的需求（"为什么这次成功那次失败"）。
  const [open, setOpen] = useState(false);
  const Icon = KIND_ICON[span.kind] ?? Wrench;
  const bad = span.status !== "ok";
  const hasDetail = span.input_preview || span.output_preview || span.error;

  // 甘特式时间条：横向【位置】表示什么时候开始，长度表示持续多久。
  //
  // ## 原来的条为什么没用
  //
  // 它只按最慢的一步归一化宽度，所有条都从左边开始。于是同一行里
  // "耗时 200ms"这个数字已经说明了一切，条本身没有增加任何信息 ——
  // 用户看到的就是一根随机长度的装饰线。
  //
  // 加上起始偏移之后它能回答"哪些步骤是串行的、哪一步卡住了整个流程"，
  // 这是纯数字列表读不出来的。
  const offsetPct =
    runSpan > 0 ? ((span.started_at - runStart) / runSpan) * 100 : 0;
  const widthPct =
    runSpan > 0 && span.duration_ms ? (span.duration_ms / runSpan) * 100 : 0;
  // 太短的步骤给一个最小可见宽度，否则 5ms 的调用画不出来，
  // 看起来像"这一步没发生"。
  const drawWidth = Math.max(widthPct, 0.8);

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

        {/* 时间轴：位置=何时开始，长度=持续多久 */}
        <span
          className="relative mx-1 h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-[var(--color-border)]/50"
          title={`第 ${fmtDur(span.started_at - runStart)} 开始，持续 ${fmtDur(span.duration_ms)}`}
        >
          <span
            className="absolute inset-y-0 rounded-full"
            style={{
              left: `${Math.min(99, offsetPct)}%`,
              width: `${drawWidth}%`,
              background: bad
                ? "var(--color-err)"
                : span.kind === "llm"
                  ? "var(--color-accent)"
                  : "color-mix(in srgb, var(--color-accent) 50%, transparent)",
            }}
          />
        </span>

        <span className="shrink-0 tabular-nums text-[var(--color-muted)]">
          {fmtDur(span.duration_ms)}
        </span>
        {span.total_tokens > 0 && (
          <span className="shrink-0 tabular-nums text-[10px] text-[var(--color-muted)]">
            {fmtTok(span.total_tokens)}
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
            <SpanRow
              key={c.span_id}
              span={c}
              maxMs={maxMs}
              runStart={runStart}
              runSpan={runSpan}
            />
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

function collectStarts(spans: TraceSpan[]): number[] {
  const out: number[] = [];
  for (const s of spans) {
    out.push(s.started_at);
    out.push(...collectStarts(s.children));
  }
  return out;
}

function collectEnds(spans: TraceSpan[]): number[] {
  const out: number[] = [];
  for (const s of spans) {
    out.push(s.started_at + (s.duration_ms ?? 0));
    out.push(...collectEnds(s.children));
  }
  return out;
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

  // 时间轴的基准：整个 run 的起点和总跨度。
  //
  // 用 span 里最早的 started_at 而不是 run.started_at —— 两者可能差
  // 几十毫秒（run 记录先写、第一个 span 稍后开始），
  // 用 run 的会让所有条都往右偏一点，看起来像开头有段空白。
  const starts = collectStarts(data.spans);
  const runStart = starts.length ? Math.min(...starts) : 0;
  const ends = collectEnds(data.spans);
  const runEnd = ends.length ? Math.max(...ends) : runStart;
  const runSpan = Math.max(1, runEnd - runStart);

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-[var(--color-muted)]">
        <span>{data.turns} 轮</span>
        <span>{fmtDur(data.duration_ms)}</span>
        <span>{fmtTok(totals.total_tokens)}</span>
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
              {a.agent_name} {fmtTok(a.total_tokens)} / {a.llm_calls} 次
            </span>
          ))}
        </div>
      )}

      <ul>
        {data.spans.map((s) => (
          <SpanRow
              key={s.span_id}
              span={s}
              maxMs={maxMs}
              runStart={runStart}
              runSpan={runSpan}
            />
        ))}
      </ul>
    </div>
  );
}

export default function TracePanel() {
  const qc = useQueryClient();
  // 【用 Set 而不是单个 id】。
  //
  // 单个 id 时展开第二条会自动收起第一条 —— 而对比两次执行
  // （"上次成功这次失败，差在哪"）恰恰需要同时看。
  const [openRuns, setOpenRuns] = useState<Set<string>>(new Set());
  // 选中的会话。null = 还在会话列表层
  const [pickedSession, setPickedSession] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  // 两层结构：先会话列表，点进去才看这个会话的 run。
  //
  // ## 为什么要分层
  //
  // 原来直接铺开所有 run，几十条 run_id 的后 8 位混在一起，没有任何
  // 线索说明哪条属于哪个对话。想找"刚才那次出错的"只能一条条点开。
  //
  // 会话是用户脑子里的单位 —— 他记得的是"那次让它改 calc.py 的对话"，
  // 不是 run_38a91c04。
  //
  // 默认选中当前会话：打开追踪几乎总是因为当前对话有问题。
  const sessionId = useChatStore((s) => s.sessionId);

  const { data: stats } = useQuery({
    queryKey: ["traceStats"],
    queryFn: api.traceStats,
  });

  const { data: sessions, isLoading: loadingSessions } = useQuery({
    queryKey: ["traceSessions"],
    queryFn: api.traceSessions,
  });

  // 当前会话优先。它没有记录时留在列表层，
  // 而不是显示一个空的详情页。
  const effective =
    pickedSession ??
    (sessions?.items.some((s) => s.session_id === sessionId)
      ? sessionId
      : null);

  const { data: runs, isLoading } = useQuery({
    // effective 进 key：切会话后必须重新拉，
    // 否则显示的还是上一个会话的记录
    queryKey: ["traces", effective ?? "none"],
    queryFn: () => api.listTraces(effective ?? undefined),
    enabled: !!effective,
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
        {/* 详情层显示会话标题 —— 不显示的话用户点进来之后
            不知道自己在看哪个对话的记录。 */}
        {effective ? (
          <span className="min-w-0 truncate text-xs text-[var(--color-muted)]">
            /{" "}
            {sessions?.items.find((s) => s.session_id === effective)?.title ??
              "未命名会话"}
          </span>
        ) : (
          stats && (
            <span className="text-xs text-[var(--color-muted)]">
              {stats.runs} 次执行 · {stats.spans} 条 span
            </span>
          )
        )}
        {/* 回到会话列表。只在详情层显示 —— 列表层显示它没有意义，
            而一个点了没反应的按钮比没有按钮更让人困惑。 */}
        {effective && (
          <button
            type="button"
            onClick={() => {
              setPickedSession(null);
              setOpenRuns(new Set());
            }}
            className="ml-auto rounded-lg border border-[var(--color-border)] px-2 py-1 text-xs hover:bg-[var(--color-bg)]"
          >
            ← 所有对话
          </button>
        )}
        <button
          type="button"
          onClick={() => cleanup.mutate(stats?.retain_days ?? 14)}
          disabled={cleanup.isPending}
          className={`${
            effective ? "" : "ml-auto "
          }rounded-lg border border-[var(--color-border)] px-2 py-1 text-xs hover:bg-[var(--color-bg)]`}
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

      {/* ── 第一层：会话列表 ── */}
      {!effective ? (
        loadingSessions ? (
          <div className="flex items-center gap-1.5 text-xs text-[var(--color-muted)]">
            <Loader2 size={12} className="animate-spin" aria-hidden />
            加载中…
          </div>
        ) : !sessions || sessions.items.length === 0 ? (
          <div className="rounded-lg border border-dashed border-[var(--color-border)] px-3 py-6 text-center text-xs text-[var(--color-muted)]">
            还没有执行记录。发一条消息后这里会出现。
          </div>
        ) : (
          <ul className="space-y-1">
            {sessions.items.map((s) => (
              <li key={s.session_id}>
                <button
                  type="button"
                  onClick={() => {
                    setPickedSession(s.session_id);
                    setOpenRuns(new Set());
                  }}
                  className="flex w-full items-center gap-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-2 text-left hover:border-[var(--color-accent)]"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <span className="truncate text-xs">{s.title}</span>
                      {/* 当前对话标出来 —— 列表按时间排，
                          用户未必认得出哪条是自己正在聊的 */}
                      {s.session_id === sessionId && (
                        <span className="shrink-0 rounded bg-[var(--color-accent)]/15 px-1 text-[9px] text-[var(--color-accent)]">
                          当前
                        </span>
                      )}
                      {s.errors > 0 && (
                        <span className="shrink-0 text-[10px] text-[var(--color-err)]">
                          {s.errors} 次失败
                        </span>
                      )}
                    </div>
                    <div className="mt-0.5 text-[10px] text-[var(--color-muted)]">
                      {new Date(s.last_at).toLocaleString("zh-CN")}
                    </div>
                  </div>
                  <div className="shrink-0 text-right text-[10px] tabular-nums text-[var(--color-muted)]">
                    <div>{s.runs} 次执行</div>
                    <div>{fmtTok(s.total_tokens)}</div>
                    {/* 花费只在配过单价时显示。没配价时显示 $0.00
                        会让人以为免费 —— 那是"不知道"，不是零。 */}
                    {s.cost_usd > 0 && <div>${s.cost_usd.toFixed(4)}</div>}
                  </div>
                  <ChevronRight
                    size={12}
                    className="shrink-0 text-[var(--color-muted)]"
                    aria-hidden
                  />
                </button>
              </li>
            ))}
          </ul>
        )
      ) : isLoading ? (
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
            const open = openRuns.has(r.run_id);
            const bad = r.status === "error" || r.stop_reason !== "final";
            return (
              <li
                key={r.run_id}
                className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)]"
              >
                <button
                  type="button"
                  onClick={() =>
                    setOpenRuns((s) => {
                      const n = new Set(s);
                      if (n.has(r.run_id)) n.delete(r.run_id);
                      else n.add(r.run_id);
                      return n;
                    })
                  }
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
                    {r.turns} 轮 · {fmtDur(r.duration_ms)} · {fmtTok(r.total_tokens)}
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
