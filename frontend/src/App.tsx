import { useEffect } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import Sidebar from "@/components/Sidebar";
import ChatPage from "@/pages/ChatPage";
import SettingsPage from "@/pages/SettingsPage";
import CronPage from "@/pages/CronPage";

export default function App() {
  const { data: meta } = useQuery({
    queryKey: ["meta"],
    queryFn: api.meta,
    // 元信息在设置页改动后需要刷新，但不用轮询
    staleTime: 30_000,
  });

  // 无鉴权却绑到非本机地址时警示。
  // 这个服务能执行任意命令，暴露到网络上等于把机器交出去。
  useEffect(() => {
    if (meta && !meta.host_is_localhost) {
      console.warn(
        "[安全] 服务绑定到非本机地址且无鉴权，任何能访问该端口的人都能执行命令",
      );
    }
  }, [meta]);

  return (
    <div className="flex h-full">
      <Sidebar />
      <main className="flex min-w-0 flex-1 flex-col">
        {meta && !meta.host_is_localhost && (
          <div
            role="alert"
            className="bg-[var(--color-err)]/15 border-b border-[var(--color-err)]/40 px-4 py-2 text-sm text-[var(--color-err)]"
          >
            服务已绑定到非本机地址且<strong>没有任何鉴权</strong>
            ——任何能访问该端口的人都能通过它执行命令、读写文件。请改回
            127.0.0.1。
          </div>
        )}
        {/*
          沙箱降级提示。

          【必须持续显示】而不是 toast —— 用户配了 docker 就是想要隔离，
          如果实际跑在宿主上，他需要【一直】知道这件事。
          一闪而过的提示看漏了的话，他会以为命令在容器里，
          于是放心地让 agent 执行危险操作。

          不可关闭：这不是"提醒"而是"当前状态"，
          能关掉的话就退化成了 toast。
        */}
        {meta?.sandbox_fallback_reason && (
          <div
            role="alert"
            className="border-b border-[var(--color-warn)]/40 bg-[var(--color-warn)]/15 px-4 py-2 text-sm text-[var(--color-warn)]"
          >
            已配置 Docker 沙箱但<strong>当前是非隔离环境</strong>
            ，命令直接在宿主上执行。原因：{meta.sandbox_fallback_reason}
          </div>
        )}
        <Routes>
          <Route path="/" element={<Navigate to="/chat" replace />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/chat/:sessionId" element={<ChatPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/cron" element={<CronPage />} />
          <Route
            path="*"
            element={
              <div className="p-8 text-[var(--color-muted)]">页面不存在</div>
            }
          />
        </Routes>
      </main>
    </div>
  );
}
