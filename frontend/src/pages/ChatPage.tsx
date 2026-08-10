import { useEffect } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { AgentSwitcher } from "@/components/AgentSwitcher";
import { ModelSwitcher } from "@/components/ModelSwitcher";
import { ApprovalDialog } from "@/components/ApprovalDialog";
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

  const { data: meta } = useQuery({ queryKey: ["meta"], queryFn: api.meta });

  // 直接访问 /chat 时自动建一个会话，省掉一次点击
  const create = useMutation({
    mutationFn: () => api.createSession(),
    onSuccess: (s) => nav(`/chat/${s.id}`, { replace: true }),
  });

  useEffect(() => {
    if (!sessionId && !create.isPending && !create.isSuccess) {
      create.mutate();
    }
  }, [sessionId, create]);

  useEffect(() => {
    if (sessionId) void openSession(sessionId);
  }, [sessionId, openSession]);

  if (!sessionId) {
    return (
      <div className="flex flex-1 items-center justify-center text-sm text-[var(--color-muted)]">
        正在创建会话…
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
          <AgentSwitcher sessionId={sessionId} agentId={agentId} />
          <ModelSwitcher sessionId={sessionId} modelPk={modelPk} />
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
      {/* 审批框是模态的，放在最外层 —— 嵌在消息列表里会被滚动裁掉 */}
      <ApprovalDialog />
    </div>
  );
}
