import { useEffect, useState } from "react";
import { NavLink, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Clock,
  Download,
  ListChecks,
  MessageSquarePlus,
  PanelLeftClose,
  PanelLeftOpen,
  Pencil,
  Pin,
  PinOff,
  Settings,
  Trash2,
} from "lucide-react";
import clsx from "clsx";
import { api } from "@/lib/api";
import { useChatStore } from "@/store/chat";
import { useAuth } from "@/store/auth";
import { LogOut } from "lucide-react";

export default function Sidebar() {
  const nav = useNavigate();
  const { sessionId } = useParams();
  const qc = useQueryClient();
  const authEnabled = useAuth((s) => s.authEnabled);
  const username = useAuth((s) => s.username);
  const logout = useAuth((s) => s.logout);
  const [q, setQ] = useState("");
  const [exportErr, setExportErr] = useState<string | null>(null);
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [batchNote, setBatchNote] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(false);

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
      
      // 如果删除的是当前会话，导航到最近的会话
      if (id === sessionId) {
        const sessions = data?.items || [];
        // 找到下一个会话（排除刚删除的）
        const nextSession = sessions.find(s => s.id !== id);
        if (nextSession) {
          nav(`/chat/${nextSession.id}`);
        } else {
          // 没有其他会话，自动创建一个空会话并跳转
          create.mutate();
        }
      }
    },
  });

  /**
   * 批量删除。结果区分成功 / 失败 / 不存在，分别反馈 ——
   * 部分失败时用户要知道是哪几条、为什么。
   */
  const batchDelete = useMutation({
    mutationFn: (ids: string[]) => api.batchDeleteSessions(ids),
    onSuccess: (r) => {
      const parts: string[] = [];
      if (r.succeeded.length) parts.push(`成功 ${r.succeeded.length}`);
      if (r.failed.length) parts.push(`失败 ${r.failed.length}`);
      if (r.not_found.length) parts.push(`不存在 ${r.not_found.length}`);
      setBatchNote(parts.length ? `批量删除：${parts.join("，")}` : "批量删除完成");
      if (r.failed.length > 0) {
        setBatchNote(
          (prev) => `${prev}（${r.failed[0].session_id}：${r.failed[0].error}）`,
        );
      }
      setSelected(new Set());
      setSelectMode(false);
      void qc.invalidateQueries({ queryKey: ["sessions"] });
      
      // 当前会话被删了，导航到最近的会话
      if (sessionId && r.succeeded.includes(sessionId)) {
        const sessions = data?.items || [];
        // 找到下一个会话（排除已删除的）
        const nextSession = sessions.find(s => !r.succeeded.includes(s.id));
        if (nextSession) {
          nav(`/chat/${nextSession.id}`);
        } else {
          // 没有其他会话，自动创建一个空会话并跳转
          create.mutate();
        }
      }
    },
    onError: (e: Error) => setBatchNote(`批量删除失败：${e.message}`),
  });

  const allIds = (data?.items ?? []).map((s) => s.id);
  const allSelected = allIds.length > 0 && allIds.every((id) => selected.has(id));

  const toggleSelect = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const exitSelect = () => {
    setSelectMode(false);
    setSelected(new Set());
  };

  const confirmBatchDelete = () => {
    if (selected.size === 0) return;
    if (
      confirm(
        `删除选中的 ${selected.size} 个会话？\n\n此操作不可恢复，会连同会话里的所有消息一起删除。`,
      )
    ) {
      batchDelete.mutate([...selected]);
    }
  };

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

  const renameSession = useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) =>
      api.patchSession(id, { title }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["sessions"] });
    },
    onError: (e: Error) => alert(e.message),
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

  // 收起态：只留一条窄栏 + 展开按钮
  if (collapsed) {
    return (
      <aside className="flex w-12 shrink-0 flex-col items-center border-r border-[var(--color-border)] bg-[var(--color-surface)] py-3">
        <button
          type="button"
          onClick={() => setCollapsed(false)}
          title="展开侧边栏"
          aria-label="展开侧边栏"
          className="rounded p-1.5 text-[var(--color-muted)] transition hover:bg-[var(--color-surface-2)] hover:text-[var(--color-text)]"
        >
          <PanelLeftOpen size={18} aria-hidden />
        </button>
      </aside>
    );
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

      <div className="flex items-center gap-1.5 px-3 pb-2">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="搜索会话"
          aria-label="搜索会话"
          className="min-w-0 flex-1 rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5 text-sm outline-none placeholder:text-[var(--color-muted)] focus:border-[var(--color-accent)]"
        />
        <button
          type="button"
          onClick={() => {
            if (selectMode) exitSelect();
            else {
              setSelectMode(true);
              setBatchNote(null);
            }
          }}
          aria-pressed={selectMode}
          title={selectMode ? "退出多选" : "多选会话"}
          className={clsx(
            "shrink-0 rounded-md border p-1.5 transition",
            selectMode
              ? "border-[var(--color-accent)] text-[var(--color-accent)]"
              : "border-[var(--color-border)] text-[var(--color-muted)] hover:text-[var(--color-text)]",
          )}
        >
          <ListChecks size={15} aria-hidden />
        </button>
      </div>

      {/* 多选工具栏 */}
      {selectMode && (
        <div className="mb-1 flex items-center gap-2 px-3">
          <label className="flex cursor-pointer items-center gap-1.5 text-xs text-[var(--color-muted)]">
            <input
              type="checkbox"
              checked={allSelected}
              onChange={() => setSelected(allSelected ? new Set() : new Set(allIds))}
              className="shrink-0 accent-[var(--color-accent)]"
            />
            全选
          </label>
          <span className="text-xs text-[var(--color-muted)]">已选 {selected.size} 项</span>
          <div className="ml-auto flex items-center gap-2">
            <button
              type="button"
              onClick={exitSelect}
              className="text-xs text-[var(--color-muted)] hover:text-[var(--color-text)]"
            >
              取消
            </button>
            <button
              type="button"
              onClick={confirmBatchDelete}
              disabled={selected.size === 0 || batchDelete.isPending}
              className="rounded-md bg-[var(--color-err)] px-2 py-1 text-xs font-medium text-white transition hover:brightness-110 disabled:opacity-40"
            >
              {batchDelete.isPending ? "删除中…" : "删除"}
            </button>
          </div>
        </div>
      )}

      {batchNote && (
        <div className="mx-3 mb-2 rounded-md bg-[var(--color-surface-2)] px-2 py-1.5 text-[11px] text-[var(--color-muted)]">
          {batchNote}
          <button type="button" onClick={() => setBatchNote(null)} className="ml-1 underline">
            关闭
          </button>
        </div>
      )}

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
              {selectMode ? (
                <button
                  type="button"
                  onClick={() => toggleSelect(s.id)}
                  className={clsx(
                    "flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-sm transition",
                    selected.has(s.id)
                      ? "bg-[var(--color-surface-2)] text-[var(--color-text)]"
                      : "text-[var(--color-muted)] hover:bg-[var(--color-surface-2)]/60",
                  )}
                >
                  <input
                    type="checkbox"
                    checked={selected.has(s.id)}
                    readOnly
                    className="shrink-0 accent-[var(--color-accent)]"
                  />
                  {s.pinned && (
                    <Pin size={11} aria-hidden className="shrink-0 text-[var(--color-accent)]" />
                  )}
                  <span className="truncate">{s.title || "未命名会话"}</span>
                </button>
              ) : (
                <>
                  <NavLink
                    to={`/chat/${s.id}`}
                    className={({ isActive }) =>
                      clsx(
                        // pr-28 给四个按钮留位（重命名 + 导出 + 置顶 + 删除）
                        "flex items-center gap-1 truncate rounded-md px-2.5 py-2 pr-28 text-sm transition",
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
                  {/* 四个按钮排成一行而不是各自 absolute 定位。
                      各自定位的话每加一个都要重算 right-* 和容器的 pr-*，
                      而侧边栏只有 256px，四个图标挤在一起容易点错。 */}
                  <div className="absolute top-1/2 right-1 flex -translate-y-1/2 items-center gap-0.5 opacity-0 transition group-hover:opacity-100 focus-within:opacity-100">
                    <button
                      type="button"
                      aria-label={`重命名会话 ${s.title || "未命名会话"}`}
                      title="重命名"
                      onClick={(e) => {
                        e.preventDefault();
                        const next = window.prompt(
                          "输入会话标题",
                          s.title || "未命名会话",
                        );
                        if (next != null && next.trim() && next.trim() !== s.title) {
                          renameSession.mutate({ id: s.id, title: next.trim() });
                        }
                      }}
                      className="rounded p-1 text-[var(--color-muted)] transition hover:text-[var(--color-accent)]"
                    >
                      <Pencil size={13} aria-hidden />
                    </button>
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
                </>
              )}
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
        <button
          type="button"
          onClick={() => setCollapsed(true)}
          title="收起侧边栏"
          className="flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-sm text-[var(--color-muted)] transition hover:bg-[var(--color-surface-2)]/60"
        >
          <PanelLeftClose size={16} aria-hidden />
          收起
        </button>
        {authEnabled && (
          <button
            type="button"
            onClick={() => void logout()}
            title="退出登录"
            className="flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-sm text-[var(--color-muted)] transition hover:bg-[var(--color-surface-2)]/60"
          >
            <LogOut size={16} aria-hidden />
            退出登录（{username || "?"}）
          </button>
        )}
      </div>
    </aside>
  );
}
