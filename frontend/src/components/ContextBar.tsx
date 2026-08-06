import type { ContextUsageEvent } from "../lib/types";

/**
 * 上下文占用条。
 *
 * ## 为什么要分段着色
 *
 * 原来是一根单色条，只显示"4547 / 131072"。用户看到一句"你好"占了
 * 4547，唯一的解释是"计数坏了"—— 而实际上其中 3181 是工具定义、
 * 1365 是系统提示词，对话本身只有 1。
 *
 * 分段之后这件事不用解释：三种颜色的宽度直接说明了钱花在哪。
 *
 * ## 为什么要一直显示
 *
 * 原来只在收到 context_usage 事件后才出现，也就是发过消息才有。
 * 但"这个模型有多大窗口""固定开销占多少"是发消息【之前】就该知道的 ——
 * 尤其是准备粘一段长代码进去的时候。
 *
 * 没有实测数据时显示窗口大小和"发一条消息后显示占用"，
 * 而不是整块消失。
 */
export function ContextBar({
  usage,
  windowTokens,
}: {
  usage: ContextUsageEvent | null;
  /** 没有 usage 时也要能显示窗口大小 */
  windowTokens: number;
}) {
  // 还没有实测数据：只显示窗口大小
  if (!usage) {
    if (!windowTokens) return null;
    return (
      <div className="mb-2 flex items-center gap-2 text-[11px] text-[var(--color-muted)]">
        <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-[var(--color-border)]" />
        <span>
          窗口 {windowTokens.toLocaleString()} token · 发一条消息后显示占用
        </span>
      </div>
    );
  }

  const win = usage.window_tokens || windowTokens || 1;
  const tools = usage.tools_tokens ?? 0;
  const system = usage.system_tokens ?? 0;
  // 【必须 clamp 到 0】。分项是按占比估的，四舍五入后可能比总数多 1~2，
  // 不 clamp 的话对话段会是负宽度，整条渐变错位。
  const convo = Math.max(0, usage.used_tokens - tools - system);

  const pct = (n: number) => (n / win) * 100;
  const share = (n: number) =>
    usage.used_tokens > 0 ? Math.round((n / usage.used_tokens) * 100) : 0;

  const near = usage.ratio > 0.75;

  const segments = [
    {
      key: "tools",
      label: "工具定义",
      tokens: tools,
      color: "var(--color-accent)",
      note: usage.tool_count ? `${usage.tool_count} 个工具` : "",
    },
    {
      key: "system",
      label: "系统提示词",
      tokens: system,
      color: "color-mix(in srgb, var(--color-accent) 45%, transparent)",
      note: "性格、行为规则、运行环境",
    },
    {
      key: "convo",
      label: "对话内容",
      tokens: convo,
      color: near ? "var(--color-warn)" : "var(--color-ok)",
      note: "消息、工具返回",
    },
  ].filter((s) => s.tokens > 0);

  return (
    <div className="mb-2 space-y-1 text-[11px] text-[var(--color-muted)]">
      {/* 分段条 */}
      <div
        className="flex h-1.5 overflow-hidden rounded-full bg-[var(--color-border)]"
        role="img"
        aria-label={
          `上下文占用 ${usage.used_tokens.toLocaleString()} / ` +
          `${win.toLocaleString()} token：` +
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

      {/* 图例 + 百分比。一直显示，不藏在 hover 里 ——
          用户不会去 hover 一个他认为是坏的数字。 */}
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-0.5">
        {segments.map((s) => (
          <span
            key={s.key}
            className="flex items-center gap-1"
            title={s.note ? `${s.label}：${s.note}` : s.label}
          >
            <span
              className="inline-block h-2 w-2 shrink-0 rounded-sm"
              style={{ background: s.color }}
              aria-hidden
            />
            {s.label} {s.tokens.toLocaleString()}
            <span className="opacity-70">({share(s.tokens)}%)</span>
          </span>
        ))}

        <span className="ml-auto tabular-nums">
          共 {usage.used_tokens.toLocaleString()} / {win.toLocaleString()} token
          {" · "}
          {Math.round(usage.ratio * 100)}%
          {/* 估算必须标出来。反过来同样要紧：真实值【不能】标成估算，
              否则用户会以为最可信的那个数字不可信。 */}
          {usage.is_estimate && "（估算）"}
        </span>
      </div>

      {/* 固定开销占大头时给一句可操作的话。
          只给数字的话用户知道"很大"但不知道能做什么。 */}
      {tools + system > usage.used_tokens * 0.6 && (
        <p className="opacity-80">
          前两项每一轮都会重发。想降下来：设置页关掉用不到的 MCP 服务器，
          或精简人格与偏好里的行为规则。
        </p>
      )}

      {near && (
        <p style={{ color: "var(--color-warn)" }}>
          接近窗口上限，超过 {Math.round((usage.compact_at / win) * 100)}%
          会自动压缩较早的消息。
        </p>
      )}
    </div>
  );
}
