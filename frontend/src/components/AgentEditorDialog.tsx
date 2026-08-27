/**
 * 智能体编辑子窗口（模态框）。
 *
 * 新建和编辑都用它 —— 不再在当前页面内联展开。
 *
 * 技能 / MCP 不在这里逐个勾选：这里只【展示当前已启用的】，通过
 * 「管理」按钮弹出 TogglePickerDialog 批量增删。取消勾选 = 从当前
 * 智能体移除（不是真删除，真删除在技能/MCP 各自设置页）。
 */

import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, Plus, X } from "lucide-react";

import { api } from "@/lib/api";
import { filterModelsForPurpose } from "@/lib/purposeMeta";
import type { AgentItem, ModelItem } from "@/lib/types";
import { TogglePickerDialog } from "./TogglePickerDialog";

type SkillLike = { name: string; description: string };
type McpLike = { server_id: string; status: string; tool_count: number };

const PERMISSIONS = [
  ["permission_read", "读文件"],
  ["permission_write", "写文件"],
  ["permission_shell", "终端"],
  ["permission_network", "联网"],
  ["permission_subagent", "子智能体"],
] as const;

function Chips({
  items,
  empty,
  onManage,
}: {
  items: string[];
  empty: string;
  onManage: () => void;
}) {
  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <span className="text-xs" style={{ color: "var(--color-muted)" }}>
          已启用（{items.length}）
        </span>
        <button
          type="button"
          onClick={onManage}
          className="inline-flex items-center gap-1 rounded border px-2 py-0.5 text-xs hover:bg-[var(--color-surface-2)]"
          style={{ borderColor: "var(--color-border)", color: "var(--color-accent)" }}
        >
          <Plus size={11} />
          管理
        </button>
      </div>
      {items.length === 0 ? (
        <p className="rounded border border-dashed px-2 py-2 text-xs" style={{ borderColor: "var(--color-border)", color: "var(--color-muted)" }}>
          {empty}
        </p>
      ) : (
        <div className="flex flex-wrap gap-1">
          {items.map((n) => (
            <span
              key={n}
              className="rounded bg-[var(--color-surface-2)] px-2 py-0.5 text-xs"
              style={{ color: "var(--color-text)" }}
            >
              {n}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

export function AgentEditorDialog({
  agent,
  models,
  skills,
  mcpServers,
  onDone,
  onClose,
}: {
  agent: AgentItem | null;
  models: ModelItem[];
  skills: SkillLike[];
  mcpServers: McpLike[];
  onDone: () => void;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState(agent?.name ?? "");
  const [description, setDescription] = useState(agent?.description ?? "");
  const [avatar, setAvatar] = useState(agent?.avatar ?? "");
  const [systemPrompt, setSystemPrompt] = useState(agent?.system_prompt ?? "");
  const [modelId, setModelId] = useState(agent?.model_id ?? "");
  const [skillNames, setSkillNames] = useState<string[]>(agent?.skill_names ?? []);
  const [mcpServerIds, setMcpServerIds] = useState<string[]>(agent?.mcp_servers ?? []);
  const [extraLlmParams, setExtraLlmParams] = useState(agent?.extra_llm_params ?? "");
  const [llmDraft, setLlmDraft] = useState("");
  const [permissions, setPermissions] = useState({
    permission_read: agent?.permission_read ?? true,
    permission_write: agent?.permission_write ?? false,
    permission_shell: agent?.permission_shell ?? false,
    permission_network: agent?.permission_network ?? false,
    permission_subagent: agent?.permission_subagent ?? false,
  });
  const [picker, setPicker] = useState<null | "skill" | "mcp" | "llm">(null);
  const [err, setErr] = useState("");

  // 智能体只能用对话/推理模型。嵌入、重排、TTS 等模型出现在这里只会
  // 让用户误选，发消息时再报协议错误。
  const chatModels = useMemo(() => filterModelsForPurpose(models, "chat"), [models]);
  const selectedModel = models.find((m) => m.id === modelId);
  const selectedHidden =
    modelId !== "" && selectedModel != null && !chatModels.some((m) => m.id === modelId);

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
        extra_llm_params: extraLlmParams,
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

  const inputCls =
    "w-full rounded-md border px-2.5 py-1.5 text-sm outline-none focus:border-[var(--color-accent)]";
  const inputStyle = {
    borderColor: "var(--color-border)",
    background: "var(--color-bg)",
  } as const;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={agent ? `编辑 ${agent.name}` : "新建智能体"}
    >
      <div
        className="flex max-h-[90vh] w-full max-w-lg flex-col overflow-hidden rounded-xl border bg-[var(--color-surface)] shadow-2xl"
        style={{ borderColor: "var(--color-border)" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between border-b px-5 py-4" style={{ borderColor: "var(--color-border)" }}>
          <div>
            <h2 className="text-base font-medium">
              {agent ? `编辑 ${agent.name}` : "新建智能体"}
            </h2>
            <p className="mt-0.5 text-xs" style={{ color: "var(--color-muted)" }}>
              配置模型、系统提示词、工具权限、技能与 MCP。
            </p>
          </div>
          <button type="button" onClick={onClose} aria-label="关闭" className="rounded p-1 hover:bg-[var(--color-surface-2)]">
            <X size={16} style={{ color: "var(--color-muted)" }} />
          </button>
        </div>

        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-5 py-4">
          {err && (
            <p role="alert" className="rounded border px-3 py-2 text-sm" style={{ borderColor: "var(--color-err)", color: "var(--color-err)" }}>
              {err}
            </p>
          )}

          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="mb-1 block text-xs" style={{ color: "var(--color-muted)" }}>名称</span>
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="我的助手" className={inputCls} style={inputStyle} autoFocus />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs" style={{ color: "var(--color-muted)" }}>头像（emoji）</span>
              <input value={avatar} onChange={(e) => setAvatar(e.target.value)} placeholder="🤖" className={inputCls} style={inputStyle} />
            </label>
          </div>

          <label className="block">
            <span className="mb-1 block text-xs" style={{ color: "var(--color-muted)" }}>描述</span>
            <input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="擅长编写 Python 代码的助手" className={inputCls} style={inputStyle} />
          </label>

          <label className="block">
            <span className="mb-1 block text-xs" style={{ color: "var(--color-muted)" }}>使用模型</span>
            <select value={modelId} onChange={(e) => setModelId(e.target.value)} className={inputCls} style={inputStyle}>
              <option value="">（不指定，跟随全局绑定）</option>
              {selectedHidden && selectedModel && (
                <option value={modelId}>
                  {selectedModel.display_name || selectedModel.model_id}（类型不匹配）
                </option>
              )}
              {chatModels.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.display_name || m.model_id} · {(m.context_window / 1000).toFixed(0)}K
                </option>
              ))}
            </select>
          </label>

          <label className="block">
            <span className="mb-1 block text-xs" style={{ color: "var(--color-muted)" }}>系统提示词</span>
            <textarea value={systemPrompt} onChange={(e) => setSystemPrompt(e.target.value)} placeholder="你是一个专业的 Python 开发者…" rows={4} className={inputCls} style={inputStyle} />
          </label>

          <fieldset className="rounded-lg border p-3" style={{ borderColor: "var(--color-border)" }}>
            <legend className="px-1 text-xs" style={{ color: "var(--color-muted)" }}>工具权限</legend>
            <div className="flex flex-wrap gap-3">
              {PERMISSIONS.map(([key, label]) => (
                <label key={key} className="flex items-center gap-1.5 text-xs cursor-pointer">
                  <input
                    type="checkbox"
                    checked={permissions[key]}
                    onChange={() => setPermissions((p) => ({ ...p, [key]: !p[key] }))}
                    className="shrink-0 accent-[var(--color-accent)]"
                  />
                  {label}
                </label>
              ))}
            </div>
          </fieldset>

          {/* 技能：只展示已启用的，管理走子窗口 */}
          <Chips
            items={skillNames}
            empty="尚未启用任何技能"
            onManage={() => setPicker("skill")}
          />

          {/* MCP：同理 */}
          <Chips
            items={mcpServerIds}
            empty="尚未启用任何 MCP 服务器"
            onManage={() => setPicker("mcp")}
          />

          {/* 额外 LLM 参数：透传给上游的原始参数，各模型差异交给用户自己填 */}
          <div>
            <div className="mb-1 flex items-center justify-between">
              <span className="text-xs" style={{ color: "var(--color-muted)" }}>
                额外 LLM 参数
              </span>
              <button
                type="button"
                onClick={() => {
                  setLlmDraft(extraLlmParams);
                  setPicker("llm");
                }}
                className="inline-flex items-center gap-1 rounded border px-2 py-0.5 text-xs hover:bg-[var(--color-surface-2)]"
                style={{ borderColor: "var(--color-border)", color: "var(--color-accent)" }}
              >
                <Plus size={11} />
                设置
              </button>
            </div>
            {extraLlmParams.trim() ? (
              <p
                className="truncate rounded bg-[var(--color-surface-2)] px-2 py-1.5 font-mono text-xs"
                style={{ color: "var(--color-text)" }}
                title={extraLlmParams}
              >
                {extraLlmParams}
              </p>
            ) : (
              <p
                className="rounded border border-dashed px-2 py-2 text-xs"
                style={{ borderColor: "var(--color-border)", color: "var(--color-muted)" }}
              >
                未设置。可输入如 thinking: {"{\"type\": \"disabled\"}"} 或完整 JSON 对象。
              </p>
            )}
          </div>
        </div>

        <div className="flex items-center justify-end gap-2 border-t px-5 py-3" style={{ borderColor: "var(--color-border)" }}>
          <button type="button" onClick={onClose} className="rounded-lg border px-4 py-1.5 text-sm hover:bg-[var(--color-surface-2)]" style={{ borderColor: "var(--color-border)" }}>
            取消
          </button>
          <button
            type="button"
            onClick={() => save.mutate()}
            disabled={!name || save.isPending}
            className="inline-flex items-center gap-1.5 rounded-lg px-4 py-1.5 text-sm text-white disabled:opacity-50"
            style={{ background: "var(--color-accent)" }}
          >
            {save.isPending && <Loader2 size={14} className="animate-spin" />}
            {agent ? "保存修改" : "创建"}
          </button>
        </div>
      </div>

      {/* 技能选择器子窗口 */}
      {picker === "skill" && (
        <TogglePickerDialog
          title="启用技能"
          hint="勾选要在当前智能体里启用的技能。取消勾选只是移除，不删除技能本身。"
          items={skills.map((s) => ({ id: s.name, name: s.name, detail: s.description }))}
          selected={new Set(skillNames)}
          onConfirm={(next) => {
            setSkillNames([...next]);
            setPicker(null);
          }}
          onClose={() => setPicker(null)}
        />
      )}

      {/* MCP 选择器子窗口 */}
      {picker === "mcp" && (
        <TogglePickerDialog
          title="启用 MCP 服务器"
          hint="勾选要在当前智能体里启用的 MCP 服务器。取消勾选只是移除，不删除服务器。"
          items={mcpServers.map((m) => ({
            id: m.server_id,
            name: m.server_id,
            detail: `${m.tool_count} 工具 · ${m.status}`,
          }))}
          selected={new Set(mcpServerIds)}
          onConfirm={(next) => {
            setMcpServerIds([...next]);
            setPicker(null);
          }}
          onClose={() => setPicker(null)}
        />
      )}

      {/* 额外 LLM 参数子窗口 */}
      {picker === "llm" && (
        <div
          className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
          onClick={() => setPicker(null)}
          role="dialog"
          aria-modal="true"
          aria-label="额外 LLM 参数"
        >
          <div
            className="flex max-h-[80vh] w-full max-w-lg flex-col overflow-hidden rounded-xl border bg-[var(--color-surface)] shadow-2xl"
            style={{ borderColor: "var(--color-border)" }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-start justify-between border-b px-5 py-4" style={{ borderColor: "var(--color-border)" }}>
              <div>
                <h2 className="text-base font-medium">额外 LLM 参数</h2>
                <p className="mt-0.5 text-xs" style={{ color: "var(--color-muted)" }}>
                  透传给上游的原始参数，各模型差异自己填。支持 key: value 多行或完整 JSON 对象。
                </p>
              </div>
              <button type="button" onClick={() => setPicker(null)} aria-label="关闭" className="rounded p-1 hover:bg-[var(--color-surface-2)]">
                <X size={16} style={{ color: "var(--color-muted)" }} />
              </button>
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
              <textarea
                value={llmDraft}
                onChange={(e) => setLlmDraft(e.target.value)}
                placeholder={`thinking: {"type": "disabled"}\n# 或\n{"thinking": {"type": "disabled"}}`}
                rows={8}
                autoFocus
                className="w-full rounded-md border px-2.5 py-1.5 font-mono text-sm outline-none focus:border-[var(--color-accent)]"
                style={{ borderColor: "var(--color-border)", background: "var(--color-bg)" }}
              />
            </div>

            <div className="flex items-center justify-end gap-2 border-t px-5 py-3" style={{ borderColor: "var(--color-border)" }}>
              <button
                type="button"
                onClick={() => setPicker(null)}
                className="rounded-lg border px-4 py-1.5 text-sm hover:bg-[var(--color-surface-2)]"
                style={{ borderColor: "var(--color-border)" }}
              >
                取消
              </button>
              <button
                type="button"
                onClick={() => {
                  setExtraLlmParams(llmDraft);
                  setPicker(null);
                }}
                className="rounded-lg px-4 py-1.5 text-sm text-white"
                style={{ background: "var(--color-accent)" }}
              >
                确定
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
