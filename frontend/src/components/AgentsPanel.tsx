import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, EyeOff, Pencil, Plus, Trash2 } from "lucide-react";
import clsx from "clsx";

import { api } from "../lib/api";
import type { AgentItem, ModelItem } from "../lib/types";

/**
 * 智能体管理面板。
 *
 * ## 卡片布局
 *
 * 每张卡片只显示：头像 + 名称 + 描述 + 可见性滑块 + [编辑] 按钮。
 * 不显示模型、权限等详情 —— 编辑弹窗里看。
 *
 * ## 可见性 slider
 *
 * 控制的是对话页 AgentSwitcher 下拉里是否出现，不是启用/禁用。
 * 智能体没有"禁用"概念 —— hidden=true 就是不出现。
 */

// ── 编辑弹窗 ──

function AgentEditor({
  agent,
  models,
  skills,
  mcpServers,
  onDone,
}: {
  agent: AgentItem | null; // null = 新建
  models: ModelItem[];
  skills: { name: string; description: string }[];
  mcpServers: { server_id: string; status: string; tool_count: number }[];
  onDone: () => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState(agent?.name ?? "");
  const [description, setDescription] = useState(agent?.description ?? "");
  const [avatar, setAvatar] = useState(agent?.avatar ?? "");
  const [systemPrompt, setSystemPrompt] = useState(agent?.system_prompt ?? "");
  const [modelId, setModelId] = useState(agent?.model_id ?? "");
  const [hidden, setHidden] = useState(agent?.hidden ?? false);
  const [skillNames, setSkillNames] = useState<string[]>(agent?.skill_names ?? []);
  const [mcpServerIds, setMcpServerIds] = useState<string[]>(agent?.mcp_servers ?? []);
  const [verificationEnabled, setVerificationEnabled] = useState(
    agent?.verification_enabled ?? true,
  );
  const [strictMode, setStrictMode] = useState(agent?.strict_mode ?? false);
  const [permissions, setPermissions] = useState({
    permission_read: agent?.permission_read ?? true,
    permission_write: agent?.permission_write ?? false,
    permission_shell: agent?.permission_shell ?? false,
    permission_network: agent?.permission_network ?? false,
    permission_subagent: agent?.permission_subagent ?? false,
  });
  const [err, setErr] = useState("");

  const save = useMutation({
    mutationFn: () => {
      const data = {
        name,
        description,
        avatar: avatar || null,
        system_prompt: systemPrompt,
        model_id: modelId || null,
        skill_names: skillNames,
        mcp_servers: mcpServerIds,
        hidden,
        verification_enabled: verificationEnabled,
        strict_mode: strictMode,
        ...permissions,
      };
      if (agent) {
        return api.agents.update(agent.id, data);
      }
      return api.agents.create(data);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agents"], exact: false });
      onDone();
    },
    onError: (e: Error) => setErr(e.message),
  });

  const permToggle = (key: keyof typeof permissions) => {
    setPermissions((p) => ({ ...p, [key]: !p[key] }));
  };

  return (
    <div className="space-y-4">
      <h3 className="text-sm font-medium">
        {agent ? `编辑 ${agent.name}` : "新建智能体"}
      </h3>

      {err && (
        <p
          role="alert"
          className="rounded border px-3 py-2 text-sm"
          style={{ borderColor: "var(--color-err)", color: "var(--color-err)" }}
        >
          {err}
        </p>
      )}

      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block">
          <span className="mb-1 block text-xs text-[var(--color-muted)]">名称</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="我的助手"
            className="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5 text-sm outline-none focus:border-[var(--color-accent)]"
          />
        </label>

        <label className="block">
          <span className="mb-1 block text-xs text-[var(--color-muted)]">
            头像（emoji）
          </span>
          <input
            value={avatar}
            onChange={(e) => setAvatar(e.target.value)}
            placeholder="🤖"
            className="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5 text-sm outline-none focus:border-[var(--color-accent)]"
          />
        </label>
      </div>

      <label className="block">
        <span className="mb-1 block text-xs text-[var(--color-muted)]">描述</span>
        <input
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="擅长编写 Python 代码的助手"
          className="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5 text-sm outline-none focus:border-[var(--color-accent)]"
        />
      </label>

      <label className="block">
        <span className="mb-1 block text-xs text-[var(--color-muted)]">使用模型</span>
        <select
          value={modelId}
          onChange={(e) => setModelId(e.target.value)}
          className="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5 text-sm outline-none focus:border-[var(--color-accent)]"
        >
          <option value="">（不指定，跟随全局绑定）</option>
          {models.map((m) => (
            <option key={m.id} value={m.id}>
              {m.display_name || m.model_id}
              {" · "}
              {(m.context_window / 1000).toFixed(0)}K
            </option>
          ))}
        </select>
      </label>

      <label className="block">
        <span className="mb-1 block text-xs text-[var(--color-muted)]">
          系统提示词
        </span>
        <textarea
          value={systemPrompt}
          onChange={(e) => setSystemPrompt(e.target.value)}
          placeholder="你是一个专业的 Python 开发者..."
          rows={5}
          className="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5 text-sm outline-none focus:border-[var(--color-accent)]"
        />
      </label>

      {/* ── 技能 ── */}
      <div>
        <span className="mb-1 block text-xs text-[var(--color-muted)]">技能</span>
        {skills.length === 0 ? (
          <p className="text-xs text-[var(--color-muted)]">暂无已安装技能。在「技能」标签页上传。</p>
        ) : (
          <div className="max-h-32 space-y-1 overflow-y-auto rounded border border-[var(--color-border)] p-2">
            {skills.map((s) => (
              <label key={s.name} className="flex items-center gap-1.5 text-xs cursor-pointer">
                <input
                  type="checkbox"
                  checked={skillNames.includes(s.name)}
                  onChange={() =>
                    setSkillNames((prev) =>
                      prev.includes(s.name)
                        ? prev.filter((n) => n !== s.name)
                        : [...prev, s.name],
                    )
                  }
                  className="shrink-0 accent-[var(--color-accent)]"
                />
                <span className="truncate">{s.name}</span>
              </label>
            ))}
          </div>
        )}
      </div>

      {/* ── MCP 服务器 ── */}
      <div>
        <span className="mb-1 block text-xs text-[var(--color-muted)]">MCP 服务器</span>
        {mcpServers.length === 0 ? (
          <p className="text-xs text-[var(--color-muted)]">暂无 MCP 服务器。在「MCP 服务器」标签页添加。</p>
        ) : (
          <div className="max-h-32 space-y-1 overflow-y-auto rounded border border-[var(--color-border)] p-2">
            {mcpServers.map((m) => (
              <label key={m.server_id} className="flex items-center gap-1.5 text-xs cursor-pointer">
                <input
                  type="checkbox"
                  checked={mcpServerIds.includes(m.server_id)}
                  onChange={() =>
                    setMcpServerIds((prev) =>
                      prev.includes(m.server_id)
                        ? prev.filter((id) => id !== m.server_id)
                        : [...prev, m.server_id],
                    )
                  }
                  className="shrink-0 accent-[var(--color-accent)]"
                />
                <span className="truncate">{m.server_id}</span>
                <span className="text-[var(--color-muted)]">({m.tool_count} 工具)</span>
              </label>
            ))}
          </div>
        )}
      </div>

      {/* 权限 */}
      <fieldset className="rounded-lg border border-[var(--color-border)] p-3">
        <legend className="px-1 text-xs text-[var(--color-muted)]">工具权限</legend>
        <div className="flex flex-wrap gap-3">
          {(
            [
              ["permission_read", "读文件"],
              ["permission_write", "写文件"],
              ["permission_shell", "终端"],
              ["permission_network", "联网"],
              ["permission_subagent", "子智能体"],
            ] as const
          ).map(([key, label]) => (
            <label
              key={key}
              className="flex items-center gap-1.5 text-xs cursor-pointer"
            >
              <input
                type="checkbox"
                checked={permissions[key]}
                onChange={() => permToggle(key)}
                className="shrink-0 accent-[var(--color-accent)]"
              />
              {label}
            </label>
          ))}
        </div>
      </fieldset>

      {/* 开关 */}
      <div className="flex flex-wrap gap-4">
        <label className="flex items-center gap-2 text-xs cursor-pointer">
          <input
            type="checkbox"
            checked={!hidden}
            onChange={() => setHidden((v) => !v)}
            className="shrink-0 accent-[var(--color-accent)]"
          />
          在对话页显示
        </label>

        <label className="flex items-center gap-2 text-xs cursor-pointer">
          <input
            type="checkbox"
            checked={verificationEnabled}
            onChange={() => setVerificationEnabled((v) => !v)}
            className="shrink-0 accent-[var(--color-accent)]"
          />
          验证增强
        </label>

        <label className="flex items-center gap-2 text-xs cursor-pointer">
          <input
            type="checkbox"
            checked={strictMode}
            onChange={() => setStrictMode((v) => !v)}
            className="shrink-0 accent-[var(--color-accent)]"
          />
          严格模式
        </label>
      </div>

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => save.mutate()}
          disabled={!name || save.isPending}
          className="rounded-lg bg-[var(--color-accent)] px-3 py-1.5 text-sm font-medium text-white transition hover:brightness-110 disabled:opacity-50"
        >
          {save.isPending ? "保存中…" : agent ? "保存修改" : "创建"}
        </button>
        <button
          type="button"
          onClick={onDone}
          className="rounded-lg border border-[var(--color-border)] px-3 py-1.5 text-sm text-[var(--color-muted)] hover:bg-[var(--color-surface-2)]"
        >
          取消
        </button>
      </div>
    </div>
  );
}

// ── 主面板 ──

export default function AgentsPanel() {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<AgentItem | null>(null); // null = 关，AgentItem = 编辑，new = 新建标记
  const [isNew, setIsNew] = useState(false);
  const [err, setErr] = useState("");

  const { data: agents } = useQuery({
    queryKey: ["agents", "all"],
    queryFn: () => api.agents.list(),
  });

  const { data: modelsData } = useQuery({
    queryKey: ["models", "all"],
    queryFn: () => api.models(),
  });

  const { data: skillsData } = useQuery({
    queryKey: ["skills"],
    queryFn: () => api.listSkills(),
  });

  const { data: mcpData } = useQuery({
    queryKey: ["mcp", "servers"],
    queryFn: () => api.mcpServers(),
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["agents"], exact: false });
  };

  const toggleHidden = useMutation({
    mutationFn: (a: AgentItem) =>
      api.agents.update(a.id, { hidden: !a.hidden }),
    onSuccess: invalidate,
    onError: (e: Error) => setErr(e.message),
  });

  const delAgent = useMutation({
    mutationFn: (id: string) => api.agents.delete(id),
    onSuccess: invalidate,
    onError: (e: Error) => setErr(e.message),
  });

  const allAgents = agents ?? [];
  const allModels = modelsData?.items ?? [];
  const allSkills = skillsData?.items ?? [];
  const allMcp = mcpData?.items ?? [];

  const openNew = () => {
    setEditing(null);
    setIsNew(true);
  };

  const openEdit = (a: AgentItem) => {
    setEditing(a);
    setIsNew(false);
  };

  const closeEditor = () => {
    setEditing(null);
    setIsNew(false);
  };

  return (
    <section className="space-y-4">
      <header className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-medium">智能体</h2>
          <p className="mt-1 text-sm text-[var(--color-muted)]">
            创建不同的智能体来应对不同场景。每个智能体可以绑定不同的模型、系统提示词和工具权限。
            <br />
            可见的智能体会出现在对话页的快捷切换菜单里。
          </p>
        </div>
        <button
          type="button"
          onClick={openNew}
          className="flex shrink-0 items-center gap-1.5 rounded-lg bg-[var(--color-accent)] px-3 py-1.5 text-sm font-medium text-white transition hover:brightness-110"
        >
          <Plus size={14} />
          新建智能体
        </button>
      </header>

      {err && (
        <p
          role="alert"
          className="rounded border px-3 py-2 text-sm"
          style={{ borderColor: "var(--color-err)", color: "var(--color-err)" }}
        >
          {err}
        </p>
      )}

      {/* 编辑弹窗 */}
      {(isNew || editing) && (
        <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
          <AgentEditor
            agent={editing}
            models={allModels}
            skills={allSkills}
            mcpServers={allMcp}
            onDone={closeEditor}
          />
        </div>
      )}

      {allAgents.length === 0 ? (
        <p className="text-sm text-[var(--color-muted)]">
          还没有智能体。点「新建智能体」创建第一个。
        </p>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {allAgents.map((a: AgentItem) => (
            <div
              key={a.id}
              className="flex flex-col gap-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-3"
            >
              <div className="flex items-start gap-2">
                {/* 头像 + 名称 + 描述 */}
                <button
                  onClick={() => openEdit(a)}
                  className="min-w-0 flex-1 text-left"
                >
                  <div className="flex items-center gap-1.5">
                    {a.avatar && (
                      <span className="text-lg leading-none">{a.avatar}</span>
                    )}
                    <span
                      className={clsx(
                        "truncate text-sm font-medium",
                        a.hidden && "opacity-50",
                      )}
                    >
                      {a.name}
                    </span>
                    {a.is_default && (
                      <span className="shrink-0 rounded bg-[var(--color-surface-2)] px-1.5 py-0.5 text-[10px] text-[var(--color-muted)]">
                        默认
                      </span>
                    )}
                  </div>
                  {a.description && (
                    <p
                      className={clsx(
                        "mt-0.5 truncate text-xs",
                        a.hidden ? "opacity-40" : "text-[var(--color-muted)]",
                      )}
                    >
                      {a.description}
                    </p>
                  )}
                </button>

                {/* 编辑按钮 */}
                <button
                  onClick={() => openEdit(a)}
                  aria-label={`编辑 ${a.name}`}
                  className="shrink-0 rounded p-1 text-[var(--color-muted)] transition hover:text-[var(--color-accent)]"
                >
                  <Pencil size={13} />
                </button>
              </div>

              {/* 底部：可见性滑块 + 删除 */}
              <div className="flex items-center gap-2">
                {/* 可见性开关 */}
                <button
                  onClick={() => toggleHidden.mutate(a)}
                  disabled={toggleHidden.isPending}
                  aria-pressed={!a.hidden}
                  aria-label={`${a.hidden ? "显示" : "隐藏"} ${a.name}`}
                  title={
                    a.hidden
                      ? "已隐藏：不出现在对话页切换菜单里"
                      : "可见：在对话页切换菜单里可以选择"
                  }
                  className={clsx(
                    "flex shrink-0 items-center gap-1 rounded px-2 py-1 text-xs",
                    a.hidden
                      ? "bg-[var(--color-surface-2)] text-[var(--color-muted)]"
                      : "bg-[var(--color-accent)] text-white",
                  )}
                >
                  {a.hidden ? <EyeOff size={11} /> : <Eye size={11} />}
                  {a.hidden ? "隐藏" : "可见"}
                </button>

                <div className="flex-1" />

                {/* 删除按钮 */}
                <button
                  onClick={() => {
                    if (
                      confirm(
                        `删除智能体「${a.name}」？\\n\\n此操作不可恢复。` +
                          (a.is_default
                            ? "\\n注意：它是默认智能体，删除后需重新指定。"
                            : ""),
                      )
                    ) {
                      delAgent.mutate(a.id);
                    }
                  }}
                  disabled={delAgent.isPending}
                  aria-label={`删除 ${a.name}`}
                  className="shrink-0 rounded p-1 text-[var(--color-muted)] transition hover:text-[var(--color-err)]"
                >
                  <Trash2 size={13} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      <p className="text-xs text-[var(--color-muted)]">
        在对话页选择一个智能体后，对话会使用该智能体配置的模型、系统提示词和工具权限。
      </p>
    </section>
  );
}
