import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  FileText,
  Loader2,
  RefreshCw,
  ToggleLeft,
  ToggleRight,
  Trash2,
  TriangleAlert,
  Upload,
} from "lucide-react";
import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "@/lib/api";
import { ApiError } from "@/lib/sse";
import type { AgentItem } from "@/lib/types";

/**
 * 技能管理面板。
 *
 * ## 为什么要显示诊断
 *
 * 常见实现把加载诊断只写进日志，用户在界面上看不到 —— 上传的技能
 * 因为缺 description 被跳过时，它就是凭空消失了，用户完全不知道为什么。
 *
 * 所以这里把诊断显示出来，并且说清是哪个文件、什么问题。
 */
export default function SkillsPanel() {
  const qc = useQueryClient();
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["skills"],
    queryFn: api.listSkills,
  });

  const refresh = () => void qc.invalidateQueries({ queryKey: ["skills"] });

  const upload = useMutation({
    mutationFn: ({ file, overwrite }: { file: File; overwrite: boolean }) =>
      api.uploadSkill(file, overwrite),
    onSuccess: (r) => {
      setErr(null);
      const parts = [`已安装 ${r.name}（${r.files} 个文件）`];
      if (r.skipped.length > 0) {
        // 跳过的文件要说清楚 —— 用户打包时带了不支持的类型，
        // 不告诉他的话技能可能少了关键文件而他不知道。
        parts.push(`跳过 ${r.skipped.length} 个不支持的文件`);
      }
      setNote(parts.join("，"));
      refresh();
    },
    onError: (e) => {
      setNote(null);
      if (e instanceof ApiError && e.status === 409) {
        setErr(`${e.message}。勾选"覆盖已存在"后重试`);
      } else {
        setErr(e instanceof Error ? e.message : "上传失败");
      }
    },
  });

  const reload = useMutation({
    mutationFn: api.reloadSkills,
    onSuccess: (r) => {
      setErr(null);
      setNote(`已重扫，共 ${r.count} 个技能`);
      refresh();
    },
  });

  const toggle = useMutation({
    mutationFn: (v: { name: string; enabled: boolean }) =>
      api.toggleSkill(v.name, v.enabled),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["skills"] });
      // 固定开销跟着变 —— 关掉一个技能就少了它的名字和描述，
      // 上下文条上的数字应该立刻反映这件事
      void qc.invalidateQueries({ queryKey: ["contextOverhead"] });
    },
  });

  const del = useMutation({
    mutationFn: api.deleteSkill,
    onSuccess: (r) => {
      setNote(`已删除 ${r.deleted}`);
      refresh();
    },
    onError: (e) => setErr(e instanceof Error ? e.message : "删除失败"),
  });

  const [overwrite, setOverwrite] = useState(false);

  const navigate = useNavigate();

  /** 查看某个技能被哪些智能体预设使用 */
  function AgentUsers({ skillName }: { skillName: string }) {
    const { data: agents, isLoading: loadingAgents } = useQuery({
      queryKey: ["agents", "using_skill", skillName],
      queryFn: () => api.agents.list({ using_skill: skillName }),
      enabled: !!skillName,
      staleTime: 30_000,
    });

    const removeAgent = useMutation({
      mutationFn: (agent: AgentItem) =>
        api.agents.update(agent.id, {
          skill_names: agent.skill_names.filter((n) => n !== skillName),
        }),
      onSuccess: () => {
        void qc.invalidateQueries({ queryKey: ["agents", "using_skill", skillName] });
        void qc.invalidateQueries({ queryKey: ["agents"] });
      },
    });

    if (loadingAgents) {
      return (
        <div className="mt-2 text-[11px] text-[var(--color-muted)]">
          加载引用信息…
        </div>
      );
    }

    if (!agents || agents.length === 0) {
      return (
        <div className="mt-2 text-[11px] text-[var(--color-muted)]">
          未被任何智能体预设使用
        </div>
      );
    }

    return (
      <div className="mt-2 border-t border-[var(--color-border)] pt-2">
        <div className="mb-1 text-[11px] font-medium text-[var(--color-muted)]">
          被 {agents.length} 个智能体预设使用：
        </div>
        <ul className="space-y-1">
          {agents.map((agent) => (
            <li
              key={agent.id}
              className="flex items-center gap-1.5 text-[11px]"
            >
              <span className="shrink-0">
                {agent.avatar ?? "🤖"}
              </span>
              <span className="min-w-0 flex-1 truncate font-medium">
                {agent.name}
              </span>
              <button
                type="button"
                onClick={() => navigate("/settings?tab=agents")}
                className="shrink-0 rounded px-1 py-0.5 text-[10px] text-[var(--color-accent)] hover:bg-[var(--color-accent)]/10 hover:underline"
                title="跳转到智能体设置页"
              >
                跳转
              </button>
              <button
                type="button"
                onClick={() => {
                  if (
                    confirm(
                      `从智能体「${agent.name}」中移除技能「${skillName}」？`,
                    )
                  ) {
                    removeAgent.mutate(agent);
                  }
                }}
                disabled={removeAgent.isPending}
                className="shrink-0 rounded px-1 py-0.5 text-[10px] text-[var(--color-err)] hover:bg-[var(--color-err)]/10 hover:underline disabled:opacity-50"
                title="从此智能体的预设中移除此技能"
              >
                {removeAgent.isPending ? "移除中…" : "移除"}
              </button>
            </li>
          ))}
        </ul>
      </div>
    );
  }

  return (
    <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <div className="mb-3 flex items-center gap-2">
        <h2 className="text-sm font-medium">技能</h2>
        <span className="text-xs text-[var(--color-muted)]">
          {data ? `${data.items.length} 个` : ""}
        </span>
        <button
          type="button"
          onClick={() => reload.mutate()}
          disabled={reload.isPending}
          className="ml-auto flex items-center gap-1 rounded-lg border border-[var(--color-border)] px-2 py-1 text-xs hover:bg-[var(--color-bg)]"
        >
          {reload.isPending ? (
            <Loader2 size={12} className="animate-spin" aria-hidden />
          ) : (
            <RefreshCw size={12} aria-hidden />
          )}
          重扫
        </button>
      </div>

      <p className="mb-3 text-xs text-[var(--color-muted)]">
        技能是一个含 SKILL.md 的目录。模型根据描述自己判断何时加载，
        只有名字和描述常驻上下文，正文按需读取。
      </p>

      <div className="mb-3 flex flex-wrap items-center gap-2">
        <input
          ref={fileRef}
          type="file"
          accept=".zip"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) upload.mutate({ file: f, overwrite });
            // 清空 value，否则选同一个文件不会再触发 change
            e.target.value = "";
          }}
        />
        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          disabled={upload.isPending}
          className="flex items-center gap-1.5 rounded-lg bg-[var(--color-accent)] px-3 py-1.5 text-xs text-white hover:brightness-110 disabled:opacity-50"
        >
          {upload.isPending ? (
            <Loader2 size={13} className="animate-spin" aria-hidden />
          ) : (
            <Upload size={13} aria-hidden />
          )}
          上传 zip
        </button>
        <label className="flex items-center gap-1.5 text-xs text-[var(--color-muted)]">
          <input
            type="checkbox"
            checked={overwrite}
            onChange={(e) => setOverwrite(e.target.checked)}
            className="rounded border-[var(--color-border)]"
          />
          覆盖已存在
        </label>
      </div>

      {err && (
        <div
          role="alert"
          className="mb-3 flex items-start gap-2 rounded-lg bg-[var(--color-err)]/10 px-3 py-2 text-xs text-[var(--color-err)]"
        >
          <TriangleAlert size={13} className="mt-0.5 shrink-0" aria-hidden />
          {err}
        </div>
      )}
      {note && (
        <div className="mb-3 rounded-lg bg-[var(--color-ok)]/10 px-3 py-2 text-xs text-[var(--color-ok)]">
          {note}
        </div>
      )}

      {isLoading ? (
        <div className="text-xs text-[var(--color-muted)]">加载中…</div>
      ) : !data || data.items.length === 0 ? (
        <div className="rounded-lg border border-dashed border-[var(--color-border)] px-3 py-6 text-center text-xs text-[var(--color-muted)]">
          还没有技能。上传一个 zip，或直接把技能目录放到 skills/ 下再点重扫。
        </div>
      ) : (
        <ul className="space-y-2">
          {data.items.map((s) => (
            <li
              key={s.name}
              className={`rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 ${
                s.enabled === false ? "opacity-55" : ""
              }`}
            >
              <div className="flex items-start gap-2">
                <FileText size={13} className="mt-0.5 shrink-0 text-[var(--color-muted)]" aria-hidden />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-medium">{s.name}</span>
                    {s.version && (
                      <span className="text-[10px] text-[var(--color-muted)]">
                        v{s.version}
                      </span>
                    )}
                    {s.enabled === false && (
                      <span className="rounded bg-[var(--color-border)] px-1 text-[10px] text-[var(--color-muted)]">
                        已关闭
                      </span>
                    )}
                    {/* 开关放在标题行右侧。
                        关掉的技能不进系统提示词 —— 也就是模型看不到它的
                        名字和描述，不会主动想起来用。但用户在消息里明确
                        点名时 load_skill 仍然读得到（那是 L2，按名字查表）。
                        开关控制的是常驻上下文成本，不是访问权限。 */}
                    <button
                      type="button"
                      onClick={() =>
                        toggle.mutate({
                          name: s.name,
                          enabled: s.enabled === false,
                        })
                      }
                      aria-pressed={s.enabled !== false}
                      title={
                        s.enabled === false
                          ? "已关闭：不会出现在系统提示词里。点击开启"
                          : "已启用：名字和描述常驻上下文。点击关闭以节省 token"
                      }
                      className="ml-auto shrink-0 text-[var(--color-muted)] hover:text-[var(--color-fg)]"
                    >
                      {s.enabled === false ? (
                        <ToggleLeft size={18} aria-hidden />
                      ) : (
                        <ToggleRight
                          size={18}
                          className="text-[var(--color-accent)]"
                          aria-hidden
                        />
                      )}
                    </button>
                  </div>
                  <p className="mt-0.5 text-xs text-[var(--color-muted)]">
                    {s.description}
                  </p>
                  {s.files.length > 0 ? (
                    <button
                      type="button"
                      onClick={() =>
                        setExpanded(expanded === s.name ? null : s.name)
                      }
                      className="mt-1 text-[11px] text-[var(--color-accent)] hover:underline"
                    >
                      {expanded === s.name ? "收起" : `${s.files.length} 个附件`}
                    </button>
                  ) : (
                    <button
                      type="button"
                      onClick={() =>
                        setExpanded(expanded === s.name ? null : s.name)
                      }
                      className="mt-1 text-[11px] text-[var(--color-accent)] hover:underline"
                    >
                      {expanded === s.name ? "收起" : "展开详情"}
                    </button>
                  )}
                  {expanded === s.name && (
                    <>
                      <ul className="mt-1 space-y-0.5">
                        {s.files.map((f) => (
                          <li
                            key={f}
                            className="font-mono text-[10px] text-[var(--color-muted)]"
                          >
                            {f}
                          </li>
                        ))}
                      </ul>
                      <AgentUsers skillName={s.name} />
                    </>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => {
                    if (confirm(`删除技能 ${s.name}？这会删掉整个目录。`)) {
                      del.mutate(s.name);
                    }
                  }}
                  aria-label={`删除 ${s.name}`}
                  className="shrink-0 rounded p-1 text-[var(--color-muted)] hover:bg-[var(--color-err)]/10 hover:text-[var(--color-err)]"
                >
                  <Trash2 size={13} aria-hidden />
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {data && data.diagnostics.length > 0 && (
        <div className="mt-3 rounded-lg bg-[var(--color-warn)]/10 px-3 py-2">
          <div className="mb-1 text-xs font-medium text-[var(--color-warn)]">
            {data.diagnostics.length} 个技能未能加载
          </div>
          <ul className="space-y-1">
            {data.diagnostics.map((d, i) => (
              <li key={i} className="text-[11px] text-[var(--color-warn)]">
                {d.message}
                <span className="ml-1 font-mono text-[10px] opacity-70">
                  {d.path}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
