import { useEffect, useState } from "react";
import { NavLink, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Clock,
  Download,
  MessageSquarePlus,
  Pin,
  PinOff,
  Settings,
  Trash2,
} from "lucide-react";
import clsx from "clsx";
import { api } from "@/lib/api";
import { useChatStore } from "@/store/chat";

export default function Sidebar() {
  const nav = useNavigate();
  const { sessionId } = useParams();
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [exportErr, setExportErr] = useState<string | null>(null);

  const { data } = useQuery({
    queryKey: ["sessions", q],
    queryFn: () => api.listSessions({ size: 50, q: q || undefined }),
  });

  // 标题生成后刷新列表。
  //
  // ## 为什么需要这个
  //
  // 首轮结束时后端发 title 事件，store 里的 title 更新了 —— 但侧栏读的是
  // ["sessions"] 这个 query 的缓存，它不会因为 store 变化而重新拉。
  // 于是列表里一直显示"未命名会话"，要刷新页面或切一次会话才变。
  //
  // 监听 store 的 title 而不是在 store 里 invalidate：store 是纯状态层，
  // 不持有 queryClient；从这里订阅也更符合"谁用谁负责刷新"。
  const liveTitle = useChatStore((s) => s.title);
  useEffect(() => {
    if (!liveTitle) return;
    void qc.invalidateQueries({ queryKey: ["sessions"] });
  }, [liveTitle, qc]);

  const create = useMutation({
    mutationFn: () => api.createSession(),
    onSuccess: (s) => {
      void qc.invalidateQueries({ queryKey: ["sessions"] });
      nav(`/chat/${s.id}`);
    },
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteSession(id),
    onSuccess: (_r, id) => {
      void qc.invalidateQueries({ queryKey: ["sessions"] });
      if (id === sessionId) nav("/chat");
    },
  });

  /**
   * 置顶开关。
   *
   * 后端按 `pinned DESC, last_message_at DESC` 排序，所以切换后
   * 列表顺序会变 —— 必须 invalidate 而不是只改本地状态。
   */
  const togglePin = useMutation({
    mutationFn: ({ id, pinned }: { id: string; pinned: boolean }) =>
      api.patchSession(id, { pinned }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["sessions"] }),
  });

  /**
   * 导出。
   *
   * 失败要让用户看见 —— 静默失败的表现是"点了下载什么都没发生"，
   * 而用户会以为是浏览器的问题，去检查下载设置。
   */
  async function exportOne(id: string, fmt: "markdown" | "json") {
    setExportErr(null);
    try {
      await api.exportSession(id, fmt);
    } catch (e) {
      setExportErr(e instanceof Error ? e.message : "导出失败");
    }
  }

  return (
    <aside className="flex w-64 shrink-0 flex-col border-r border-[var(--color-border)] bg-[var(--color-surface)]">
      <div className="p-3">
        <button
          type="button"
          onClick={() => create.mutate()}
          disabled={create.isPending}
          className="flex w-full items-center justify-center gap-2 rounded-lg bg-[var(--color-accent)] px-3 py-2 text-sm font-medium text-white transition hover:brightness-110 disabled:opacity-50"
        >
          <MessageSquarePlus size={16} aria-hidden />
          新对话
        </button>
      </div>

      <div className="px-3 pb-2">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="搜索会话"
          aria-label="搜索会话"
          className="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5 text-sm outline-none placeholder:text-[var(--color-muted)] focus:border-[var(--color-accent)]"
        />
      </div>

      {exportErr && (
        <div className="mx-3 mb-2 rounded-md bg-[var(--color-err)]/10 px-2 py-1.5 text-[11px] text-[var(--color-err)]">
          导出失败：{exportErr}
          <button
            type="button"
            onClick={() => setExportErr(null)}
            className="ml-1 underline"
          >
            关闭
          </button>
        </div>
      )}

      <nav className="min-h-0 flex-1 overflow-y-auto px-2" aria-label="会话列表">
        {data?.items.length === 0 && (
          <p className="px-2 py-4 text-sm text-[var(--color-muted)]">
            还没有会话
          </p>
        )}
        <ul>
          {data?.items.map((s) => (
            <li key={s.id} className="group relative">
              <NavLink
                to={`/chat/${s.id}`}
                className={({ isActive }) =>
                  clsx(
                    // pr-20 给三个按钮留位（导出 + 置顶 + 删除）
                    "flex items-center gap-1 truncate rounded-md px-2.5 py-2 pr-20 text-sm transition",
                    isActive
                      ? "bg-[var(--color-surface-2)] text-[var(--color-text)]"
                      : "text-[var(--color-muted)] hover:bg-[var(--color-surface-2)]/60",
                  )
                }
                title={s.title || "未命名会话"}
              >
                {/* 置顶标记常显。只在 hover 时显示的话，用户看不出
                    为什么这个会话排在最前面 */}
                {s.pinned && (
                  <Pin
                    size={11}
                    aria-hidden
                    className="shrink-0 text-[var(--color-accent)]"
                  />
                )}
                <span className="truncate">{s.title || "未命名会话"}</span>
              </NavLink>
              {/* 三个按钮排成一行而不是各自 absolute 定位。
                  各自定位的话每加一个都要重算 right-* 和容器的 pr-*，
                  而侧边栏只有 256px，三个图标挤在一起容易点错。 */}
              <div className="absolute top-1/2 right-1 flex -translate-y-1/2 items-center gap-0.5 opacity-0 transition group-hover:opacity-100 focus-within:opacity-100">
                <button
                  type="button"
                  aria-label={`导出会话 ${s.title || "未命名会话"}`}
                  title="导出为 Markdown（按住 Alt 导出 JSON）"
                  onClick={(e) => {
                    e.preventDefault();
                    // Alt 切 JSON —— 放两个按钮太挤，而导出 JSON 是
                    // 备份场景，不需要一等入口
                    void exportOne(s.id, e.altKey ? "json" : "markdown");
                  }}
                  className="rounded p-1 text-[var(--color-muted)] transition hover:text-[var(--color-accent)]"
                >
                  <Download size={13} aria-hidden />
                </button>
                <button
                  type="button"
                  aria-label={
                    s.pinned
                      ? `取消置顶 ${s.title || "未命名会话"}`
                      : `置顶 ${s.title || "未命名会话"}`
                  }
                  title={s.pinned ? "取消置顶" : "置顶"}
                  onClick={(e) => {
                    e.preventDefault();
                    togglePin.mutate({ id: s.id, pinned: !s.pinned });
                  }}
                  className="rounded p-1 text-[var(--color-muted)] transition hover:text-[var(--color-accent)]"
                >
                  {s.pinned ? (
                    <PinOff size={13} aria-hidden />
                  ) : (
                    <Pin size={13} aria-hidden />
                  )}
                </button>
                <button
                  type="button"
                  aria-label={`删除会话 ${s.title || "未命名会话"}`}
                  title="删除"
                  onClick={(e) => {
                    e.preventDefault();
                    if (confirm(`删除会话「${s.title || "未命名"}」？不可恢复。`)) {
                      remove.mutate(s.id);
                    }
                  }}
                  className="rounded p-1 text-[var(--color-muted)] transition hover:text-[var(--color-err)]"
                >
                  <Trash2 size={14} aria-hidden />
                </button>
              </div>
            </li>
          ))}
        </ul>
      </nav>

      <div className="space-y-0.5 border-t border-[var(--color-border)] p-2">
        <NavLink
          to="/cron"
          className={({ isActive }) =>
            clsx(
              "flex items-center gap-2 rounded-md px-2.5 py-2 text-sm transition",
              isActive
                ? "bg-[var(--color-surface-2)]"
                : "text-[var(--color-muted)] hover:bg-[var(--color-surface-2)]/60",
            )
          }
        >
          <Clock size={16} aria-hidden />
          定时任务
        </NavLink>
        <NavLink
          to="/settings"
          className={({ isActive }) =>
            clsx(
              "flex items-center gap-2 rounded-md px-2.5 py-2 text-sm transition",
              isActive
                ? "bg-[var(--color-surface-2)]"
                : "text-[var(--color-muted)] hover:bg-[var(--color-surface-2)]/60",
            )
          }
        >
          <Settings size={16} aria-hidden />
          设置
        </NavLink>
      </div>
    </aside>
  );
}
