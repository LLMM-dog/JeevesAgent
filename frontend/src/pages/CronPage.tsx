/**
 * 定时任务页。
 *
 * ## 三件必须做到的事
 *
 * 1. **cron 表达式实时预览**。表达式很容易写错且错得不明显 ——
 *    `0 9 * * 1` 到底是周一还是周日？看到接下来几次的具体时间就一目了然。
 *
 * 2. **明确告知强制 auto 审批**。定时任务里的 agent 能不经确认执行命令，
 *    这个风险必须在创建时就看到，不能埋在文档里。
 *
 * 3. **执行历史能点进会话**。任务是无人值守的，出问题时唯一的线索
 *    就是历史记录 + 那次对话。
 */

import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Clock,
  Play,
  Plus,
  Trash2,
} from "lucide-react";
import clsx from "clsx";
import { api } from "@/lib/api";

function fmt(ms: number): string {
  if (!ms) return "—";
  return new Date(ms).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

const STATUS_LABEL: Record<string, string> = {
  ok: "成功",
  failed: "失败",
  missed: "错过",
  running: "执行中",
};

const STATUS_CLASS: Record<string, string> = {
  ok: "text-[var(--color-ok)]",
  failed: "text-[var(--color-err)]",
  missed: "text-[var(--color-warn)]",
  running: "text-[var(--color-accent)]",
};

export default function CronPage() {
  const qc = useQueryClient();
  const [showForm, setShowForm] = useState(false);
  const [openTask, setOpenTask] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const { data } = useQuery({
    queryKey: ["cron-tasks"],
    queryFn: api.cronTasks,
    // 有任务在执行时状态会变，但不需要秒级刷新
    refetchInterval: 15_000,
  });

  const del = useMutation({
    mutationFn: api.deleteCronTask,
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["cron-tasks"] }),
    onError: (e: Error) => setErr(e.message),
  });

  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      api.patchCronTask(id, { enabled }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["cron-tasks"] }),
    onError: (e: Error) => setErr(e.message),
  });

  const runNow = useMutation({
    mutationFn: api.runCronTask,
    onSuccess: () => {
      // 立刻刷一次历史，让用户看到 running 状态
      void qc.invalidateQueries({ queryKey: ["cron-runs"] });
      void qc.invalidateQueries({ queryKey: ["cron-tasks"] });
    },
    onError: (e: Error) => setErr(e.message),
  });

  const items = data?.items ?? [];

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-6">
      <div className="mx-auto max-w-3xl">
        <header className="mb-5 flex items-center justify-between">
          <div>
            <h1 className="text-lg font-medium text-[var(--color-text)]">
              定时任务
            </h1>
            <p className="mt-0.5 text-xs text-[var(--color-muted)]">
              到点自动开一个新会话并把消息发给助手。
              {data ? `已装载 ${data.scheduler_loaded} 个` : ""}
              {data && data.scheduler_inflight > 0
                ? `，${data.scheduler_inflight} 个正在执行`
                : ""}
            </p>
          </div>
          <button
            type="button"
            onClick={() => setShowForm((v) => !v)}
            className="flex items-center gap-1.5 rounded-md bg-[var(--color-accent)] px-3 py-1.5 text-sm text-white transition hover:opacity-90"
          >
            <Plus size={14} aria-hidden />
            新建
          </button>
        </header>

        {err && (
          <div
            role="alert"
            className="mb-4 rounded-md bg-[var(--color-err)]/10 px-3 py-2 text-sm text-[var(--color-err)]"
          >
            {err}
            <button
              type="button"
              onClick={() => setErr(null)}
              className="ml-2 underline"
            >
              关闭
            </button>
          </div>
        )}

        {showForm && (
          <CreateForm
            onDone={() => {
              setShowForm(false);
              void qc.invalidateQueries({ queryKey: ["cron-tasks"] });
            }}
            onError={setErr}
          />
        )}

        {items.length === 0 && !showForm && (
          <p className="py-10 text-center text-sm text-[var(--color-muted)]">
            还没有定时任务
          </p>
        )}

        <ul className="space-y-2">
          {items.map((t) => (
            <li
              key={t.id}
              className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-3"
            >
              <div className="flex items-start gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span
                      className={clsx(
                        "truncate text-sm",
                        t.enabled
                          ? "text-[var(--color-text)]"
                          : "text-[var(--color-muted)] line-through",
                      )}
                    >
                      {t.name || t.cron_text}
                    </span>
                    <span className="shrink-0 rounded bg-[var(--color-surface-2)] px-1.5 py-0.5 text-[11px] text-[var(--color-muted)]">
                      {t.cron_text}
                    </span>
                    {t.fail_count > 0 && (
                      <span className="shrink-0 text-[11px] text-[var(--color-err)]">
                        失败 {t.fail_count} 次
                      </span>
                    )}
                  </div>
                  <p className="mt-1 line-clamp-2 text-xs text-[var(--color-muted)]">
                    {t.prompt}
                  </p>
                  <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-[var(--color-muted)]">
                    <span>下次 {fmt(t.next_fire_at)}</span>
                    <span>上次 {fmt(t.last_fired_at)}</span>
                    <span>共 {t.run_count} 次</span>
                    {t.on_missed === "run_once" && <span>错过会补跑</span>}
                  </div>
                </div>

                <div className="flex shrink-0 items-center gap-1">
                  <button
                    type="button"
                    aria-label={`立即执行 ${t.name || t.cron}`}
                    title="立即执行一次"
                    onClick={() => runNow.mutate(t.id)}
                    className="rounded p-1.5 text-[var(--color-muted)] transition hover:text-[var(--color-accent)]"
                  >
                    <Play size={14} aria-hidden />
                  </button>
                  <button
                    type="button"
                    aria-label={t.enabled ? "停用" : "启用"}
                    title={t.enabled ? "停用" : "启用"}
                    onClick={() =>
                      toggle.mutate({ id: t.id, enabled: !t.enabled })
                    }
                    className={clsx(
                      "rounded px-2 py-1 text-[11px] transition",
                      t.enabled
                        ? "bg-[var(--color-ok)]/15 text-[var(--color-ok)]"
                        : "bg-[var(--color-surface-2)] text-[var(--color-muted)]",
                    )}
                  >
                    {t.enabled ? "已启用" : "已停用"}
                  </button>
                  <button
                    type="button"
                    aria-label={`删除 ${t.name || t.cron}`}
                    title="删除"
                    onClick={() => {
                      if (confirm(`删除任务「${t.name || t.cron}」？`)) {
                        del.mutate(t.id);
                      }
                    }}
                    className="rounded p-1.5 text-[var(--color-muted)] transition hover:text-[var(--color-err)]"
                  >
                    <Trash2 size={14} aria-hidden />
                  </button>
                </div>
              </div>

              <button
                type="button"
                onClick={() => setOpenTask(openTask === t.id ? null : t.id)}
                className="mt-2 text-[11px] text-[var(--color-muted)] underline"
              >
                {openTask === t.id ? "收起历史" : "执行历史"}
              </button>
              {openTask === t.id && <RunList taskId={t.id} />}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function RunList({ taskId }: { taskId: string }) {
  const { data } = useQuery({
    queryKey: ["cron-runs", taskId],
    queryFn: () => api.cronRuns(taskId),
    refetchInterval: 10_000,
  });
  const runs = data?.items ?? [];

  if (runs.length === 0) {
    return (
      <p className="mt-2 text-[11px] text-[var(--color-muted)]">还没有执行记录</p>
    );
  }

  return (
    <ul className="mt-2 space-y-1 border-t border-[var(--color-border)] pt-2">
      {runs.map((r) => (
        <li key={r.id} className="flex items-center gap-2 text-[11px]">
          <span className="w-24 shrink-0 text-[var(--color-muted)]">
            {fmt(r.scheduled_at)}
          </span>
          <span
            className={clsx(
              "w-12 shrink-0",
              STATUS_CLASS[r.status] ?? "text-[var(--color-muted)]",
            )}
          >
            {STATUS_LABEL[r.status] ?? r.status}
          </span>
          {/* 能点进会话很重要 —— 任务是无人值守的，
              出问题时唯一的线索就是那次对话 */}
          {r.session_id ? (
            <Link
              to={`/chat/${r.session_id}`}
              className="shrink-0 text-[var(--color-accent)] underline"
            >
              查看对话
            </Link>
          ) : (
            <span className="shrink-0 text-[var(--color-muted)]">—</span>
          )}
          {r.detail && (
            <span className="truncate text-[var(--color-muted)]">
              {r.detail}
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}

function CreateForm({
  onDone,
  onError,
}: {
  onDone: () => void;
  onError: (m: string) => void;
}) {
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [cron, setCron] = useState("0 9 * * *");
  const [onMissed, setOnMissed] = useState("skip");

  // cron 实时校验与预览。
  //
  // 表达式很容易写错且错得不明显 —— 看到接下来几次的具体时间
  // 比看着 "0 9 * * 1" 猜"周一还是周日"可靠得多。
  const { data: preview } = useQuery({
    queryKey: ["cron-validate", cron],
    queryFn: () => api.validateCron(cron),
    enabled: cron.trim().length > 0,
  });

  const create = useMutation({
    mutationFn: () =>
      api.createCronTask({ name, prompt, cron, on_missed: onMissed }),
    onSuccess: onDone,
    onError: (e: Error) => onError(e.message),
  });

  const canSubmit = prompt.trim().length > 0 && preview?.valid === true;

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (canSubmit) create.mutate();
      }}
      className="mb-4 space-y-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4"
    >
      <div>
        <label
          htmlFor="cron-name"
          className="mb-1 block text-xs text-[var(--color-muted)]"
        >
          名字（可留空）
        </label>
        <input
          id="cron-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="每日日报"
          className="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5 text-sm"
        />
      </div>

      <div>
        <label
          htmlFor="cron-prompt"
          className="mb-1 block text-xs text-[var(--color-muted)]"
        >
          到点后发给助手的消息
        </label>
        <textarea
          id="cron-prompt"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          rows={3}
          placeholder="看一下 workspace 里今天改过的文件，总结成一份日报"
          className="w-full resize-y rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5 text-sm"
        />
      </div>

      <div>
        <label
          htmlFor="cron-expr"
          className="mb-1 block text-xs text-[var(--color-muted)]"
        >
          cron 表达式（分 时 日 月 周）
        </label>
        <input
          id="cron-expr"
          value={cron}
          onChange={(e) => setCron(e.target.value)}
          className="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5 font-mono text-sm"
        />
        {/* 预览。写错了立刻看到，而不是等到明天发现任务没跑 */}
        {preview && (
          <div className="mt-1.5 text-[11px]">
            {preview.valid ? (
              <div className="text-[var(--color-muted)]">
                <span className="text-[var(--color-ok)]">{preview.text}</span>
                {" — 接下来："}
                {preview.next.slice(0, 3).map((n) => fmt(n)).join("、")}
              </div>
            ) : (
              <div className="text-[var(--color-err)]">{preview.error}</div>
            )}
          </div>
        )}
      </div>

      <div>
        <label
          htmlFor="cron-missed"
          className="mb-1 block text-xs text-[var(--color-muted)]"
        >
          服务没运行时错过了怎么办
        </label>
        <select
          id="cron-missed"
          value={onMissed}
          onChange={(e) => setOnMissed(e.target.value)}
          className="rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5 text-sm"
        >
          <option value="skip">跳过（只记录）</option>
          <option value="run_once">启动后补跑一次</option>
        </select>
        <p className="mt-1 text-[11px] text-[var(--color-muted)]">
          补跑只在错过 6 小时内有效 —— 昨天的日报今天补出来意义不大。
        </p>
      </div>

      {/* 【必须显眼】——定时任务里的 agent 能不经确认执行命令。
          这个风险不能埋在文档里 */}
      <div className="flex gap-2 rounded-md bg-[var(--color-warn)]/10 px-3 py-2 text-[11px] text-[var(--color-warn)]">
        <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden />
        <span>
          定时任务触发的会话会<strong>自动批准所有工具调用</strong>
          （包括执行命令、写文件）——因为触发时没有人在旁边点确认。
          请确认这条消息不会让助手做危险操作。
        </span>
      </div>

      <div className="flex items-center gap-2">
        <button
          type="submit"
          disabled={!canSubmit || create.isPending}
          className="flex items-center gap-1.5 rounded-md bg-[var(--color-accent)] px-3 py-1.5 text-sm text-white transition hover:opacity-90 disabled:opacity-40"
        >
          <Clock size={14} aria-hidden />
          {create.isPending ? "创建中…" : "创建"}
        </button>
        <button
          type="button"
          onClick={onDone}
          className="rounded-md px-3 py-1.5 text-sm text-[var(--color-muted)] transition hover:text-[var(--color-text)]"
        >
          取消
        </button>
      </div>
    </form>
  );
}
