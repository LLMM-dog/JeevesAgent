import { useEffect, useRef, useState } from "react";
import {
  Eye,
  File,
  ImagePlus,
  Mic,
  MicOff,
  Send,
  ShieldCheck,
  Sparkles,
  Square,
  Wrench,
  X,
  Zap,
} from "lucide-react";
import clsx from "clsx";
import { MacroPicker } from "@/components/MacroPicker";
import { RefPicker, type RefCandidate, type RefKind } from "@/components/RefPicker";
import { ModelSwitcher } from "@/components/ModelSwitcher";
import { WorkDirPicker } from "@/components/WorkDirPicker";
import { useChatStore } from "@/store/chat";
import { speechUploadsAudio, useSpeechInput } from "@/hooks/useSpeechInput";

/**
 * 触发符检测。
 *
 * ## 为什么全角和半角都要认
 *
 * 中文输入法下 `！`、`＃`、`＠` 默认出全角。只认半角的话用户按了没反应，
 * 而且**完全想不到是因为标点宽度** —— 这类问题排查成本极高，支持成本极低。
 *
 * 认了 `！`说明作者遇到过，
 * 但没认 `＠` 和 `＃` —— 认了一半。
 *
 * ## 为什么要判前一个字符
 *
 * `foo@bar.com`、`@decorator`、`arr[#i]` 里的触发符都不该弹面板。
 * 不判的话用户输邮箱时面板一直跳。
 */
const TRIGGERS: { chars: string[]; kind: RefKind }[] = [
  { chars: ["@"], kind: "file" },
  { chars: ["＠"], kind: "file" },
  { chars: ["#", "＃"], kind: "tool" },
];

interface TriggerHit {
  kind: RefKind;
  /** 触发符在文本中的下标 */
  pos: number;
  /** 触发符后已输入的过滤词 */
  query: string;
}

function detectTrigger(text: string, cursor: number): TriggerHit | null {
  const before = text.slice(0, cursor);
  let best: TriggerHit | null = null;
  let bestPos = -1;

  for (const { chars, kind } of TRIGGERS) {
    for (const ch of chars) {
      const idx = before.lastIndexOf(ch);
      // 取最近者 —— "@a #b" 连续输入时，光标前最后一个才是正在编辑的
      if (idx <= bestPos) continue;

      const prev = idx === 0 ? " " : before[idx - 1];
      // 前一个字符是单词字符 → 是邮箱/装饰器之类，不是引用
      if (/[\w]/.test(prev)) continue;

      const query = before.slice(idx + ch.length);
      // 查询词里有空格或换行说明这个触发符已经"过期"了
      if (/[\s\n]/.test(query)) continue;

      bestPos = idx;
      best = { kind, pos: idx, query };
    }
  }
  return best;
}

/** 单轮图片数上限。与后端 vision.MAX_IMAGES_PER_TURN 一致 */
const MAX_IMAGES = 5;
/** 单张 8MB。与后端 vision.MAX_IMAGE_BYTES 一致 */
const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
const ALLOWED_MIME = new Set([
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/gif",
]);

function fileToDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result));
    r.onerror = () => reject(new Error("读取失败"));
    r.readAsDataURL(file);
  });
}

/** 一个已确认的引用，UI 上是个 chip */
interface Chip {
  type: "file" | "skill" | "tool" | "macro";
  label: string;
  /** file 用 path，其余用 name */
  path?: string;
  name?: string;
}

export default function Composer({ disabled }: { disabled?: boolean }) {
  const [text, setText] = useState("");
  const [chips, setChips] = useState<Chip[]>([]);
  const [images, setImages] = useState<string[]>([]);
  const [imgError, setImgError] = useState<string | null>(null);
  const [trigger, setTrigger] = useState<TriggerHit | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const ref = useRef<HTMLTextAreaElement>(null);
  const send = useChatStore((s) => s.send);
  const visionMode = useChatStore((s) => s.visionMode);
  const setVisionMode = useChatStore((s) => s.setVisionMode);
  const stop = useChatStore((s) => s.stop);
  const sessionId = useChatStore((s) => s.sessionId);
  const workDir = useChatStore((s) => s.workDir);
  const modelPk = useChatStore((s) => s.modelPk);
  const approvalMode = useChatStore((s) => s.approvalMode);
  const setApprovalMode = useChatStore((s) => s.setApprovalMode);
  const pending = useChatStore((s) => s.pending);
  const streaming = useChatStore((s) => s.streaming);
  const usage = useChatStore((s) => s.usage);

  const busy = pending || streaming !== null;

  // 语音输入。
  //
  // 识别到的文字插在【光标处】而不是追加到末尾 ——
  // 用户可能是在已有文字中间补一段。
  const speech = useSpeechInput((spoken) => {
    const el = ref.current;
    setText((prev) => {
      if (!el) return prev + spoken;
      const at = el.selectionStart ?? prev.length;
      const next = prev.slice(0, at) + spoken + prev.slice(at);
      // 光标移到插入内容之后。
      //
      // 放 requestAnimationFrame 里：此刻 React 还没把新值写进 DOM，
      // 直接设 selectionStart 会被随后的重渲染覆盖回去
      const pos = at + spoken.length;
      requestAnimationFrame(() => {
        el.focus();
        el.setSelectionRange(pos, pos);
      });
      return next;
    });
  });

  // 自适应高度。上限 200px，超过就内部滚动。
  //
  // interim 也要触发 —— 临时文本显示在输入框下方，
  // 它出现/消失时容器高度会变
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [text]);

  const submit = () => {
    const v = text.trim();
    // 只有图片或只有引用也允许发 —— "看看这张图"、"读一下这个文件"
    // 这种场景下强制要求文字是多余的负担
    if ((!v && images.length === 0 && chips.length === 0) || busy || disabled) return;
    setText("");
    const imgs = images;
    const rs = chips.map((c) =>
      c.type === "file"
        ? { type: "file", path: c.path }
        : { type: c.type, name: c.name },
    );
    setImages([]);
    setChips([]);
    setImgError(null);
    void send(v, imgs, rs);
  };

  /**
   * 确认一个候选项 → 转成 chip。
   *
   * ## 为什么删掉输入框里的文字
   *
   * 引用转 chip 而不是留 `@src/main.py` 在输入框里，三个好处：
   * 不占输入框空间（长路径会把输入框撑爆）、可单独删除（纯文本方案里
   * 要在文本中找那段字符）、结构化数据不会被误改（纯文本方案里用户删掉
   * `@` 后一个字母，引用就静默失效了）。
   *
   * 也是这么做的，是它最好的
   * 交互决定。同类实现 插纯文本是因为 TUI 没有 chip 这个选项。
   */
  const pickRef = (hit: TriggerHit, item: RefCandidate) => {
    const el = ref.current;
    const cursor = el?.selectionStart ?? text.length;
    // 把触发符到光标之间的文字删掉
    setText(text.slice(0, hit.pos) + text.slice(cursor));
    setChips((prev) => {
      const chip: Chip =
        hit.kind === "file"
          ? { type: "file", label: item.name, path: item.path }
          : { type: hit.kind, label: item.name, name: item.name };
      // 去重：同一个文件引用两次没有意义
      const key = chip.path ?? chip.name;
      if (prev.some((c) => (c.path ?? c.name) === key)) return prev;
      return [...prev, chip];
    });
    setTrigger(null);
    ref.current?.focus();
  };

  /**
   * 收图片。粘贴、拖拽、选文件三条路都走这里。
   *
   * 前端校验只为了快速反馈 —— 服务端会再校验一遍（含魔数）。
   * 只信前端校验等于没校验。
   */
  const addFiles = async (files: File[]) => {
    setImgError(null);
    if (!visionMode) {
      setImgError("请先开启视觉模式，否则图片会被丢弃");
      return;
    }
    const pics = files.filter((f) => f.type.startsWith("image/"));
    if (pics.length === 0) return;

    const accepted: string[] = [];
    for (const f of pics) {
      if (!ALLOWED_MIME.has(f.type)) {
        setImgError(`不支持 ${f.type || f.name}，可用 PNG / JPEG / WebP / GIF`);
        continue;
      }
      if (f.size > MAX_IMAGE_BYTES) {
        setImgError(
          `${f.name} 有 ${(f.size / 1024 / 1024).toFixed(1)}MB，超过 8MB 上限`,
        );
        continue;
      }
      if (images.length + accepted.length >= MAX_IMAGES) {
        setImgError(`单轮最多 ${MAX_IMAGES} 张，多余的已忽略`);
        break;
      }
      try {
        accepted.push(await fileToDataUrl(f));
      } catch {
        setImgError(`${f.name} 读取失败`);
      }
    }
    if (accepted.length > 0) setImages((prev) => [...prev, ...accepted]);
  };

  // 以 ! 或 ！开头时弹宏提词器。
  //
  // 全角 ！ 必须支持：中文输入法下打感叹号默认出来的就是全角。只认半角的话
  // 中文用户按了没反应，而且完全想不到是因为标点宽度。
  const macroTrigger = /^[!！]/.test(text);
  const macroQuery = macroTrigger ? text.slice(1) : "";

  return (
    <div className="border-t border-[var(--color-border)] bg-[var(--color-surface)]/50 px-4 py-3">
      <div className="relative mx-auto max-w-3xl">
        {/* 引用提词器。宏提词器只在开头触发，引用可以在任意位置 */}
        {trigger && !busy && !macroTrigger && (
          <RefPicker
            kind={trigger.kind}
            query={trigger.query}
            onPick={(item) => pickRef(trigger, item)}
            onClose={() => setTrigger(null)}
          />
        )}

        {macroTrigger && !busy && (
          <MacroPicker
            query={macroQuery}
            onPick={(body) => {
              // 宏正文直接填进输入框，让用户能看见、能改再发。
              //
              // 不直接发送：宏是流程模板，用户通常要补充具体参数
              //（"整理日报" 得说清是哪一天）。直接发出去等于逼他在
              // 下一轮再补，白烧一轮。
              setText(body);
              ref.current?.focus();
            }}
            onClose={() => setText("")}
          />
        )}

        {usage && (
          <div className="mb-2 flex items-center gap-2 text-[11px] text-[var(--color-muted)]">
            <div className="h-1 flex-1 overflow-hidden rounded-full bg-[var(--color-border)]">
              <div
                className="h-full rounded-full transition-all"
                style={{
                  width: `${Math.min(100, usage.ratio * 100)}%`,
                  background:
                    usage.ratio > 0.75
                      ? "var(--color-warn)"
                      : "var(--color-accent)",
                }}
              />
            </div>
            <span
              title={
                usage.tools_tokens
                  ? `工具定义 ${usage.tools_tokens.toLocaleString()}` +
                    (usage.tool_count ? `（${usage.tool_count} 个）` : "") +
                    ` + 系统提示词 ${(usage.system_tokens ?? 0).toLocaleString()}` +
                    ` + 对话内容 ${Math.max(
                      0,
                      usage.used_tokens -
                        usage.tools_tokens -
                        (usage.system_tokens ?? 0),
                    ).toLocaleString()}` +
                    "\n\n（分项是按比例估的：本地分词器和模型的不一样，" +
                    "只有总数是模型给的准确值）" +
                    "\n\n前两项每一轮都会重发。数字偏大通常是工具太多，" +
                    "去设置页关掉用不到的 MCP 服务器。"
                  : undefined
              }
            >
              {usage.used_tokens.toLocaleString()} /{" "}
              {usage.window_tokens.toLocaleString()} token
              {/* 估算值必须标出来 —— 用户看到"上下文 80%"时应该知道
                  这是精确值还是估算。
                  反过来同样要紧：真实值【不能】标成估算，
                  否则用户会以为最可信的那个数字不可信。 */}
              {usage.is_estimate && "（估算）"}
              {/* 固定开销占大头时直接摆出来，不藏在 hover 里 ——
                  用户看到"你好 = 4551 token"的第一反应是关掉界面，
                  不是去 hover 一个数字。 */}
              {usage.tools_tokens != null &&
                usage.tools_tokens > 0 &&
                usage.used_tokens > 0 &&
                usage.tools_tokens / usage.used_tokens > 0.5 && (
                  <span style={{ color: "var(--color-muted)" }}>
                    {" "}
                    · 其中工具定义 {usage.tools_tokens.toLocaleString()}
                  </span>
                )}
            </span>
          </div>
        )}

        {/* 图片错误提示。放在预览上面 —— 它解释的是"为什么少了一张" */}
        {imgError && (
          <div
            role="alert"
            className="mb-2 rounded-lg bg-[var(--color-warn)]/10 px-2.5 py-1.5 text-[11px] text-[var(--color-warn)]"
          >
            {imgError}
          </div>
        )}

        {/* 语音错误。可点掉 —— 权限类错误改完设置要刷新，
            提示会一直留着挡视线 */}
        {speech.error && (
          <div
            role="alert"
            className="mb-2 flex items-start gap-2 rounded-lg bg-[var(--color-err)]/10 px-2.5 py-1.5 text-[11px] text-[var(--color-err)]"
          >
            <span className="flex-1">{speech.error}</span>
            <button
              type="button"
              onClick={speech.clearError}
              aria-label="关闭提示"
              className="shrink-0 underline"
            >
              知道了
            </button>
          </div>
        )}

        {/* 正在听的状态条。
            【必须有】—— 没有它的话按下按钮后好几秒界面毫无反应，
            用户不知道到底有没有在听、说的话有没有被识别到。
            aria-live 让读屏软件也能跟上。 */}
        {speech.listening && (
          <div
            aria-live="polite"
            className="mb-2 flex items-center gap-2 rounded-lg bg-[var(--color-accent)]/10 px-2.5 py-1.5 text-[11px] text-[var(--color-accent)]"
          >
            <span className="inline-block h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-[var(--color-accent)]" />
            <span className="flex-1 truncate">
              {speech.interim || "正在听……说完点一下麦克风"}
            </span>
          </div>
        )}

        {/* 引用 chip 条。可单独删 —— 这是 chip 相对纯文本的主要优势 */}
        {chips.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-1.5">
            {chips.map((c, i) => {
              const Icon =
                c.type === "file" ? File : c.type === "tool" ? Wrench : Sparkles;
              return (
                <span
                  key={`${c.type}:${c.path ?? c.name}`}
                  className="flex items-center gap-1 rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-1.5 py-0.5 text-[11px]"
                  title={c.path ?? c.name}
                >
                  <Icon size={10} className="shrink-0 text-[var(--color-muted)]" aria-hidden />
                  <span className="max-w-[160px] truncate">{c.label}</span>
                  <button
                    type="button"
                    onClick={() => setChips((p) => p.filter((_, j) => j !== i))}
                    aria-label={`移除引用 ${c.label}`}
                    className="text-[var(--color-muted)] hover:text-[var(--color-err)]"
                  >
                    <X size={9} aria-hidden />
                  </button>
                </span>
              );
            })}
          </div>
        )}

        {/* 已选图片的缩略图 */}
        {images.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-1.5">
            {images.map((url, i) => (
              <div key={i} className="group relative">
                <img
                  src={url}
                  alt={`待发送的图片 ${i + 1}`}
                  className="h-14 w-14 rounded-lg border border-[var(--color-border)] object-cover"
                />
                <button
                  type="button"
                  onClick={() => setImages((p) => p.filter((_, j) => j !== i))}
                  aria-label={`移除图片 ${i + 1}`}
                  className="absolute -right-1 -top-1 flex h-4 w-4 items-center justify-center rounded-full bg-[var(--color-err)] text-white opacity-0 transition group-hover:opacity-100"
                >
                  <X size={9} aria-hidden />
                </button>
              </div>
            ))}
          </div>
        )}

        <div
          onDragOver={(e) => {
            if (!visionMode) return;
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            void addFiles(Array.from(e.dataTransfer.files));
          }}
          className={`flex items-end gap-2 rounded-xl border bg-[var(--color-bg)] p-2 ${
            dragging
              ? "border-[var(--color-accent)] bg-[var(--color-accent)]/5"
              : "border-[var(--color-border)] focus-within:border-[var(--color-accent)]"
          }`}
        >
          <textarea
            ref={ref}
            value={text}
            onChange={(e) => {
              setText(e.target.value);
              // 每次输入重算触发状态。用 selectionStart 而非文本末尾 ——
              // 用户可能在中间插入 @，光标不在末尾。
              setTrigger(
                detectTrigger(e.target.value, e.target.selectionStart ?? 0),
              );
            }}
            onPaste={(e) => {
              // 粘贴图片。这是最常用的路径 —— 截图后直接 Ctrl+V。
              //
              // 只在有图片项时拦截，否则会破坏正常的文本粘贴。
              const files = Array.from(e.clipboardData.files);
              if (files.some((f) => f.type.startsWith("image/"))) {
                e.preventDefault();
                void addFiles(files);
              }
            }}
            onKeyDown={(e) => {
              // Enter 发送，Shift+Enter 换行。
              // 中文输入法组字期间的 Enter 是"确认候选词"，
              // 不能当发送 —— 否则每次选词都会误发。
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                submit();
              }
            }}
            rows={1}
            disabled={disabled}
            placeholder={
              disabled
                ? "请先在设置页配置模型"
                : "输入消息…… @文件 · #工具 · !宏，Enter 发送"
            }
            aria-label="消息输入框"
            className="max-h-[200px] min-h-[24px] flex-1 resize-none bg-transparent px-1.5 py-1 text-sm outline-none placeholder:text-[var(--color-muted)] disabled:cursor-not-allowed"
          />
          {/* 选图按钮只在视觉模式下出现 ——
              开关关着时给个能点但没用的按钮只会让人困惑 */}
          {visionMode && !busy && (
            <>
              <input
                ref={fileRef}
                type="file"
                accept="image/png,image/jpeg,image/webp,image/gif"
                multiple
                hidden
                onChange={(e) => {
                  void addFiles(Array.from(e.target.files ?? []));
                  e.target.value = "";
                }}
              />
              <button
                type="button"
                onClick={() => fileRef.current?.click()}
                disabled={disabled}
                aria-label="添加图片"
                title="添加图片（也可以直接粘贴或拖进来）"
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-[var(--color-muted)] transition hover:bg-[var(--color-surface)] disabled:opacity-40"
              >
                <ImagePlus size={14} aria-hidden />
              </button>
            </>
          )}
          {/* 麦克风按钮。
              【不支持时整个不渲染】—— 而不是渲染一个点了没反应的按钮。
              和"选图按钮只在视觉模式下出现"是同一个原则。
              SpeechRecognition 至今不是 Baseline，Firefox 长期不支持。 */}
          {speech.supported && !busy && (
            <button
              type="button"
              onClick={() => (speech.listening ? speech.stop() : speech.start())}
              disabled={disabled}
              aria-label={speech.listening ? "停止语音输入" : "语音输入"}
              aria-pressed={speech.listening}
              title={
                speech.listening
                  ? "点击停止"
                  : speechUploadsAudio()
                    ? "语音输入（音频会发到浏览器厂商的识别服务）"
                    : "语音输入"
              }
              className={clsx(
                "flex h-8 w-8 shrink-0 items-center justify-center rounded-lg transition disabled:opacity-40",
                speech.listening
                  ? "bg-[var(--color-err)]/20 text-[var(--color-err)]"
                  : "text-[var(--color-muted)] hover:bg-[var(--color-surface)]",
              )}
            >
              {speech.listening ? (
                <MicOff size={14} aria-hidden />
              ) : (
                <Mic size={14} aria-hidden />
              )}
            </button>
          )}
          {busy ? (
            <button
              type="button"
              onClick={() => void stop()}
              aria-label="停止生成"
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-[var(--color-err)]/20 text-[var(--color-err)] transition hover:bg-[var(--color-err)]/30"
            >
              <Square size={14} aria-hidden fill="currentColor" />
            </button>
          ) : (
            <button
              type="button"
              onClick={submit}
              disabled={
                (!text.trim() && images.length === 0 && chips.length === 0) ||
                disabled
              }
              aria-label="发送"
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-[var(--color-accent)] text-white transition hover:brightness-110 disabled:opacity-40"
            >
              <Send size={14} aria-hidden />
            </button>
          )}
        </div>
        <div className="mt-1.5 flex items-center gap-1.5 px-1">
          {/* 工作目录放在审批模式旁边：两者都是"这次对话的作用范围"，
              而且用户在发第一句之前就该看到它 */}
          {sessionId && (
            <WorkDirPicker sessionId={sessionId} workDir={workDir} />
          )}
          {sessionId && (
            <ModelSwitcher sessionId={sessionId} modelPk={modelPk} />
          )}
          <button
            type="button"
            onClick={() =>
              void setApprovalMode(approvalMode === "auto" ? "manual" : "auto")
            }
            // aria-pressed 而非 checked：这是个双态开关按钮，不是复选框
            aria-pressed={approvalMode === "auto"}
            title={
              approvalMode === "auto"
                ? "自动执行：危险操作不再逐个确认"
                : "逐个确认：执行命令、写文件前都会问你"
            }
            className={`flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] transition ${
              approvalMode === "auto"
                ? "bg-[var(--color-warn)]/15 text-[var(--color-warn)]"
                : "text-[var(--color-muted)] hover:bg-[var(--color-surface)]"
            }`}
          >
            {approvalMode === "auto" ? (
              <Zap size={11} aria-hidden />
            ) : (
              <ShieldCheck size={11} aria-hidden />
            )}
            {approvalMode === "auto" ? "自动执行" : "逐个确认"}
          </button>

          {/* 视觉模式开关。
              未核验的模型点了会拿到 400 并附带"去设置页核验"的提示 ——
              后端拦在那里，前端不重复判断（能力状态在设置页才拿得到）。 */}
          <button
            type="button"
            onClick={() => void setVisionMode(!visionMode)}
            aria-pressed={visionMode}
            title={
              visionMode
                ? "视觉模式已开：可以粘贴或拖入图片"
                : "开启视觉模式后可以发图片（需要模型支持）"
            }
            className={`flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] transition ${
              visionMode
                ? "bg-[var(--color-accent)]/15 text-[var(--color-accent)]"
                : "text-[var(--color-muted)] hover:bg-[var(--color-surface)]"
            }`}
          >
            <Eye size={11} aria-hidden />
            视觉
          </button>
        </div>
      </div>
    </div>
  );
}
