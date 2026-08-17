/**
 * 记忆提取状态指示器（对话页 header 里的轻量提示）。
 *
 * 提取是后台异步任务，不阻塞对话。这里轮询状态、在提取时显示一个
 * 低调的小图标 +「记忆中…」，不提取时完全不出现 —— 不打扰用户。
 *
 * 提取完成（true → false）时刷新 token 占用条：归档后 load_context
 * 加载的消息减少，占用条里"对话内容"那段应该回落。后端没推归档事件，
 * 所以这里靠轮询到的状态变化来触发。
 *
 * 失败静默：retry=false，接口挂了不影响对话。
 */

import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Brain } from "lucide-react";

import { api } from "@/lib/api";
import { useChatStore } from "@/store/chat";

export function MemoryStatusIndicator({ sessionId }: { sessionId: string }) {
  const qc = useQueryClient();
  const prevExtracting = useRef(false);

  const { data } = useQuery({
    queryKey: ["memory-status", sessionId],
    queryFn: () => api.sessionMemoryStatus(sessionId),
    refetchInterval: 2000,
    retry: false,
    staleTime: 0,
  });

  const extracting = data?.extracting ?? false;

  useEffect(() => {
    if (prevExtracting.current && !extracting) {
      // 归档完成：清掉过期的 usage（占用条回落到固定开销），
      // 并重拉固定开销。下次对话的 context_usage 会给出归档后的真实值。
      useChatStore.setState({ usage: null });
      void qc.invalidateQueries({ queryKey: ["contextOverhead"] });
    }
    prevExtracting.current = extracting;
  }, [extracting, qc]);

  if (!extracting) return null;

  return (
    <span
      className="flex shrink-0 items-center gap-1.5 text-xs text-[var(--color-muted)]"
      title="正在后台整理对话记忆，不影响当前对话"
    >
      <Brain size={12} className="animate-pulse text-[var(--color-accent)]" />
      记忆中…
    </span>
  );
}
