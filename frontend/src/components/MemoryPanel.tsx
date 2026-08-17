/**
 * 记忆管理面板。
 *
 * 顶部是「范围」筛选条（智能体 / 会话 / 类型），列表和搜索两个子页共用。
 * 范围语义对齐后端的三层隔离：
 *   - 只选智能体     → 全局 + 该智能体
 *   - 选智能体 + 会话 → 全局 + 该智能体 + 该会话
 *   - 都不选         → 只有全局
 */

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Database,
  Loader2,
  RotateCcw,
  TriangleAlert,
  Search,
  Trash2,
  Eye,
  X,
  FileText,
  Settings,
  List,
  Check,
  Filter,
} from "lucide-react";
import clsx from "clsx";
import { api } from "@/lib/api";
import { ApiError } from "@/lib/sse";
import type {
  MemorySettingItem,
  MemoryListItem,
  MemorySearchHit,
} from "@/lib/types";

/** 把 key 分组显示。用前缀判断，避免再给后端加一个 group 字段。 */
function groupOf(key: string): string {
  if (key.includes("auto_commit")) return "自动提取";
  if (key.includes("embedding") || key.includes("search_min")) return "向量与检索";
  if (key.includes("prefetch") || key.includes("tool_")) return "预取与工具";
  if (key.includes("chars")) return "截断";
  return "提取";
}

const GROUP_ORDER = ["自动提取", "提取", "截断", "预取与工具", "向量与检索"];

const MEMORY_TYPES = [
  { value: "", label: "全部类型" },
  { value: "preferences", label: "偏好" },
  { value: "events", label: "事件" },
  { value: "entities", label: "实体" },
  { value: "experiences", label: "经验" },
];

// ── 通用样式 ──

const inputCls =
  "rounded-md border px-2.5 py-1.5 text-sm outline-none focus:border-[var(--color-accent)]";
const inputStyle = {
  borderColor: "var(--color-border)",
  background: "var(--color-bg)",
} as const;

const primaryBtn =
  "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium text-white transition hover:opacity-90 disabled:opacity-40";
const ghostBtn =
  "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs transition hover:bg-[var(--color-surface-2)] disabled:opacity-40";

// ── 范围筛选条 ──

export interface MemoryScope {
  agent_id: string;
  session_id: string;
  memory_type: string;
}

function ScopeBar({
  scope,
  onChange,
  agents,
  sessions,
}: {
  scope: MemoryScope;
  onChange: (s: MemoryScope) => void;
  agents: { id: string; name: string }[];
  sessions: { id: string; title: string }[];
}) {
  const sessionWithoutAgent = scope.session_id && !scope.agent_id;

  return (
    <div className="flex flex-wrap items-center gap-2 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2">
      <Filter size={13} className="shrink-0 text-[var(--color-muted)]" />
      <span className="text-xs text-[var(--color-muted)]">范围</span>

      <select
        value={scope.agent_id}
        onChange={(e) => onChange({ ...scope, agent_id: e.target.value })}
        className={clsx("px-2 py-1 text-xs", inputCls)}
        style={inputStyle}
        aria-label="智能体"
      >
        <option value="">全部智能体</option>
        {agents.map((a) => (
          <option key={a.id} value={a.id}>
            {a.name}
          </option>
        ))}
      </select>

      <select
        value={scope.session_id}
        onChange={(e) => onChange({ ...scope, session_id: e.target.value })}
        className={clsx("max-w-40 px-2 py-1 text-xs", inputCls)}
        style={inputStyle}
        aria-label="会话"
      >
        <option value="">全部会话</option>
        {sessions.map((s) => (
          <option key={s.id} value={s.id}>
            {s.title || "未命名会话"}
          </option>
        ))}
      </select>

      <select
        value={scope.memory_type}
        onChange={(e) => onChange({ ...scope, memory_type: e.target.value })}
        className={clsx("px-2 py-1 text-xs", inputCls)}
        style={inputStyle}
        aria-label="类型"
      >
        {MEMORY_TYPES.map((t) => (
          <option key={t.value} value={t.value}>
            {t.label}
          </option>
        ))}
      </select>

      {sessionWithoutAgent && (
        <span className="flex items-center gap-1 text-xs text-[var(--color-warn)]">
          <TriangleAlert size={12} />
          选会话时建议同时选智能体
        </span>
      )}

      {(scope.agent_id || scope.session_id || scope.memory_type) && (
        <button
          type="button"
          onClick={() => onChange({ agent_id: "", session_id: "", memory_type: "" })}
          className="text-xs text-[var(--color-muted)] underline hover:text-[var(--color-text)]"
        >
          重置
        </button>
      )}
    </div>
  );
}

// ── 设置项 ──

function SettingField({
  item,
  draft,
  onChange,
}: {
  item: MemorySettingItem;
  draft: number | boolean | string | undefined;
  onChange: (v: number | boolean | string) => void;
}) {
  const current = draft ?? item.value;
  const dirty = draft !== undefined && draft !== item.value;

  if (item.type === "bool") {
    return (
      <label className="flex cursor-pointer items-start gap-3 py-2">
        <input
          type="checkbox"
          checked={Boolean(current)}
          onChange={(e) => onChange(e.target.checked)}
          className="mt-0.5 h-4 w-4 shrink-0 accent-[var(--color-accent)]"
        />
        <span className="min-w-0">
          <span className={clsx("text-sm", dirty && "text-[var(--color-accent)]")}>
            {item.label}
            {dirty && <span className="ml-1 text-xs text-[var(--color-warn)]">未保存</span>}
          </span>
          {item.hint && (
            <span className="block text-xs text-[var(--color-muted)]">{item.hint}</span>
          )}
        </span>
      </label>
    );
  }

  const isFloat = item.type === "float";
  return (
    <label className="block py-2">
      <span className={clsx("text-sm", dirty && "text-[var(--color-accent)]")}>
        {item.label}
        {dirty && <span className="ml-1 text-xs text-[var(--color-warn)]">未保存</span>}
      </span>
      {item.hint && <span className="block text-xs text-[var(--color-muted)]">{item.hint}</span>}
      <span className="mt-1 flex items-center gap-2">
        <input
          type="number"
          value={String(current)}
          min={item.min ?? undefined}
          max={item.max ?? undefined}
          step={isFloat ? 0.05 : 1}
          onChange={(e) => {
            const raw = e.target.value;
            if (raw === "") return;
            onChange(isFloat ? Number.parseFloat(raw) : Number.parseInt(raw, 10));
          }}
          className={clsx("w-32", inputCls)}
          style={inputStyle}
        />
        {(item.min !== null || item.max !== null) && (
          <span className="text-xs text-[var(--color-muted)]">
            范围 {item.min ?? "-"} ~ {item.max ?? "-"}
          </span>
        )}
      </span>
    </label>
  );
}

// ── 向量索引 ──

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone?: "ok" | "warn" | "muted";
}) {
  const color =
    tone === "ok"
      ? "var(--color-ok)"
      : tone === "warn"
        ? "var(--color-warn)"
        : "var(--color-muted)";
  return (
    <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 text-center">
      <p className="text-lg font-semibold leading-none" style={{ color }}>
        {value}
      </p>
      <p className="mt-1 text-[11px] text-[var(--color-muted)]">{label}</p>
    </div>
  );
}

function VectorSection() {
  const qc = useQueryClient();
  const [note, setNote] = useState<string>("");

  const status = useQuery({
    queryKey: ["memory", "vectors"],
    queryFn: api.memory.vectors,
  });

  const rebuild = useMutation({
    mutationFn: (onlyStale: boolean) => api.memory.rebuildVectors(onlyStale),
    onSuccess: (r) => {
      setNote(
        r.errors.length
          ? `重算 ${r.succeeded}/${r.attempted}，${r.errors.length} 条失败：${r.errors[0]}`
          : `重算完成：${r.succeeded} 条，${r.model}（${r.dim} 维）`,
      );
      void qc.invalidateQueries({ queryKey: ["memory", "vectors"] });
    },
    onError: (e) => setNote(e instanceof ApiError ? e.message : String(e)),
  });

  const clear = useMutation({
    mutationFn: api.memory.clearVectors,
    onSuccess: (r) => {
      setNote(`已清空 ${r.cleared} 条向量，语义召回将回落关键词搜索`);
      void qc.invalidateQueries({ queryKey: ["memory", "vectors"] });
    },
    onError: (e) => setNote(e instanceof ApiError ? e.message : String(e)),
  });

  const s = status.data;
  const stale = s ? s.never + s.model + s.content : 0;
  const busy = rebuild.isPending || clear.isPending;
  const hasModelMismatch = (s?.model ?? 0) > 0;

  return (
    <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <h3 className="flex items-center gap-2 text-sm font-medium">
        <Database size={14} />
        向量索引
      </h3>

      {!s ? (
        <p className="mt-3 text-xs text-[var(--color-muted)]">读取中…</p>
      ) : !s.embedding_configured ? (
        <p className="mt-3 flex items-start gap-2 rounded-md border border-[var(--color-warn)]/30 bg-[var(--color-warn)]/10 p-3 text-xs text-[var(--color-warn)]">
          <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            没有配置嵌入模型，语义召回已关闭（回落关键词搜索）。
            去「模型」标签把某个嵌入模型绑到「嵌入」功能位。
          </span>
        </p>
      ) : (
        <>
          {/* 模型不匹配警告横幅：换了嵌入模型时醒目提示 */}
          {hasModelMismatch && (
            <div className="mt-3 rounded-lg border border-[var(--color-warn)]/40 bg-[var(--color-warn)]/10 p-3">
              <div className="flex items-start gap-2">
                <TriangleAlert size={15} className="mt-0.5 shrink-0 text-[var(--color-warn)]" />
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium text-[var(--color-warn)]">嵌入模型已更改</p>
                  <p className="mt-1 text-xs text-[var(--color-text)]">
                    检测到 {s.model} 条记忆用的是旧的嵌入模型，当前模型为{" "}
                    <code className="rounded bg-[var(--color-surface-2)] px-1 py-0.5">
                      {s.embedding_model}
                    </code>
                    。旧向量已自动停止参与召回，建议重新向量化。
                  </p>
                  <div className="mt-2 flex flex-wrap gap-2">
                    <button
                      onClick={() => rebuild.mutate(true)}
                      disabled={busy}
                      className={primaryBtn}
                      style={{ background: "var(--color-accent)" }}
                    >
                      {rebuild.isPending && <Loader2 size={13} className="animate-spin" />}
                      重新向量化（推荐）
                    </button>
                    <button
                      onClick={() => clear.mutate()}
                      disabled={busy}
                      className={ghostBtn}
                      style={{ borderColor: "var(--color-border)" }}
                    >
                      清空向量（回落关键词搜索）
                    </button>
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* 向量状态统计 */}
          <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-5">
            <Stat label="总记忆" value={s.total} />
            <Stat label="新鲜" value={s.fresh} tone="ok" />
            <Stat label="从未计算" value={s.never} tone="muted" />
            <Stat label="模型不匹配" value={s.model} tone="warn" />
            <Stat label="内容变更" value={s.content} tone="warn" />
          </div>

          <p className="mt-3 text-xs text-[var(--color-muted)]">
            当前嵌入模型{" "}
            <code className="rounded bg-[var(--color-surface-2)] px-1 py-0.5 text-[var(--color-text)]">
              {s.embedding_model}
            </code>
          </p>
        </>
      )}

      <div className="mt-3 flex flex-wrap gap-2">
        <button
          onClick={() => rebuild.mutate(true)}
          disabled={busy || !s?.embedding_configured || stale === 0}
          className={primaryBtn}
          style={{ background: "var(--color-accent)" }}
        >
          {rebuild.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
          重算失效的{stale > 0 ? `（${stale}）` : ""}
        </button>
        <button
          onClick={() => rebuild.mutate(false)}
          disabled={busy || !s?.embedding_configured || !s?.total}
          className={ghostBtn}
          style={{ borderColor: "var(--color-border)" }}
          title="怀疑向量算错时用。会重算全部记忆，可能产生大量 API 调用"
        >
          全部重算
        </button>
        <button
          onClick={() => clear.mutate()}
          disabled={busy || !s?.total}
          className={ghostBtn}
          style={{ borderColor: "var(--color-border)", color: "var(--color-err)" }}
          title="清空后语义召回回落关键词搜索。记忆文件本身不受影响"
        >
          清空向量
        </button>
      </div>

      <p className="mt-2 text-xs text-[var(--color-muted)]">
        换嵌入模型后旧向量会立即停止参与召回，但不会自动重算 ——
        那可能是大量 API 调用，需要你确认。
      </p>

      {note && (
        <p className="mt-2 rounded-md border border-[var(--color-border)] bg-[var(--color-surface-2)] p-2 text-xs text-[var(--color-muted)]">
          {note}
        </p>
      )}
    </section>
  );
}

// ── 记忆列表 ──

function MemoryListView({ scope }: { scope: MemoryScope }) {
  const qc = useQueryClient();
  const [selectedUri, setSelectedUri] = useState<string | null>(null);

  const { data: memories, isLoading } = useQuery({
    queryKey: ["memory-list", scope.agent_id, scope.session_id, scope.memory_type],
    queryFn: () =>
      api.memory.list({
        agent_id: scope.agent_id || undefined,
        session_id: scope.session_id || undefined,
        memory_type: scope.memory_type || undefined,
        limit: 500,
      }),
  });

  // 详情按需读取（列表项只有元数据，没有正文）
  const detail = useQuery({
    queryKey: ["memory-read", selectedUri],
    queryFn: () => api.memory.read(selectedUri!),
    enabled: !!selectedUri,
  });

  const deleteMutation = useMutation({
    mutationFn: (uri: string) => api.memory.delete(uri),
    onSuccess: () => {
      setSelectedUri(null);
      void qc.invalidateQueries({ queryKey: ["memory-list"] });
    },
  });

  const items = memories?.items ?? [];

  return (
    <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <div className="flex items-center justify-between">
        <h3 className="flex items-center gap-2 text-sm font-medium">
          <List size={14} />
          记忆列表
        </h3>
        <span className="text-xs text-[var(--color-muted)]">
          {memories ? `${memories.total} 条` : ""}
        </span>
      </div>

      {isLoading ? (
        <div className="mt-4 flex items-center justify-center py-8">
          <Loader2 className="h-5 w-5 animate-spin text-[var(--color-muted)]" />
        </div>
      ) : items.length === 0 ? (
        <div className="mt-4 rounded-lg border border-dashed border-[var(--color-border)] p-8 text-center">
          <FileText className="mx-auto h-8 w-8 text-[var(--color-muted)]" />
          <p className="mt-2 text-sm text-[var(--color-muted)]">当前范围内暂无记忆</p>
        </div>
      ) : (
        <div className="mt-3 space-y-2">
          {items.map((item: MemoryListItem) => (
            <div
              key={item.uri}
              className="group rounded-md border border-[var(--color-border)] p-3 transition-colors hover:border-[var(--color-accent)]"
            >
              <div className="flex items-start justify-between gap-3">
                <button
                  type="button"
                  onClick={() => setSelectedUri(item.uri)}
                  className="min-w-0 flex-1 text-left"
                >
                  <p className="truncate text-sm font-medium text-[var(--color-text)]">
                    {item.title}
                  </p>
                  <p className="mt-1 text-xs text-[var(--color-muted)]">
                    {item.memory_type} · {item.scope}
                    {item.agent_id && ` · agent:${item.agent_id}`}
                    {item.session_id && ` · session:${item.session_id}`}
                  </p>
                  <p className="mt-1 text-xs text-[var(--color-muted)]">
                    版本 {item.version} · 更新于{" "}
                    {new Date(item.updated_at * 1000).toLocaleString("zh-CN")}
                  </p>
                </button>
                <div className="flex shrink-0 gap-1 opacity-0 transition-opacity group-hover:opacity-100">
                  <button
                    onClick={() => setSelectedUri(item.uri)}
                    className="rounded p-1.5 hover:bg-[var(--color-surface-2)]"
                    title="查看详情"
                  >
                    <Eye size={14} className="text-[var(--color-muted)]" />
                  </button>
                  <button
                    onClick={() => {
                      if (confirm(`删除记忆「${item.title}」？不可恢复。`)) {
                        deleteMutation.mutate(item.uri);
                      }
                    }}
                    className="rounded p-1.5 hover:bg-[var(--color-surface-2)]"
                    title="删除"
                  >
                    <Trash2 size={14} className="text-[var(--color-muted)]" />
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 详情对话框 */}
      {selectedUri && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
          onClick={() => setSelectedUri(null)}
          role="dialog"
          aria-modal="true"
        >
          <div
            className="max-h-[80vh] w-full max-w-2xl overflow-auto rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-5 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            {detail.isLoading ? (
              <div className="flex items-center justify-center py-8">
                <Loader2 className="h-5 w-5 animate-spin text-[var(--color-muted)]" />
              </div>
            ) : detail.data ? (
              <>
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <h3 className="text-base font-medium text-[var(--color-text)]">
                      {detail.data.fields?.title
                        ? String((detail.data.fields as Record<string, unknown>).title)
                        : detail.data.memory_type}
                    </h3>
                    <p className="mt-1 text-xs text-[var(--color-muted)]">
                      {detail.data.memory_type}
                    </p>
                  </div>
                  <button
                    onClick={() => setSelectedUri(null)}
                    className="shrink-0 rounded p-1 hover:bg-[var(--color-surface-2)]"
                    aria-label="关闭"
                  >
                    <X size={16} className="text-[var(--color-muted)]" />
                  </button>
                </div>
                <div className="mt-4 rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] p-4">
                  <pre className="whitespace-pre-wrap text-sm text-[var(--color-text)]">
                    {detail.data.body}
                  </pre>
                </div>
                <div className="mt-4 flex items-center justify-between text-xs text-[var(--color-muted)]">
                  <span className="truncate">URI: {detail.data.uri}</span>
                  <span className="shrink-0">版本 {detail.data.version}</span>
                </div>
              </>
            ) : (
              <p className="py-8 text-center text-xs text-[var(--color-err)]">
                {(detail.error as Error)?.message ?? "读取失败"}
              </p>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

// ── 语义搜索 ──

function SearchSection({ scope }: { scope: MemoryScope }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<MemorySearchHit[]>([]);
  const [searching, setSearching] = useState(false);
  const [err, setErr] = useState("");
  const [searched, setSearched] = useState(false);

  // 拉向量状态，进来就知道嵌入模型配没配，不用等搜一次
  const { data: vectors } = useQuery({
    queryKey: ["memory", "vectors"],
    queryFn: api.memory.vectors,
  });
  const embeddingConfigured = vectors?.embedding_configured ?? true;

  const handleSearch = async () => {
    if (!query.trim()) return;
    setSearching(true);
    setErr("");
    try {
      const res = await api.memory.search(query, {
        agent_id: scope.agent_id || undefined,
        session_id: scope.session_id || undefined,
        memory_type: scope.memory_type || undefined,
        limit: 20,
      });
      setResults(res.hits);
      setSearched(true);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setSearching(false);
    }
  };

  return (
    <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <h3 className="flex items-center gap-2 text-sm font-medium">
        <Search size={14} />
        语义搜索
      </h3>

      {!embeddingConfigured && (
        <p className="mt-3 flex items-start gap-2 rounded-md border border-[var(--color-warn)]/30 bg-[var(--color-warn)]/10 p-3 text-xs text-[var(--color-warn)]">
          <TriangleAlert size={13} className="mt-0.5 shrink-0" />
          <span>
            未配置嵌入模型，当前只能关键词搜索。去「模型」标签把嵌入模型绑到「嵌入」功能位。
          </span>
        </p>
      )}

      <div className="mt-3 flex gap-2">
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleSearch()}
          placeholder="输入关键词搜索记忆…"
          className={clsx("flex-1", inputCls)}
          style={inputStyle}
        />
        <button
          onClick={handleSearch}
          disabled={searching || !query.trim()}
          className={primaryBtn}
          style={{ background: "var(--color-accent)" }}
        >
          {searching && <Loader2 size={14} className="animate-spin" />}
          搜索
        </button>
      </div>

      {err && (
        <p className="mt-3 flex items-start gap-1.5 text-xs text-[var(--color-err)]">
          <TriangleAlert size={13} className="mt-0.5 shrink-0" />
          {err}
        </p>
      )}

      {searched && results.length === 0 && !err && (
        <p className="mt-3 rounded-lg border border-dashed border-[var(--color-border)] p-6 text-center text-xs text-[var(--color-muted)]">
          没有找到相关记忆
        </p>
      )}

      {results.length > 0 && (
        <div className="mt-3 space-y-2">
          {results.map((hit) => (
            <div
              key={hit.uri}
              className="rounded-md border border-[var(--color-border)] p-3 transition-colors hover:border-[var(--color-accent)]"
            >
              <p className="text-sm font-medium text-[var(--color-text)]">{hit.title}</p>
              <p className="mt-1 text-xs text-[var(--color-muted)]">
                {hit.memory_type} · 相关度 {(hit.score * 100).toFixed(1)}%
              </p>
              <p className="mt-1 truncate text-xs text-[var(--color-muted)]">{hit.uri}</p>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

// ── 提取参数 ──

function SettingsSection() {
  const qc = useQueryClient();
  const [draft, setDraft] = useState<Record<string, number | boolean | string>>({});
  const [err, setErr] = useState<string>("");
  const [saved, setSaved] = useState(false);

  const settings = useQuery({
    queryKey: ["memory", "settings"],
    queryFn: api.memory.settings,
  });

  const save = useMutation({
    mutationFn: (values: Record<string, unknown>) => api.memory.updateSettings(values),
    onSuccess: () => {
      setDraft({});
      setErr("");
      setSaved(true);
      void qc.invalidateQueries({ queryKey: ["memory", "settings"] });
    },
    onError: (e) => setErr(e instanceof ApiError ? e.message : String(e)),
  });

  const reset = useMutation({
    mutationFn: api.memory.resetSettings,
    onSuccess: () => {
      setDraft({});
      setErr("");
      void qc.invalidateQueries({ queryKey: ["memory", "settings"] });
    },
    onError: (e) => setErr(e instanceof ApiError ? e.message : String(e)),
  });

  useEffect(() => {
    if (!saved) return;
    const t = setTimeout(() => setSaved(false), 2000);
    return () => clearTimeout(t);
  }, [saved]);

  const grouped = useMemo(() => {
    // 只渲染 memory 段的项 —— websearch 项走独立的 /api/websearch 页
    const items = (settings.data?.items ?? []).filter((i) => i.section === "memory");
    const map = new Map<string, MemorySettingItem[]>();
    for (const item of items) {
      const g = groupOf(item.key);
      const list = map.get(g) ?? [];
      list.push(item);
      map.set(g, list);
    }
    return GROUP_ORDER.filter((g) => map.has(g)).map((g) => [g, map.get(g)!] as const);
  }, [settings.data]);

  const dirtyKeys = Object.keys(draft).filter((k) => {
    const item = settings.data?.items.find((i) => i.key === k);
    return item && draft[k] !== item.value;
  });

  if (settings.isLoading) {
    return <p className="text-sm text-[var(--color-muted)]">读取中…</p>;
  }

  return (
    <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="flex items-center gap-2 text-sm font-medium">
            <Settings size={14} />
            提取参数
          </h3>
          <p className="mt-1 text-xs text-[var(--color-muted)]">
            改动立即生效，不需要重启。窗口小的模型建议调低截断相关的值。
          </p>
        </div>
        <button
          onClick={() => reset.mutate()}
          disabled={reset.isPending}
          className={ghostBtn}
          style={{ borderColor: "var(--color-border)" }}
        >
          <RotateCcw size={13} />
          恢复默认
        </button>
      </div>

      <div className="mt-3 space-y-4">
        {grouped.map(([group, items]) => (
          <div key={group}>
            <p className="mb-1 text-xs font-medium uppercase tracking-wide text-[var(--color-muted)]">
              {group}
            </p>
            <div className="divide-y divide-[var(--color-border)]">
              {items.map((item) => (
                <SettingField
                  key={item.key}
                  item={item}
                  draft={draft[item.key]}
                  onChange={(v) => setDraft((d) => ({ ...d, [item.key]: v }))}
                />
              ))}
            </div>
          </div>
        ))}
      </div>

      {err && (
        <p className="mt-3 flex items-start gap-2 rounded-md border border-[var(--color-err)]/30 bg-[var(--color-err)]/10 p-2 text-xs text-[var(--color-err)]">
          <TriangleAlert size={14} className="mt-0.5 shrink-0" />
          {err}
        </p>
      )}

      <div className="mt-3 flex items-center gap-3">
        <button
          onClick={() => save.mutate(Object.fromEntries(dirtyKeys.map((k) => [k, draft[k]])))}
          disabled={save.isPending || dirtyKeys.length === 0}
          className={primaryBtn}
          style={{ background: "var(--color-accent)" }}
        >
          {save.isPending && <Loader2 size={13} className="animate-spin" />}
          保存{dirtyKeys.length > 0 ? `（${dirtyKeys.length} 项）` : ""}
        </button>
        {dirtyKeys.length > 0 && (
          <button
            onClick={() => setDraft({})}
            className="text-xs text-[var(--color-muted)] underline hover:text-[var(--color-text)]"
          >
            放弃改动
          </button>
        )}
        {saved && (
          <span className="inline-flex items-center gap-1 text-xs text-[var(--color-ok)]">
            <Check size={12} />
            已保存并生效
          </span>
        )}
      </div>
    </section>
  );
}

// ── 主面板 ──

export default function MemoryPanel() {
  const [activeTab, setActiveTab] = useState<"list" | "search" | "vectors" | "settings">(
    "list",
  );
  const [scope, setScope] = useState<MemoryScope>({
    agent_id: "",
    session_id: "",
    memory_type: "",
  });

  // 范围筛选需要列出智能体和会话
  const { data: agents } = useQuery({
    queryKey: ["agents", "all"],
    queryFn: () => api.agents.list(),
  });
  const { data: sessions } = useQuery({
    queryKey: ["sessions", "scope"],
    queryFn: () => api.listSessions({ size: 100 }),
  });

  const agentOptions = (agents ?? []).map((a) => ({ id: a.id, name: a.name }));
  const sessionOptions = (sessions?.items ?? []).map((s) => ({
    id: s.id,
    title: s.title || "未命名会话",
  }));

  const showScope = activeTab === "list" || activeTab === "search";

  const tabs = [
    { key: "list" as const, label: "记忆列表", icon: List },
    { key: "search" as const, label: "搜索", icon: Search },
    { key: "vectors" as const, label: "向量", icon: Database },
    { key: "settings" as const, label: "设置", icon: Settings },
  ];

  return (
    <div className="space-y-3">
      {/* 子页切换 */}
      <div className="flex gap-1 border-b border-[var(--color-border)]">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={clsx(
              "flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm transition-colors",
              "-mb-px",
              activeTab === tab.key
                ? "border-[var(--color-accent)] text-[var(--color-text)]"
                : "border-transparent text-[var(--color-muted)] hover:text-[var(--color-text)]",
            )}
          >
            <tab.icon size={14} />
            {tab.label}
          </button>
        ))}
      </div>

      {/* 范围筛选条：列表和搜索共用 */}
      {showScope && (
        <ScopeBar
          scope={scope}
          onChange={setScope}
          agents={agentOptions}
          sessions={sessionOptions}
        />
      )}

      {/* 内容区 */}
      {activeTab === "list" && <MemoryListView scope={scope} />}
      {activeTab === "search" && <SearchSection scope={scope} />}
      {activeTab === "vectors" && <VectorSection />}
      {activeTab === "settings" && <SettingsSection />}
    </div>
  );
}
