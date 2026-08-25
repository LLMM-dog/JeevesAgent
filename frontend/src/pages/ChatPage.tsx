import { useEffect, useRef } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { AgentSwitcher } from "@/components/AgentSwitcher";
import { ModelSwitcher } from "@/components/ModelSwitcher";
import { WorkspaceSwitcher } from "@/components/WorkspaceSwitcher";
import { MemoryStatusIndicator } from "@/components/MemoryStatusIndicator";
import Banner from "@/components/Banner";
import Composer from "@/components/Composer";
import MessageList from "@/components/MessageList";
import TodoPanel from "@/components/TodoPanel";
import { api } from "@/lib/api";
import { useChatStore } from "@/store/chat";

export default function ChatPage() {
  const { sessionId } = useParams();
  const nav = useNavigate();
  const openSession = useChatStore((s) => s.openSession);
  const title = useChatStore((s) => s.title);
  const agentId = useChatStore((s) => s.agentId);
  const modelPk = useChatStore((s) => s.modelPk);
  const hasTriedCreate = useRef(false);

  const { data: meta } = useQuery({ queryKey: ["meta"], queryFn: api.meta });

  // 直接访问 /chat 时：有历史会话就进最近的一个，没有才新建。
  // 不能每次都新建 —— 否则每次打开应用都会多出一个空会话。
  const create = useMutation({
    mutationFn: () => api.createSession(),
    onSuccess: (s) => nav(`/chat/${s.id}`, { replace: true }),
  });

  // 最近的会话（列表按 last_message_at 倒序，items[0] 即上次会话）
  const { data: recentSessions } = useQuery({
    queryKey: ["sessions", "recent"],
    queryFn: () => api.listSessions({ size: 1 }),
  });

  useEffect(() => {
    // 只在首次渲染且没有 sessionId 时处理一次，防止无限循环
    if (!sessionId && !hasTriedCreate.current && recentSessions) {
      hasTriedCreate.current = true;
      if (recentSessions.items.length > 0) {
        nav(`/chat/${recentSessions.items[0].id}`, { replace: true });
      } else {
        create.mutate();
      }
    }
    // 当有 sessionId 时重置标记，允许下次再处理
    if (sessionId) {
      hasTriedCreate.current = false;
    }
  }, [sessionId, recentSessions]);

  useEffect(() => {
    if (sessionId) void openSession(sessionId);
  }, [sessionId, openSession]);

  if (!sessionId) {
    return (
      <div className="flex flex-1 items-center justify-center text-sm text-[var(--color-muted)]">
        加载中…
      </div>
    );
  }

  const noModel = meta && !meta.has_chat_model;

  return (
    <div className="flex min-h-0 flex-1">
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-3 border-b border-[var(--color-border)] px-4 py-2.5">
          <h1 className="truncate text-sm font-medium">
            {title || "新对话"}
          </h1>
          <WorkspaceSwitcher sessionId={sessionId} />
          <AgentSwitcher sessionId={sessionId} agentId={agentId} />
          <ModelSwitcher sessionId={sessionId} modelPk={modelPk} />
          <div className="ml-auto" />
          <MemoryStatusIndicator sessionId={sessionId} />
        </header>

        {noModel && (
          <div
            role="alert"
            className="mx-4 mt-3 rounded-lg border border-[var(--color-warn)]/40 bg-[var(--color-warn)]/10 px-3 py-2 text-sm text-[var(--color-warn)]"
          >
            还没有配置模型。
            <Link to="/settings" className="ml-1 underline">
              去设置页添加端点
            </Link>
            —— 填 base_url 和 API Key 即可自动拉取可用模型列表。
          </div>
        )}

        <Banner />
        <MessageList />
        <Composer disabled={noModel} />
      </div>
      <TodoPanel />
    </div>
  );
}
