import { useQuery } from "@tanstack/react-query";

import { api } from "../lib/api";
import type { ContextUsageEvent } from "../lib/types";

/**
 * 上下文占用条。
 *
 * ## 百分比按窗口算，不是分项之间互比
 *
 * 之前显示"工具定义 70%"——那是它在【已用部分】里的占比。用户看到
 * 70% 的第一反应是"窗口快被工具吃满了"，而实际上 3188 / 131072 只有
 * 2.4%，空间非常充裕。
 *
 * 分项之间的比例只在"该优化谁"时有意义，而那件事看绝对数字就够了。
 * 用户真正要的是"我还剩多少"—— 那必须以窗口为分母。
 *
 * ## 固定开销常驻显示
 *
 * 工具定义和系统提示词在发消息之前就确定了（工具集和人格文件都是
 * 配置，不随对话变）。之前只有 run 期间的 context_usage 事件带这两项，
 * 于是切一次页面就只剩对话内容那一段 —— 看起来像固定开销凭空消失。
 *
 * 现在从 /context-overhead 单独拉，没有实测数据时也显示。
 *
 * ## 两种数据的可信度不同
 *
 * 固定开销是本地 tiktoken 估的（比模型的分词器偏高 30% 左右），
 * 总数是模型返回的真实值。有实测值时按比例校正分项，
 * 并标明分项仍是估算。
 */
export function ContextBar({
  usage,
  windowTokens,
  sessionId,
}: {
  usage: ContextUsageEvent | null;
  windowTokens: number;
  sessionId: string | null;
}) {
  // 固定开销。会话换了模型时窗口会变，所以带上 sessionId。
  //
  // staleTime 给 5 分钟：它只在改了 MCP 配置或人格文件后变，
  // 每次渲染都拉的话是白算 —— 那两个操作都要过设置页，
  // 而设置页保存后会 invalidate。
  const { data: overhead } = useQuery({
    queryKey: ["contextOverhead", sessionId ?? "none"],
    queryFn: () => api.contextOverhead(sessionId ?? undefined),
    staleTime: 5 * 60_000,
  });

  const win = usage?.window_tokens || windowTokens || overhead?.window_tokens || 0;
  if (!win) return null;

  // 分项来源：有实测就用实测（已按比例校正过），否则用本地估算
  const hasLive = !!usage && (usage.tools_tokens ?? 0) > 0;
  const tools = hasLive
    ? (usage!.tools_tokens ?? 0)
    : (overhead?.tools_tokens ?? 0);
  const system = hasLive
    ? (usage!.system_tokens ?? 0)
    : (overhead?.system_tokens ?? 0);
  const toolCount = hasLive
    ? (usage!.tool_count ?? 0)
    : (overhead?.tool_count ?? 0);

  // 对话内容：只有实测时才知道。
  //
  // 【必须 clamp 到 0】。分项是按占比估的，四舍五入后可能比总数多 1~2，
  // 不 clamp 的话这一段是负宽度，整条渐变错位。
  const convo = usage ? Math.max(0, usage.used_tokens - tools - system) : 0;

  const used = usage ? usage.used_tokens : tools + system;
  const ratio = used / win;
  const near = ratio > 0.75;

  // 【分母是窗口】。用 used 当分母的话"工具定义 70%"会让用户以为
  // 窗口快满了，而它其实只占 2.4%。
  const pct = (n: number) => (n / win) * 100;
  const label = (n: number) => {
    const v = pct(n);
    // 小于 0.1% 时显示 "<0.1%" 而不是 "0.0%"——后者看起来像"没有"，
    // 而用户正在找这个数字。
    return v > 0 && v < 0.1 ? "<0.1%" : `${v.toFixed(1)}%`;
  };

  const segments = [
    {
      key: "tools",
      label: "工具定义",
      tokens: tools,
      color: "var(--color-accent)",
      note: toolCount ? `${toolCount} 个工具，每轮重发` : "每轮重发",
    },
    {
      key: "system",
      label: "系统提示词",
      tokens: system,
      color: "color-mix(in srgb, var(--color-accent) 45%, transparent)",
      note: "性格、行为规则、运行环境，每轮重发",
    },
    {
      key: "convo",
      label: "对话内容",
      tokens: convo,
      color: near ? "var(--color-warn)" : "var(--color-ok)",
      note: "消息、工具返回",
    },
  ].filter((s) => s.tokens > 0);

  if (segments.length === 0) {
    return (
      <div className="mb-2 flex items-center gap-2 text-[11px] text-[var(--color-muted)]">
        <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-[var(--color-border)]" />
        <span>窗口 {win.toLocaleString()} token</span>
      </div>
    );
  }

  return (
    <div className="mb-2 space-y-1 text-[11px] text-[var(--color-muted)]">
      <div
        className="flex h-1.5 overflow-hidden rounded-full bg-[var(--color-border)]"
        role="img"
        aria-label={
          `上下文占用 ${used.toLocaleString()} / ${win.toLocaleString()} token，` +
          `共 ${(ratio * 100).toFixed(1)}%：` +
          segments
            .map((s) => `${s.label} ${s.tokens.toLocaleString()}`)
            .join("，")
        }
      >
        {segments.map((s) => (
          <div
            key={s.key}
            className="h-full transition-all"
            style={{ width: `${pct(s.tokens)}%`, background: s.color }}
          />
        ))}
      </div>

      {/* 图例一直显示，不藏在 hover 里 —— 用户不会去 hover 一个
          他认为是坏的数字。 */}
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-0.5">
        {segments.map((s) => (
          <span
            key={s.key}
            className="flex items-center gap-1"
            title={`${s.label}：${s.note}`}
          >
            <span
              className="inline-block h-2 w-2 shrink-0 rounded-sm"
              style={{ background: s.color }}
              aria-hidden
            />
            {s.label} {s.tokens.toLocaleString()}
            <span className="opacity-70">({label(s.tokens)})</span>
          </span>
        ))}

        <span className="ml-auto tabular-nums">
          共 {used.toLocaleString()} / {win.toLocaleString()} ({label(used)})
          {/* 没有实测数据时说清这是固定部分，否则用户会以为
              "才用了 3%"包括了他的对话。 */}
          {!usage && " 固定开销"}
          {/* 估算必须标出来。反过来同样要紧：真实总数【不能】标成估算。 */}
          {usage?.is_estimate && "（估算）"}
          {!usage && overhead?.is_estimate && "（估算）"}
        </span>
      </div>

      {/* 固定开销占窗口比例高时才提示。
          用"占已用部分的比例"当判据会在每次新会话都触发 ——
          那时对话内容是 0，固定开销必然占 100%。 */}
      {(tools + system) / win > 0.15 && (
        <p className="opacity-80">
          前两项每一轮都会重发，占窗口 {label(tools + system)}。想降下来：
          设置页关掉用不到的 MCP 服务器，或精简人格与偏好里的行为规则。
        </p>
      )}

      {near && (
        <p style={{ color: "var(--color-warn)" }}>
          接近窗口上限，超过 {Math.round((usage!.compact_at / win) * 100)}%
          会自动压缩较早的消息。
        </p>
      )}
    </div>
  );
}
