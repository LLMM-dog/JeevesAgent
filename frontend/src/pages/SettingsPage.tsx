import { useRef } from "react";
import { useSearchParams } from "react-router-dom";
import clsx from "clsx";
import McpPanel from "@/components/McpPanel";
import DeployPanel from "@/components/DeployPanel";
import WebSearchPanel from "@/components/WebSearchPanel";
import MemoryPanel from "@/components/MemoryPanel";
import SkillsPanel from "@/components/SkillsPanel";
import TracePanel from "@/components/TracePanel";
import ModelsPanel from "@/components/ModelsPanel";
import AgentsPanel from "@/components/AgentsPanel";
import WhitelistPanel from "@/components/WhitelistPanel";
import WorkspacePanel from "@/components/WorkspacePanel";


// URL 里用英文 key，中文只用于显示 —— 中文 key 进 query string 会被
// percent-encode 成一长串乱码，分享链接和翻日志时都不可读。
const TABS = [
  { key: "models", label: "模型" },
  { key: "agents", label: "智能体" },
  { key: "skills", label: "技能" },
  { key: "mcp", label: "MCP" },
  { key: "memory", label: "记忆" },
  { key: "files", label: "文件访问" },
  { key: "web", label: "联网" },
  { key: "trace", label: "追踪" },
  { key: "deploy", label: "部署" },
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
      <div className="mx-auto max-w-7xl space-y-5 px-6 py-6">
        <h1 className="text-lg font-medium">设置</h1>

        <SettingsTabs active={active} onSelect={selectTab} />

        {active === "models" && (
          <div {...panelProps("models")}>
            <ModelsPanel />
          </div>
        )}

        {active === "agents" && (
          <div {...panelProps("agents")}>
            <AgentsPanel />
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

        {active === "files" && (
          <div {...panelProps("files")}>
            <WorkspacePanel />
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

        {active === "deploy" && (
          <div {...panelProps("deploy")}>
            <DeployPanel />
          </div>
        )}
      </div>
    </div>
  );
}
