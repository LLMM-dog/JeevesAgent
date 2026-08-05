import { useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleAlert, Loader2, Trash2, TriangleAlert } from "lucide-react";
import clsx from "clsx";
import McpPanel from "@/components/McpPanel";
import WebSearchPanel from "@/components/WebSearchPanel";
import MemoryPanel from "@/components/MemoryPanel";
import VisionPanel from "@/components/VisionPanel";
import SkillsPanel from "@/components/SkillsPanel";
import TracePanel from "@/components/TracePanel";
import PersonaPanel from "@/components/PersonaPanel";
import WhitelistPanel from "@/components/WhitelistPanel";
import { api } from "@/lib/api";
import { ApiError } from "@/lib/sse";
import type { ProbedModel, Purpose } from "@/lib/types";

const PURPOSES: { key: Purpose; label: string; hint: string }[] = [
  { key: "chat", label: "对话", hint: "主对话模型，必填" },
  { key: "title", label: "标题", hint: "生成会话标题，可用便宜的小模型" },
  { key: "compact", label: "压缩", hint: "上下文摘要，需要长上下文" },
  { key: "vision", label: "视觉", hint: "识图，需要支持图片输入的模型" },
  { key: "embedding", label: "嵌入", hint: "向量检索，M5 用" },
];

function AddProvider({ onDone }: { onDone: () => void }) {
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [models, setModels] = useState<ProbedModel[] | null>(null);
  const [normalized, setNormalized] = useState("");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [err, setErr] = useState<{ message: string; hint?: string | null } | null>(
    null,
  );

  const probe = useMutation({
    mutationFn: () => api.probe(baseUrl, apiKey),
    onSuccess: (r) => {
      setErr(null);
      setModels(r.models);
      setNormalized(r.normalized_base_url);
      // 默认勾选看起来能对话的，非对话模型（嵌入/TTS）不勾
      setPicked(
        new Set(r.models.filter((m) => !m.looks_non_chat).map((m) => m.model_id)),
      );
      if (!name) {
        try {
          setName(new URL(r.normalized_base_url).hostname.replace(/^api\./, ""));
        } catch {
          /* 地址不合法时不自动填名字 */
        }
      }
    },
    onError: (e) =>
      setErr(
        e instanceof ApiError
          ? { message: e.message, hint: e.hint }
          : { message: String(e) },
      ),
  });

  const save = useMutation({
    mutationFn: () =>
      api.createProvider({
        name,
        base_url: baseUrl,
        api_key: apiKey,
        models: (models ?? [])
          .filter((m) => picked.has(m.model_id))
          .map((m) => ({
            model_id: m.model_id,
            context_window: m.context_window,
          })),
      }),
    onSuccess: () => {
      setName("");
      setBaseUrl("");
      setApiKey("");
      setModels(null);
      setPicked(new Set());
      onDone();
    },
    onError: (e) =>
      setErr(
        e instanceof ApiError
          ? { message: e.message, hint: e.hint }
          : { message: String(e) },
      ),
  });

  const defaultCount = (models ?? []).filter(
    (m) => picked.has(m.model_id) && m.window_source === "default",
  ).length;

  return (
    <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <h2 className="mb-3 text-sm font-medium">添加供应商</h2>

      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block">
          <span className="mb-1 block text-xs text-[var(--color-muted)]">
            接口地址
          </span>
          <input
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="https://api.deepseek.com"
            className="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5 text-sm outline-none focus:border-[var(--color-accent)]"
          />
          <span className="mt-1 block text-[11px] text-[var(--color-muted)]">
            不用手填 /v1，会自动补全
          </span>
        </label>

        <label className="block">
          <span className="mb-1 block text-xs text-[var(--color-muted)]">
            API Key
          </span>
          <input
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            type="password"
            placeholder="sk-..."
            autoComplete="off"
            className="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5 font-mono text-sm outline-none focus:border-[var(--color-accent)]"
          />
          <span className="mt-1 block text-[11px] text-[var(--color-muted)]">
            加密后存本地，界面上只显示尾 4 位
          </span>
        </label>
      </div>

      <button
        type="button"
        onClick={() => probe.mutate()}
        disabled={!baseUrl || probe.isPending}
        className="mt-3 flex items-center gap-2 rounded-lg bg-[var(--color-accent)] px-3 py-1.5 text-sm font-medium text-white transition hover:brightness-110 disabled:opacity-50"
      >
        {probe.isPending && <Loader2 size={14} className="animate-spin" aria-hidden />}
        {probe.isPending ? "探测中…" : "探测可用模型"}
      </button>

      {err && (
        <div
          role="alert"
          className="mt-3 rounded-lg border border-[var(--color-err)]/40 bg-[var(--color-err)]/10 px-3 py-2 text-sm text-[var(--color-err)]"
        >
          <p className="flex items-center gap-1.5 font-medium">
            <CircleAlert size={14} aria-hidden />
            {err.message}
          </p>
          {err.hint && <p className="mt-0.5 text-xs opacity-80">{err.hint}</p>}
        </div>
      )}

      {models && (
        <div className="mt-4">
          <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
            <span className="text-[var(--color-muted)]">
              找到 {models.length} 个模型，已勾选 {picked.size} 个
            </span>
            {/* 规范化后的地址要回显 —— 用户填的可能被改过 */}
            {normalized !== baseUrl && (
              <span className="rounded bg-[var(--color-surface-2)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--color-muted)]">
                实际使用 {normalized}
              </span>
            )}
          </div>

          {defaultCount > 0 && (
            <p className="mb-2 flex items-start gap-1.5 rounded-lg border border-[var(--color-warn)]/40 bg-[var(--color-warn)]/10 px-2.5 py-1.5 text-[11px] text-[var(--color-warn)]">
              <TriangleAlert size={13} aria-hidden className="mt-0.5 shrink-0" />
              <span>
                有 {defaultCount} 个模型没匹配到已知的上下文窗口，用了 32K
                默认值。窗口设小了会过早压缩，设大了会直接报错——
                建议保存后到模型列表里手动改成正确的值。
              </span>
            </p>
          )}

          <ul className="max-h-72 overflow-y-auto rounded-lg border border-[var(--color-border)]">
            {models.map((m) => (
              <li
                key={m.model_id}
                className="flex items-center gap-2 border-b border-[var(--color-border)] px-2.5 py-1.5 last:border-b-0"
              >
                <input
                  type="checkbox"
                  id={`m-${m.model_id}`}
                  checked={picked.has(m.model_id)}
                  onChange={(e) => {
                    const next = new Set(picked);
                    if (e.target.checked) next.add(m.model_id);
                    else next.delete(m.model_id);
                    setPicked(next);
                  }}
                  className="shrink-0 accent-[var(--color-accent)]"
                />
                <label
                  htmlFor={`m-${m.model_id}`}
                  className="min-w-0 flex-1 cursor-pointer truncate font-mono text-xs"
                >
                  {m.model_id}
                </label>
                <span
                  className={clsx(
                    "shrink-0 text-[11px]",
                    m.window_source === "default"
                      ? "text-[var(--color-warn)]"
                      : "text-[var(--color-muted)]",
                  )}
                >
                  {(m.context_window / 1000).toFixed(0)}K
                  {m.window_source === "default" && "?"}
                </span>
                {m.looks_non_chat && (
                  <span className="shrink-0 rounded bg-[var(--color-surface-2)] px-1.5 py-0.5 text-[10px] text-[var(--color-muted)]">
                    非对话
                  </span>
                )}
              </li>
            ))}
          </ul>

          <div className="mt-3 flex items-end gap-2">
            <label className="block flex-1">
              <span className="mb-1 block text-xs text-[var(--color-muted)]">
                供应商名称
              </span>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="deepseek"
                className="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5 text-sm outline-none focus:border-[var(--color-accent)]"
              />
            </label>
            <button
              type="button"
              onClick={() => save.mutate()}
              disabled={!name || picked.size === 0 || save.isPending}
              className="rounded-lg bg-[var(--color-ok)]/90 px-3 py-1.5 text-sm font-medium text-black transition hover:brightness-110 disabled:opacity-50"
            >
              {save.isPending ? "保存中…" : `保存 ${picked.size} 个模型`}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

// URL 里用英文 key，中文只用于显示 —— 中文 key 进 query string 会被
// percent-encode 成一长串乱码，分享链接和翻日志时都不可读。
const TABS = [
  { key: "models", label: "模型" },
  { key: "persona", label: "人格与偏好" },
  { key: "skills", label: "技能" },
  { key: "mcp", label: "MCP" },
  { key: "memory", label: "记忆" },
  { key: "files", label: "文件访问" },
  { key: "web", label: "联网" },
  { key: "trace", label: "追踪" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

const DEFAULT_TAB: TabKey = "models";

function isTabKey(v: string | null): v is TabKey {
  return TABS.some((t) => t.key === v);
}

function SettingsTabs({
  active,
  onSelect,
}: {
  active: TabKey;
  onSelect: (key: TabKey) => void;
}) {
  // 方向键切换要把焦点也搬过去，否则视觉焦点和键盘焦点会脱节：
  // 用户按右键看到高亮移动了，再按回车却触发的是旧标签。
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});

  const move = (delta: number) => {
    const i = TABS.findIndex((t) => t.key === active);
    // 环绕而不是撞墙停住，这是 tablist 的既定行为
    const next = TABS[(i + delta + TABS.length) % TABS.length];
    onSelect(next.key);
    refs.current[next.key]?.focus();
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowRight") {
      e.preventDefault();
      move(1);
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      move(-1);
    } else if (e.key === "Home") {
      e.preventDefault();
      onSelect(TABS[0].key);
      refs.current[TABS[0].key]?.focus();
    } else if (e.key === "End") {
      e.preventDefault();
      const last = TABS[TABS.length - 1];
      onSelect(last.key);
      refs.current[last.key]?.focus();
    }
  };

  return (
    // 窄屏横向滚动而不是换行：换行会把内容整体往下推，
    // 且标签行数随视口跳变，位置记不住。scrollbar-none 让它看起来像原生标签栏。
    <div className="-mx-4 overflow-x-auto px-4 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
      <div
        role="tablist"
        aria-label="设置分类"
        onKeyDown={onKeyDown}
        className="flex w-max gap-1 border-b border-[var(--color-border)]"
      >
        {TABS.map((t) => {
          const selected = t.key === active;
          return (
            <button
              key={t.key}
              ref={(el) => {
                refs.current[t.key] = el;
              }}
              type="button"
              role="tab"
              id={`tab-${t.key}`}
              aria-selected={selected}
              aria-controls={`panel-${t.key}`}
              // 未选中的标签移出 Tab 序列，一次 Tab 跳过整个标签栏进入面板；
              // 逐个 Tab 过去在有 7 个标签时很折磨。
              tabIndex={selected ? 0 : -1}
              onClick={() => onSelect(t.key)}
              className={clsx(
                "shrink-0 whitespace-nowrap rounded-t-lg px-3 py-2 text-sm transition",
                "-mb-px border-b-2",
                selected
                  ? "border-[var(--color-accent)] text-[var(--color-text)]"
                  : "border-transparent text-[var(--color-muted)] hover:bg-[var(--color-surface-2)]",
              )}
            >
              {t.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

export default function SettingsPage() {
  const qc = useQueryClient();
  const { data: providers } = useQuery({
    queryKey: ["providers"],
    queryFn: api.listProviders,
  });
  const { data: models } = useQuery({
    queryKey: ["models"],
    queryFn: () => api.listModels(),
  });
  const { data: bindings } = useQuery({
    queryKey: ["bindings"],
    queryFn: api.listBindings,
  });

  const refreshAll = () => {
    void qc.invalidateQueries({ queryKey: ["providers"] });
    void qc.invalidateQueries({ queryKey: ["models"] });
    void qc.invalidateQueries({ queryKey: ["bindings"] });
    void qc.invalidateQueries({ queryKey: ["meta"] });
  };

  const setBinding = useMutation({
    mutationFn: ({ purpose, model_pk }: { purpose: Purpose; model_pk: string }) =>
      api.setBinding(purpose, model_pk),
    onSuccess: refreshAll,
  });

  const removeProvider = useMutation({
    mutationFn: (id: string) => api.deleteProvider(id),
    onSuccess: refreshAll,
  });

  const byPurpose = new Map(bindings?.items.map((b) => [b.purpose, b]) ?? []);
  const providerName = new Map(providers?.items.map((p) => [p.id, p.name]) ?? []);

  // 当前标签放 URL 而不是 state：这样「把设置页某个标签发给别人」和刷新后
  // 停在原处都能成立。项目约定前端不写 localStorage（状态归后端），
  // URL 正好是唯一可用且天然可分享的持久化位置。
  const [params, setParams] = useSearchParams();
  const raw = params.get("tab");
  // 非法或缺失的 tab 值一律回落到默认标签，而不是渲染空白页 ——
  // 手改 URL 或旧书签指向已删除的标签时不该白屏。
  const active: TabKey = isTabKey(raw) ? raw : DEFAULT_TAB;

  const selectTab = (key: TabKey) => {
    const next = new URLSearchParams(params);
    next.set("tab", key);
    // replace：切标签是浏览行为不是导航，堆进历史会让「返回」要按七八次才离开设置页
    setParams(next, { replace: true });
  };

  // 只挂载当前标签的面板，避免七个面板的请求在进页面时一起打出去。
  // 代价是切走再切回会丢面板内的临时输入，权衡后接受。
  const panelProps = (key: TabKey) => ({
    role: "tabpanel",
    id: `panel-${key}`,
    "aria-labelledby": `tab-${key}`,
    className: "space-y-5",
  });

  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto max-w-3xl space-y-5 px-4 py-6">
        <h1 className="text-lg font-medium">设置</h1>

        <SettingsTabs active={active} onSelect={selectTab} />

        {active === "models" && (
          <div {...panelProps("models")}>
            <AddProvider onDone={refreshAll} />

            {providers && providers.items.length > 0 && (
              <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
                <h2 className="mb-3 text-sm font-medium">已配置的供应商</h2>
                <ul className="space-y-2">
                  {providers.items.map((p) => (
                    <li
                      key={p.id}
                      className="flex items-center gap-3 rounded-lg border border-[var(--color-border)] px-3 py-2"
                    >
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm">{p.name}</p>
                        <p className="truncate font-mono text-[11px] text-[var(--color-muted)]">
                          {p.base_url} · Key ····{p.key_hint} · {p.model_count} 个模型
                        </p>
                      </div>
                      <button
                        type="button"
                        aria-label={`删除供应商 ${p.name}`}
                        onClick={() => {
                          if (
                            confirm(
                              `删除「${p.name}」？它的所有模型和绑定也会一起删除。`,
                            )
                          ) {
                            removeProvider.mutate(p.id);
                          }
                        }}
                        className="shrink-0 rounded p-1.5 text-[var(--color-muted)] transition hover:text-[var(--color-err)]"
                      >
                        <Trash2 size={14} aria-hidden />
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {models && models.items.length > 0 && (
              <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
                <h2 className="mb-1 text-sm font-medium">功能位绑定</h2>
                <p className="mb-3 text-xs text-[var(--color-muted)]">
                  未绑定的功能位会回落到对话模型，回落时会有提示。
                </p>
                <div className="space-y-2">
                  {PURPOSES.map((p) => (
                    <div key={p.key} className="flex items-center gap-3">
                      <div className="w-28 shrink-0">
                        <p className="text-sm">{p.label}</p>
                        <p className="text-[11px] text-[var(--color-muted)]">
                          {p.hint}
                        </p>
                      </div>
                      <select
                        value={byPurpose.get(p.key)?.model_pk ?? ""}
                        onChange={(e) => {
                          if (e.target.value) {
                            setBinding.mutate({
                              purpose: p.key,
                              model_pk: e.target.value,
                            });
                          }
                        }}
                        aria-label={`${p.label}功能位的模型`}
                        className="min-w-0 flex-1 rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1.5 text-sm outline-none focus:border-[var(--color-accent)]"
                      >
                        <option value="">（未绑定）</option>
                        {models.items.map((m) => (
                          <option key={m.id} value={m.id}>
                            {providerName.get(m.provider_id) ?? "?"} / {m.model_id}
                            {" · "}
                            {(m.context_window / 1000).toFixed(0)}K
                          </option>
                        ))}
                      </select>
                    </div>
                  ))}
                </div>
              </section>
            )}

            <VisionPanel />
          </div>
        )}

        {active === "skills" && (
          <div {...panelProps("skills")}>
            <SkillsPanel />
          </div>
        )}

        {active === "mcp" && (
          <div {...panelProps("mcp")}>
            <McpPanel />
          </div>
        )}

        {active === "memory" && (
          <div {...panelProps("memory")}>
            <MemoryPanel />
          </div>
        )}

        {active === "persona" && (
          <div {...panelProps("persona")}>
            <PersonaPanel />
          </div>
        )}

        {active === "files" && (
          <div {...panelProps("files")}>
            <WhitelistPanel />
          </div>
        )}

        {active === "web" && (
          <div {...panelProps("web")}>
            <WebSearchPanel />
          </div>
        )}

        {active === "trace" && (
          <div {...panelProps("trace")}>
            <TracePanel />
          </div>
        )}
      </div>
    </div>
  );
}
