/**
 * 对话状态。
 *
 * ## 事件流 → UI 状态的映射
 *
 * 后端发的是【扁平事件流】，前端要还原成【气泡列表】。核心难点：
 * 一个 assistant 回复可能包含 思维链 + 正文 + 多个工具卡片，
 * 而它们是分散在事件流里到达的。
 *
 * 解法：维护一个 `streaming` 对象累积当前轮的所有部分，
 * `done` 事件时把它转成一条正式消息。
 */

import { create } from "zustand";
import { api } from "@/lib/api";
import { ApiError, startChat } from "@/lib/sse";
import type {
  ContextUsageEvent,
  MessageOut,
  SseEventMap,
  SseEventName,
  TodoItem,
  TodoStats,
} from "@/lib/types";

/** 一个工具调用在 UI 上的完整状态 */
export interface ToolCard {
  call_id: string;
  tool_name: string;
  args: Record<string, unknown>;
  status: "running" | "ok" | "error";
  duration_ms?: number;
  display?: Record<string, unknown> | null;
  content_preview?: string;
}

/** 正在流式生成的那一轮 */
export interface StreamingTurn {
  run_id: string;
  reasoning: string;
  content: string;
  tools: ToolCard[];
  /** 思维链是否展开。默认收起 —— 它通常很长且多数时候用户不关心 */
  reasoningOpen: boolean;
}

/**
 * 一个正在跑（或跑完）的子智能体。
 *
 * 子智能体的工具调用挂在它自己下面，不混进父代理的列表 ——
 * 否则用户看到 researcher 读的 12 个文件会以为是主代理读的，
 * "委派省了上下文"这件事在界面上完全看不出来。
 */
export interface AgentCard {
  span_id: string;
  agent_name: string;
  task: string;
  depth: number;
  status: "running" | "ok" | "error";
  tools: ToolCard[];
  turns?: number;
  /** 这个子智能体烧的 token。委派的成本必须可见，否则用户不知道值不值 */
  tokens?: number;
}

export interface BannerState {
  kind: "error" | "warn" | "info";
  code?: string;
  message: string;
  hint?: string | null;
  retryable?: boolean;
}

interface ChatState {
  sessionId: string | null;
  messages: MessageOut[];
  streaming: StreamingTurn | null;
  /** 正在等后端第一个事件。此时输入框已禁用但还没有 run_id 可取消 */
  pending: boolean;
  runId: string | null;
  banner: BannerState | null;
  usage: ContextUsageEvent | null;
  /** 本轮跑过的子智能体。每轮开始时清空 */
  activeAgents: AgentCard[];
  /** 本轮召回的记忆。每轮清空 */
  recalledMemories: { memory_id: string; theme: string; content: string; score: number }[];
  /** 非 null 表示正在压缩上下文。压缩要几秒，不显示会让人以为卡死 */
  compacting: { victim_count: number } | null;
  /**
   * 待审批的工具调用。
   *
   * 必须带完整 args —— 只显示工具名等于让用户盲签，
   * 审批一条命令时得能看到命令原文。
   */
  approval: {
    call_id: string;
    tool_name: string;
    args: Record<string, unknown>;
    /** 截止时刻（毫秒时间戳）。用绝对时刻而非剩余秒数：
     *  事件到达有网络延迟，标签页在后台还会被节流。 */
    timeout_at: number;
  } | null;
  /** manual 时每个危险操作都要人确认；auto 直接执行 */
  /**
   * 这次对话的工作目录。空串 = 未设置。
   *
   * 放 store 而不是每个组件各自查：Composer 要显示它，
   * RefPicker 的文件搜索也依赖它，两处必须看到同一个值。
   */
  workDir: string;
  approvalMode: "manual" | "auto";
  /** 视觉模式。开启后可发图片，但模型必须先通过核验 */
  visionMode: boolean;
  /** 当前工作成果的元信息（内容太大不放 store，需要时拉接口） */
  artifact: { kind: string; path: string | null; chars: number } | null;
  todos: TodoItem[];
  todoStats: TodoStats | null;
  title: string;

  cancelStream: (() => void) | null;

  openSession: (sessionId: string) => Promise<void>;
  send: (content: string, images?: string[], refs?: Record<string, unknown>[]) => Promise<void>;
  /** 从某条用户消息处截断并重发。删掉该条及其之后的全部，再用原内容重发 */
  retryFrom: (messageId: string) => Promise<void>;
  stop: () => Promise<void>;
  dismissBanner: () => void;
  toggleReasoning: () => void;
  respondApproval: (approved: boolean) => Promise<void>;
  setApprovalMode: (mode: "manual" | "auto") => Promise<void>;
  /** 设置这次对话的工作目录。传空串清除 */
  setWorkDir: (dir: string) => Promise<void>;
  setVisionMode: (on: boolean) => Promise<void>;
}

/**
 * 一条空白消息。
 *
 * MessageOut 有 20 多个字段，手写字面量会在后端加字段时到处漏 ——
 * 而 TS 只在 strict 下报错，不 strict 就静默漏掉。
 */
const blankMessage = (id: string): MessageOut => ({
  id,
  seq: Number.MAX_SAFE_INTEGER,
  role: "assistant",
  agent_name: "",
  content: "",
  reasoning: null,
  tool_calls: null,
  tool_call_id: null,
  tool_name: null,
  tool_display: null,
  is_error: false,
  refs: null,
  attachments: null,
  artifact_kind: null,
  artifact_path: null,
  run_id: null,
  span_id: null,
  prompt_tokens: null,
  completion_tokens: null,
  created_at: Date.now(),
});

const emptyStreaming = (run_id: string): StreamingTurn => ({
  run_id,
  reasoning: "",
  content: "",
  tools: [],
  reasoningOpen: false,
});

export const useChatStore = create<ChatState>((set, get) => ({
  sessionId: null,
  messages: [],
  streaming: null,
  pending: false,
  runId: null,
  banner: null,
  usage: null,
  activeAgents: [],
  recalledMemories: [],
  compacting: null,
  approval: null,
  workDir: "",
  approvalMode: "manual",
  visionMode: false,
  artifact: null,
  todos: [],
  todoStats: null,
  title: "",

  cancelStream: null,

  async openSession(sessionId) {
    // 切换会话时先断开上一个流的本地读取。
    // 注意这【不取消服务端的生成】—— 那是有意的，生成会跑完并落库。
    get().cancelStream?.();
    set({
      sessionId,
      messages: [],
      streaming: null,
      pending: false,
      runId: null,
      banner: null,
      activeAgents: [],
      recalledMemories: [],
      compacting: null,
      approval: null,
      artifact: null,
      cancelStream: null,
    });

    try {
      const [{ items }, session, todos] = await Promise.all([
        api.listMessages(sessionId),
        api.getSession(sessionId),
        api.listTodos(sessionId),
      ]);
      set({
        messages: items,
        title: session.title,
        todos: todos.items,
        todoStats: todos.stats,
        // 审批模式是会话级设置，落在库里。不读回来的话切换会话或刷新页面后
        // 开关会显示成 manual 而后端其实是 auto —— 用户会以为开关坏了。
        approvalMode: session.approval_mode ?? "manual",
        // 空串兜底：老会话在迁移前没有这个字段
        workDir: session.work_dir ?? "",
      visionMode: session.vision_mode ?? false,
      });
    } catch (err) {
      set({ banner: toBanner(err) });
    }
  },

  async send(content, images, refs) {
    const sessionId = get().sessionId;
    if (!sessionId || get().pending || get().streaming) return;

    // 乐观插入用户消息。后端也会立即落库，meta 事件回来后
    // 用真实 id 替换这条临时消息。
    const tempId = `tmp_${Date.now()}`;
    const optimistic: MessageOut = {
      ...blankMessage(tempId),
      role: "user",
      content,
      // 图片存 attachments，前端据此回显缩略图。
      // 它不参与后续轮次的 LLM 请求 —— 后端 row_to_msg 故意不还原，
      // 否则一张图会在每一轮被重发。
      attachments: images && images.length > 0 ? images : null,
    };
    set((s) => ({
      messages: [...s.messages, optimistic],
      pending: true,
      banner: null,
      // 每轮清空子智能体卡片。不清的话上一轮的委派记录会一直堆在界面上，
      // 用户分不清哪个是这一轮的。
      activeAgents: [],
      recalledMemories: [],
    }));

    const cancel = startChat(
      { session_id: sessionId, content, images: images ?? [], refs: refs ?? [] },
      {
        onEvent: (name, data) => handleEvent(set, get, name, data),
        onNetworkError: (err) => {
          set({
            banner: {
              kind: "error",
              message: `连接中断：${err.message}`,
              hint: "生成可能仍在后台继续。刷新页面可看到已保存的内容",
              retryable: true,
            },
          });
        },
        onClose: (reason) => {
          set({
            pending: false,
            cancelStream: null,
            compacting: null,
            approval: null,
            // 流结束时把 streaming 落成正式消息。
            // 即使是 aborted/network 也要落 —— 已经显示的内容不该消失。
            ...flushStreaming(get()),
          });
          if (reason === "network") {
            void get().openSession(sessionId);
          }
        },
      },
    );
    set({ cancelStream: cancel });
  },

  async stop() {
    const { runId } = get();
    if (!runId) return;
    try {
      // 先告诉服务端取消（这才真正停止生成），
      // 服务端会发 cancelled + done，本地流自然结束。
      await api.cancelRun(runId);
    } catch (err) {
      // 幂等：run 已结束时后端返回 404，不当错误处理
      if (!(err instanceof ApiError && err.status === 404)) {
        set({ banner: toBanner(err) });
      }
    }
  },

  async respondApproval(approved: boolean) {
    const { runId, approval } = get();
    if (!runId || !approval) return;
    // 先收弹窗再发请求：网络往返有延迟，不先收的话用户会以为没点上，
    // 然后连点几次 —— 后续的请求会拿到 409。
    set({ approval: null });
    try {
      await api.approve(runId, approval.call_id, approved);
    } catch (err) {
      // 409 表示这个审批已经有结论了（超时、或在别处批准过）。
      // 不当错误报 —— 用户能做的事就是等下一个弹窗。
      if (err instanceof ApiError && err.status === 409) return;
      // 404 表示 run 已经结束了，同理
      if (err instanceof ApiError && err.status === 404) return;
      set({ banner: toBanner(err) });
    }
  },

  async setWorkDir(dir) {
    const { sessionId } = get();
    if (!sessionId) return;
    // 不做乐观更新：后端要校验目录存在、还会顺手加一条白名单。
    // 先改本地的话，校验失败时界面显示的目录和实际生效的不一致 ——
    // 而那种不一致会让人以为"设了但没用"。
    const s = await api.patchSession(sessionId, { work_dir: dir });
    set({ workDir: s.work_dir ?? "" });
  },

  async setApprovalMode(mode) {
    const { sessionId } = get();
    if (!sessionId) return;
    // 乐观更新：切换是本地状态，失败再回滚。
    // 等往返完成才变的话，开关会有明显的迟滞感。
    const previous = get().approvalMode;
    set({ approvalMode: mode });
    try {
      await api.patchSession(sessionId, { approval_mode: mode });
    } catch (err) {
      set({ approvalMode: previous, banner: toBanner(err) });
    }
  },

  async setVisionMode(on) {
    const { sessionId } = get();
    if (!sessionId) return;
    const previous = get().visionMode;
    set({ visionMode: on });
    try {
      await api.patchSession(sessionId, { vision_mode: on });
    } catch (err) {
      // 回滚并把后端的提示显示出来。
      //
      // 这条路径很重要：未核验的模型开视觉会返回 400，附带
      // "去设置页核验"的 hint。不显示的话开关会静默弹回去，
      // 用户完全不知道为什么。
      set({ visionMode: previous, banner: toBanner(err) });
    }
  },

  /**
   * 从某条用户消息处截断并重发。
   *
   * ## 顺序：先取内容，再删，最后发
   *
   * 内容必须在删之前从本地 state 取出来 —— 删完之后那条消息在库里和
   * state 里都没了，拿不到原文。
   *
   * ## 为什么不复用后端已有的消息
   *
   * 截断接口只删不返回内容。让它返回被删消息的内容会把两件事耦合在
   * 一起（删除 + 读取），而前端本来就有这条消息的完整副本。
   */
  async retryFrom(messageId) {
    const sessionId = get().sessionId;
    if (!sessionId || get().pending || get().streaming) return;

    const target = get().messages.find((m) => m.id === messageId);
    if (!target || target.role !== "user") return;

    // 图片一起带回去。不带的话"重发"会丢掉原来发的图，
    // 而用户以为是同一条消息再发一次。
    const images = (target.attachments ?? []).filter((a) =>
      a.startsWith("data:image/"),
    );
    const content = target.content;

    try {
      await api.truncateFrom(sessionId, messageId);
    } catch (err) {
      // 截断失败就不发 —— 否则会在旧历史后面追加一条重复消息。
      //
      // 最常见的失败是 409 run_in_progress（流还没结束）。
      // 后端的 hint 会说"先停止当前生成"。
      set({ banner: toBanner(err) });
      return;
    }

    // 本地也截掉，不等重新拉取 —— 等的话界面会有一瞬间显示已删除的消息
    const idx = get().messages.findIndex((m) => m.id === messageId);
    if (idx >= 0) set((s) => ({ messages: s.messages.slice(0, idx) }));

    await get().send(content, images.length > 0 ? images : undefined);
  },

  dismissBanner: () => set({ banner: null }),

  toggleReasoning: () =>
    set((s) =>
      s.streaming
        ? { streaming: { ...s.streaming, reasoningOpen: !s.streaming.reasoningOpen } }
        : {},
    ),
}));

// ─────────────────────────── 事件处理 ───────────────────────────

type SetFn = (
  partial: Partial<ChatState> | ((s: ChatState) => Partial<ChatState>),
) => void;
type GetFn = () => ChatState;

function handleEvent<K extends SseEventName>(
  set: SetFn,
  get: GetFn,
  name: K,
  data: SseEventMap[K],
): void {
  switch (name) {
    case "meta": {
      const d = data as SseEventMap["meta"];
      set((s) => ({
        runId: d.run_id,
        pending: false,
        streaming: emptyStreaming(d.run_id),
        // 用后端返回的真实 id 替换乐观插入的临时消息
        messages: s.messages.map((m) =>
          m.id.startsWith("tmp_") ? { ...m, id: d.user_message_id } : m,
        ),
      }));
      break;
    }

    case "thinking": {
      const d = data as SseEventMap["thinking"];
      set((s) =>
        s.streaming
          ? { streaming: { ...s.streaming, reasoning: s.streaming.reasoning + d.delta } }
          : {},
      );
      break;
    }

    case "message": {
      const d = data as SseEventMap["message"];
      set((s) =>
        s.streaming
          ? { streaming: { ...s.streaming, content: s.streaming.content + d.delta } }
          : {},
      );
      break;
    }

    case "agent_start": {
      // 记下当前活跃的子智能体。
      //
      // 不记的话子智能体内部的工具调用会混进父代理的扁平列表里 ——
      // 用户看到 researcher 读的 12 个文件，以为是主代理自己读的，
      // "委派省了上下文"这件事在界面上完全看不出来。
      const d = data as SseEventMap["agent_start"];
      if (d.depth > 0) {
        set((s) => ({
          activeAgents: [
            ...s.activeAgents,
            {
              span_id: d.span_id ?? "",
              agent_name: d.agent_name,
              task: d.task ?? "",
              depth: d.depth,
              status: "running" as const,
              tools: [],
            },
          ],
        }));
      }
      break;
    }

    case "agent_end": {
      const d = data as SseEventMap["agent_end"];
      set((s) => ({
        activeAgents: s.activeAgents.map((a) =>
          a.span_id === d.span_id
            ? {
                ...a,
                // stop_reason 是 "final" 才算成功。max_turns / error / cancelled
                // 都要显示成异常 —— 子智能体撞轮次上限时给出的结论通常是
                // 半成品，用户需要知道这一点。
                status:
                  d.stop_reason === "final"
                    ? ("ok" as const)
                    : ("error" as const),
                turns: d.turns,
                // token 归集到子智能体自己头上。
                //
                // 少见实现做了 token 聚合，另两个的子代理开销
                // 是黑洞 —— "这次委派花了多少钱"无法回答。
                tokens: d.prompt_tokens + d.completion_tokens,
              }
            : a,
        ),
      }));
      break;
    }

    case "tool_start": {
      const d = data as SseEventMap["tool_start"];
      set((s) => {
        const entry = {
          call_id: d.call_id,
          tool_name: d.tool_name,
          args: d.args,
          status: "running" as const,
        };
        // 归属靠事件自带的 agent_name，不猜。
        //
        // 早先这里用"最后一个 running 的子智能体"来猜，同时后端的
        // tool_end 事件里 depth 是 0（emit 读的是当前 span，工具执行时
        // agent span 已不在栈顶）—— 两个问题叠加，子智能体读的 6 个文件
        // 全被算成父代理自己读的。
        if (d.agent_name) {
          const target = [...s.activeAgents]
            .reverse()
            .find((a) => a.agent_name === d.agent_name && a.status === "running");
          if (target) {
            return {
              activeAgents: s.activeAgents.map((a) =>
                a.span_id === target.span_id
                  ? { ...a, tools: [...a.tools, entry] }
                  : a,
              ),
            };
          }
        }
        return s.streaming
          ? { streaming: { ...s.streaming, tools: [...s.streaming.tools, entry] } }
          : {};
      });
      break;
    }

    case "tool_end": {
      const d = data as SseEventMap["tool_end"];
      const patch = {
        status: d.is_error ? ("error" as const) : ("ok" as const),
        duration_ms: d.duration_ms,
        display: d.display,
        content_preview: d.content_preview,
      };
      set((s) => {
        // 按 call_id 找，不猜归属 —— tool_start 时已经决定了它属于谁。
        // 猜的话并发委派时会更新到错的地方。
        const inAgent = s.activeAgents.some((a) =>
          a.tools.some((t) => t.call_id === d.call_id),
        );
        if (inAgent) {
          return {
            activeAgents: s.activeAgents.map((a) => ({
              ...a,
              tools: a.tools.map((t) =>
                t.call_id === d.call_id ? { ...t, ...patch } : t,
              ),
            })),
          };
        }
        return s.streaming
          ? {
              streaming: {
                ...s.streaming,
                tools: s.streaming.tools.map((t) =>
                  t.call_id === d.call_id ? { ...t, ...patch } : t,
                ),
              },
            }
          : {};
      });
      break;
    }

    case "memory_recalled": {
      // 记忆被召回时要让用户看到用了哪些 ——
      // 记忆是自动注入的，不显示的话用户不知道 AI 的回答受了什么影响，
      // 更不知道某条错误记忆正在生效。
      const d = data as SseEventMap["memory_recalled"];
      set({ recalledMemories: d.items });
      break;
    }

    case "context_usage":
      set({ usage: data as SseEventMap["context_usage"] });
      break;

    case "approval_required": {
      const d = data as SseEventMap["approval_required"];
      set({
        approval: {
          call_id: d.call_id,
          tool_name: d.tool_name,
          args: d.args ?? {},
          timeout_at: d.timeout_at ?? 0,
        },
      });
      break;
    }

    case "approval_resolved": {
      // 后端已经有结论了（超时、取消、或在别处批准），收掉弹窗。
      // 只处理 approval_required 的话，这几种情况下弹窗会永远留在界面上。
      set({ approval: null });
      break;
    }

    case "compacting": {
      const d = data as SseEventMap["compacting"];
      // 压缩要花一次 LLM 调用（几秒）。不显示的话用户看到界面卡住
      // 而没有任何解释，会以为是卡死了。
      set({ compacting: { victim_count: d.victim_count } });
      break;
    }

    case "compacted": {
      const d = data as SseEventMap["compacted"];
      const pct =
        d.before_tokens > 0
          ? Math.round((1 - d.after_tokens / d.before_tokens) * 100)
          : 0;
      set((s) => ({
        compacting: null,
        // 压缩是"悄悄改变了模型记忆"的操作，必须让用户知道 ——
        // 否则模型后面忘了早前的细节，用户会以为模型变笨了。
        messages: [
          ...s.messages,
          {
            ...blankMessage(`compact_${d.ts}`),
            role: "summary" as const,
            content:
              `已压缩 ${d.victim_count} 条较早的消息（${d.before_tokens.toLocaleString()} → ` +
              `${d.after_tokens.toLocaleString()} tokens，省 ${pct}%）。` +
              `关键信息已保留在摘要中。`,
          },
        ],
      }));
      break;
    }

    case "artifact_updated": {
      const d = data as SseEventMap["artifact_updated"];
      set({ artifact: { kind: d.kind, path: d.path, chars: d.chars } });
      break;
    }

    case "todo_updated": {
      const d = data as SseEventMap["todo_updated"];
      set({ todos: d.items, todoStats: d.stats });
      break;
    }

    case "title": {
      const d = data as SseEventMap["title"];
      set({ title: d.title });
      break;
    }

    case "model_fallback": {
      const d = data as SseEventMap["model_fallback"];
      // 降级必须可见。静默降级会让用户以为在用配好的模型，
      // 实际用的是另一个（可能贵 10 倍或弱很多）。
      set({
        banner: {
          kind: "warn",
          message: `${d.purpose} 功能位未绑定模型，已回落到 ${d.used}`,
          hint: d.reason,
        },
      });
      break;
    }

    case "error": {
      const d = data as SseEventMap["error"];
      set({
        banner: {
          kind: "error",
          code: d.code,
          message: d.message,
          hint: d.hint,
          retryable: d.retryable,
        },
      });
      break;
    }

    case "cancelled":
      set({
        banner: {
          kind: "info",
          message: "已停止生成",
          hint: "已生成的内容都已保存",
        },
      });
      break;

    case "done": {
      // 落成正式消息，然后重新拉一次真实数据 ——
      // 库里的消息有正确的 seq、id、token 统计，本地拼的没有。
      const sessionId = get().sessionId;
      set({ runId: null, ...flushStreaming(get()) });
      if (sessionId) {
        void api
          .listMessages(sessionId)
          .then(({ items }) => set({ messages: items, streaming: null }))
          .catch(() => {
            /* 拉取失败就保留本地拼的，不弹错误 */
          });
      }
      break;
    }

    case "ping":
      // 心跳，只为保持连接。不做任何事。
      break;

    default:
      break;
  }
}

/**
 * 把 streaming 转成正式消息插进列表。
 *
 * 即使流是异常结束的也要转 —— 已经显示给用户的内容不该凭空消失。
 */
function flushStreaming(s: ChatState): Partial<ChatState> {
  const st = s.streaming;
  if (!st) return {};
  if (!st.content && !st.reasoning && st.tools.length === 0) {
    return { streaming: null };
  }
  const msg: MessageOut = {
    ...blankMessage(`local_${st.run_id}`),
    content: st.content,
    reasoning: st.reasoning || null,
    run_id: st.run_id,
  };
  return { messages: [...s.messages, msg], streaming: null };
}

function toBanner(err: unknown): BannerState {
  if (err instanceof ApiError) {
    return {
      kind: "error",
      code: err.code,
      message: err.message,
      hint: err.hint,
      retryable: err.status >= 500,
    };
  }
  return {
    kind: "error",
    message: err instanceof Error ? err.message : String(err),
  };
}
