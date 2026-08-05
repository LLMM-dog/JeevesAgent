import { useEffect, useRef, useState } from "react";
import { Archive, Brain, ChevronRight, Loader2, RotateCcw } from "lucide-react";
import clsx from "clsx";
import { RecalledMemoriesBadge } from "./MemoryPanel";
import Markdown from "./Markdown";
import { SubAgentCards } from "./SubAgentCards";
import ToolCard from "./ToolCard";
import { useChatStore } from "@/store/chat";
import type { MessageOut } from "@/lib/types";

function Reasoning({ text, streaming }: { text: string; streaming: boolean }) {
  // 默认收起 —— 思维链通常很长，多数时候用户不关心
  const [open, setOpen] = useState(false);
  return (
    <div className="mb-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)]/60">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-[var(--color-muted)]"
      >
        <ChevronRight
          size={13}
          aria-hidden
          className={clsx("transition-transform", open && "rotate-90")}
        />
        <Brain size={13} aria-hidden />
        思考过程
        {streaming && <span className="text-[var(--color-accent)]">·</span>}
        <span className="ml-auto">{text.length} 字</span>
      </button>
      {open && (
        <div className="border-t border-[var(--color-border)] px-3 py-2 text-xs leading-relaxed whitespace-pre-wrap text-[var(--color-muted)]">
          {text}
        </div>
      )}
    </div>
  );
}

/**
 * 压缩摘要。
 *
 * 必须可展开看全文：压缩是"悄悄改变了模型记忆"的操作，用户后面发现
 * 模型忘了某个细节时，需要能查证摘要里到底留了什么。
 * 只显示"已压缩"而不给内容的话，这件事就无从排查。
 */
function SummaryBubble({ m }: { m: MessageOut }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-lg border border-dashed border-[var(--color-border)] bg-[var(--color-surface)]/40">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-[var(--color-muted)]"
      >
        <ChevronRight
          size={13}
          aria-hidden
          className={clsx("shrink-0 transition-transform", open && "rotate-90")}
        />
        <Archive size={13} aria-hidden className="shrink-0" />
        上下文已压缩
        <span className="ml-auto">{m.content.length} 字</span>
      </button>
      {open && (
        <div className="border-t border-[var(--color-border)] px-3 py-2">
          <Markdown text={m.content} />
        </div>
      )}
    </div>
  );
}

function UserBubble({ m }: { m: MessageOut }) {
  // 图片存在 attachments 里。它们只在发送那一轮进 LLM 请求，
  // 但一直留在界面上 —— 用户要能回看自己发了什么图。
  const images = (m.attachments ?? []).filter((a) => a.startsWith("data:image/"));
    return (
      <div className="group flex items-center justify-end gap-1.5">
        {/* 从这条重发。
            放在气泡【左侧】—— 右侧是气泡边缘贴着容器，按钮会被挤出去。
            只在 hover 显示：它是破坏性操作，常显会被误点。 */}
        <RetryButton m={m} />
        <div className="max-w-[85%] rounded-2xl rounded-br-md bg-[var(--color-accent)]/15 px-4 py-2.5">
          {images.length > 0 && (
            <div className="mb-1.5 flex flex-wrap gap-1.5">
              {images.map((url, i) => (
                <a key={i} href={url} target="_blank" rel="noreferrer">
                  <img
                    src={url}
                    alt={`发送的图片 ${i + 1}`}
                    className="max-h-40 rounded-lg border border-[var(--color-border)] object-cover"
                  />
                </a>
              ))}
            </div>
          )}
          {m.content && <div className="whitespace-pre-wrap">{m.content}</div>}
        </div>
      </div>
    );
  }

  /**
   * 从某条用户消息处重发。
   *
   * ## 流程
   *
   * 删掉该消息及其之后的全部 → 用原内容重新发一次。
   *
   * 这是 「消息节点化编辑」的简化版：不做分支树。分支树要额外的树形
   * 结构和 UI 上的分支切换器，而实际使用里"改掉重来"占绝大多数。
   *
   * ## 为什么要二次确认
   *
   * 删除不可恢复，且删的量可能很大（一条早期消息之后可能有几十轮）。
   * 确认框里说清会删多少条 —— 只说"确定重发吗"用户不知道代价。
   */
  function RetryButton({ m }: { m: MessageOut }) {
    const sessionId = useChatStore((s) => s.sessionId);
    const messages = useChatStore((s) => s.messages);
    const pending = useChatStore((s) => s.pending);
    const streaming = useChatStore((s) => s.streaming);
    const retryFrom = useChatStore((s) => s.retryFrom);
    const busy = pending || streaming !== null;
    if (!sessionId) return null;

    const idx = messages.findIndex((x) => x.id === m.id);
    // 后面还有多少条会被一起删掉
    const willDelete = idx >= 0 ? messages.length - idx : 0;

    return (
      <button
        type="button"
        // 生成中禁用 —— 后端也会拦（409 run_in_progress），
        // 但前端先拦掉能省一次失败请求
        disabled={busy}
        title={busy ? "生成中，无法重发" : "从这条重新发送"}
        aria-label="从这条消息重新发送"
        onClick={() => {
          const extra = willDelete - 1;
          const msg =
            extra > 0
              ? `从这条重新发送？会先删掉这条及其之后的 ${extra} 条消息，不可恢复。`
              : "重新发送这条消息？";
          if (confirm(msg)) void retryFrom(m.id);
        }}
        className="shrink-0 rounded p-1 text-[var(--color-muted)] opacity-0 transition group-hover:opacity-100 hover:text-[var(--color-accent)] focus-visible:opacity-100 disabled:cursor-not-allowed disabled:opacity-0"
      >
        <RotateCcw size={13} aria-hidden />
      </button>
    );
  }

function AssistantBubble({ m }: { m: MessageOut }) {
  return (
    <div className="max-w-[92%]">
      {m.reasoning && <Reasoning text={m.reasoning} streaming={false} />}
      {m.content && <Markdown text={m.content} />}
    </div>
  );
}

/** 已落库的 tool 消息（重新打开会话时看到的） */
function ToolBubble({ m }: { m: MessageOut }) {
  return (
    <ToolCard
      tool={{
        call_id: m.tool_call_id ?? m.id,
        tool_name: m.tool_name ?? "tool",
        args: {},
        status: m.is_error ? "error" : "ok",
        display: m.tool_display,
        content_preview: m.content.slice(0, 4000),
      }}
    />
  );
}

export default function MessageList() {
  const messages = useChatStore((s) => s.messages);
  const streaming = useChatStore((s) => s.streaming);
  const activeAgents = useChatStore((s) => s.activeAgents);
  const recalledMemories = useChatStore((s) => s.recalledMemories);
  const compacting = useChatStore((s) => s.compacting);
  const bottomRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  // 用户手动往上滚时不要强行拉回底部 —— 那让人无法阅读历史
  const pinnedRef = useRef(true);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const onScroll = () => {
      const gap = el.scrollHeight - el.scrollTop - el.clientHeight;
      pinnedRef.current = gap < 120;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    if (pinnedRef.current) {
      bottomRef.current?.scrollIntoView({ block: "end" });
    }
  });

  const visible = messages.filter(
    (m) => m.role !== "system" && m.role !== "artifact",
  );

  return (
    <div ref={containerRef} className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto max-w-3xl space-y-4 px-4 py-6">
        {visible.length === 0 && !streaming && (
          <div className="py-20 text-center text-[var(--color-muted)]">
            <p className="text-sm">开始一段对话</p>
            <p className="mt-1.5 text-xs">
              它能读写文件、执行命令，工作成果直接落在你的磁盘上
            </p>
          </div>
        )}

        {visible.map((m) => {
          if (m.role === "user") return <UserBubble key={m.id} m={m} />;
          if (m.role === "tool") return <ToolBubble key={m.id} m={m} />;
          if (m.role === "summary") return <SummaryBubble key={m.id} m={m} />;
          return <AssistantBubble key={m.id} m={m} />;
        })}

        {compacting && (
          <div
            role="status"
            className="flex items-center gap-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2 text-xs text-[var(--color-muted)]"
          >
            <Loader2 size={13} aria-hidden className="animate-spin" />
            正在压缩较早的 {compacting.victim_count} 条消息以腾出上下文空间…
          </div>
        )}

        {streaming && (
          <div className="max-w-[92%] space-y-2">
            <RecalledMemoriesBadge items={recalledMemories} />
            {streaming.reasoning && (
              <Reasoning text={streaming.reasoning} streaming />
            )}
            {streaming.tools.map((t) => (
              <ToolCard key={t.call_id} tool={t} />
            ))}
            {/* 子智能体卡片。它内部的工具调用挂在自己下面，
                不混进上面那个列表 —— 否则用户看到 researcher 读的
                12 个文件，会以为是主代理自己读的 */}
            <SubAgentCards agents={activeAgents} />
            {streaming.content ? (
              <div className="caret">
                <Markdown text={streaming.content} />
              </div>
            ) : (
              streaming.tools.length === 0 &&
              activeAgents.length === 0 &&
              !streaming.reasoning && (
                <p className="text-sm text-[var(--color-muted)]">思考中…</p>
              )
            )}
          </div>
        )}

        <div ref={bottomRef} />
      </div>
    </div>
  );
}
