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
  /** 这次对话用哪个模型。空串 = 跟随功能位绑定 */
  modelPk: string;
  /** 这次对话用哪个智能体。空串 = 未选择（直接用模型对话） */
  agentId: string;
  /**
   * 实际生效的模型窗口。
   *
   * 单独存一份是因为上下文条要在【发消息之前】就显示窗口大小 ——
   * 那时还没有任何 context_usage 事件。
   */
  contextWindow: number;
  approvalMode: "manual" | "auto";
  /** 视觉模式。开启后可发图片，但模型必须先通过核验 */
  visionMode: boolean;
  /** 会话级流式开关：控制 LLM 调用的 stream 参数 */
  streamEnabled: boolean;
  /**
   * 私密模式：这轮对话不写记忆。
   *
   * 查不到会话时【默认按禁止写处理】—— 那是实测抓到的 bug 留下的：
   * 最初只在召回侧做了拦截，模型看不到会话开关照样写，真写进去了。
   */
  privateMode: boolean;
  /**
   * 失忆模式：这轮对话不召回记忆。
   *
   * 和私密模式是两件事：私密是"别记住我说的"，失忆是"别拿以前的事来烦我"。
   * 拆成两个开关是因为它们的用途不同 —— 调试提示词时要失忆但不介意被记住，
   * 聊敏感话题时要私密但仍然需要之前的上下文。
   */
  amnesiaMode: boolean;
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
  /** 设置这次对话用哪个模型。传空串回到默认绑定 */
  setWorkModel: (pk: string) => Promise<void>;
  /** 设置这次对话用哪个智能体。传空串清除选择 */
  setAgentId: (id: string) => Promise<void>;
  setVisionMode: (on: boolean) => Promise<void>;
  setStreamEnabled: (on: boolean) => Promise<void>;
  /** 轮询后台正在跑的 run，直到它结束 */
  watchBackgroundRun: (sessionId: string) => Promise<void>;
  setPrivateMode: (on: boolean) => Promise<void>;
  setAmnesiaMode: (on: boolean) => Promise<void>;
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

/**
 * 从历史消息里恢复上下文占用。
 *
 * ## 为什么不能只靠 context_usage 事件
 *
 * 那个事件只在 run 期间发。切到一个有历史的会话时没有任何事件，
 * 计数就是空的 —— 而那个会话已经用掉几千 token，显示空是错的。
 *
 * ## 为什么取最后一条助手消息的 prompt_tokens
 *
 * prompt_tokens 是那一轮【发出去】的提示词大小，等于当轮的上下文占用。
 * 用 completion_tokens 或两者之和都不对：前者只是回复长度，
 * 后者会把回复重复算一次（下一轮它会被算进 prompt 里）。
 *
 * 找不到就返回 null —— 空会话、或者上游从不返回 usage 的情况。
 * 那时进度条不显示，比显示一个错的数字好。
 */
function restoreUsage(
  items: MessageOut[],
  contextWindow: number,
  watermark: number,
): ContextUsageEvent | null {
  // 只取【归档水位线之后】的消息。归档前的消息已被记忆归档，它们的
  // prompt_tokens 是"发出时的完整上下文"（大值），归档后已过时 ——
  // 用它恢复会让 token 条显示"没有压缩的程度"。
  for (let i = items.length - 1; i >= 0; i--) {
    const m = items[i];
    if (m.role === "assistant" && m.prompt_tokens && m.seq > watermark) {
      // 窗口大小按会话选的模型算。拿不到就用一个保守的默认值 ——
      // 宁可比率偏大（提前提示压缩），也不要偏小让用户以为还很空。
      const win = contextWindow || 32768;
      return {
        // EventCommon 的字段：这不是真事件，用零值填。
        // 事件流里的 span 信息对"恢复历史占用"没有意义。
        ts: m.created_at,
        span_id: null,
        parent_span_id: null,
        depth: 0,
        used_tokens: m.prompt_tokens,
        window_tokens: win,
        ratio: Math.round((m.prompt_tokens / Math.max(1, win)) * 10000) / 10000,
        compact_at: Math.round(win * 0.8),
        // 【不是估算】。
        //
        // message.prompt_tokens 存的是模型返回的真实 usage
        // —— 流式请求已经开了 stream_options.include_usage。
        //
        // 上一轮我在这里写了 true，界面于是在一个真实值旁边显示
        // "（估算）"。用户看到"4551（估算）"会以为这个数字不可信，
        // 而它恰恰是最可信的那个 —— 是模型自己报的。
        //
        // 只有上游不返回 usage、走本地 tiktoken 兜底时才是估算，
        // 那种情况由 loop.py 发的事件自己标。
        is_estimate: false,
      };
    }
  }
  return null;
}

export const useChatStore = create<ChatState>((set, get) => ({
  sessionId: null,
  messages: [],
  streaming: null,
  pending: false,
  runId: null,
  banner: null,
  usage: null,
  activeAgents: [],
  compacting: null,
  approval: null,
  workDir: "",
  modelPk: "",
  agentId: "",
  contextWindow: 0,
  approvalMode: "manual",
  visionMode: false,
  streamEnabled: true,
  privateMode: false,
  amnesiaMode: false,
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
      compacting: null,
      approval: null,
      cancelStream: null,
      // 【必须清掉】。usage 是上一个会话的上下文占用，
      // 不清的话切会话后计数不变 —— 用户看到的是上一个会话的数字，
      // 而它下一轮才会被新的 context_usage 事件覆盖。
      //
      // 会话是独立的上下文，token 计数必须跟着会话走。
      usage: null,
    });

    try {
      const [{ items, watermark }, session, todos] = await Promise.all([
        api.listMessages(sessionId),
        api.getSession(sessionId),
        api.listTodos(sessionId),
      ]);
      // 【必须丢弃过期响应】。
      //
      // 快速连点侧栏（A→B→C）时三个 openSession 并发飞行，Promise.all
      // 的完成顺序不保证与发起顺序一致。早发起的响应后到就会把它的
      // messages/title/workDir 写进 store，而路由指向最后点的那个 ——
      // 界面显示 A 的内容但 URL 是 C。
      if (get().sessionId !== sessionId) return;
      set({
        messages: items,
        title: session.title,
        todos: todos.items,
        todoStats: todos.stats,
        // 审批模式是会话级设置，落在库里。不读回来的话切换会话或刷新页面后
        // 开关会显示成 manual 而后端其实是 auto —— 用户会以为开关坏了。
        // 从历史消息恢复上下文占用。
        //
        // context_usage 只在 run 期间发。不恢复的话切到一个有历史的
        // 会话时计数是空的，要等下一轮才出现 —— 而那个会话明明已经
        // 用掉了几千 token，显示为空是错的。
        //
        // 取最后一条带 prompt_tokens 的助手消息：那就是上一轮真实
        // 发出去的提示词大小，也就是当前的上下文占用。
        usage: restoreUsage(items, session.context_window ?? 0, watermark ?? -1),
        approvalMode: session.approval_mode ?? "manual",
        // 空串兜底：老会话在迁移前没有这个字段
        workDir: session.work_dir ?? "",
        modelPk: session.model_pk ?? "",
        agentId: session.agent_id ?? "",
        contextWindow: session.context_window ?? 0,
        visionMode: session.vision_mode ?? false,
        streamEnabled: session.stream_enabled ?? true,
        // 【必须读回来】。不读的话切会话后开关显示的是上一个会话的状态 ——
        // 用户以为自己开着私密模式，而实际这个会话是关的。
        privateMode: session.private_mode ?? false,
        amnesiaMode: session.amnesia_mode ?? false,
      });

      // 这个会话可能有后台正在跑的 run（用户之前切走了）。
      //
      // 不检测的话现象是：切回来看到的是切走那一刻的历史，之后
      // 再没有任何新内容 —— 而后台其实一直在写库。用户以为卡死了。
      void get().watchBackgroundRun(sessionId);
    } catch (err) {
      // 过期的失败也不该弹 banner —— 用户已经在别的会话了，
      // 弹一个"加载失败"只会让他困惑。
      if (get().sessionId !== sessionId) return;
      set({ banner: toBanner(err) });
    }
  },

  /**
   * 轮询后台正在跑的 run，直到它结束。
   *
   * ## 为什么是轮询而不是重连 SSE
   *
   * 一个 run 的 EventBus 只有一个队列、一个消费者。原来的消费者
   * （切走时那个 HTTP 连接）已经 detach 了，队列被清空 —— 那些事件
   * 已经永久丢失，没有可重连的流。
   *
   * 要真正支持重连，得让 bus 支持多消费者 + 事件重放缓冲，
   * 那是个大得多的改动。而 run 一直在往库里写消息，轮询 listMessages
   * 就能拿到增量输出 —— 用户看到的是"每两秒多出一段"而不是
   * 逐字流式，但至少内容在动，而且能知道什么时候结束。
   *
   * ## 为什么必须轮到结束
   *
   * 结束的那一刻要解锁输入框。不轮的话用户只能靠手动刷新去猜
   * "它跑完了吗"—— 而发消息会撞 409。
   */
  async watchBackgroundRun(sessionId) {
    let active: { run_id: string } | null = null;
    try {
      active = await api.activeRun(sessionId);
    } catch {
      // 查不到就当没有。这是个增强功能，失败不该影响打开会话。
      return;
    }
    if (!active || get().sessionId !== sessionId) return;

    set({
      runId: active.run_id,
      // 【必须置 pending】。后台在跑，输入框要锁上 ——
      // 不锁的话用户能输入，一发就 409，而那个错误信息
      // 说的是"连接中断"，完全不指向真因。
      pending: true,
      banner: {
        kind: "info",
        message: "这个对话正在后台继续",
        hint: "你之前切走了。内容会每隔几秒刷新一次，也可以点停止按钮结束它",
        retryable: false,
      },
    });

    // 轮询到结束。间隔 2 秒：再密就是在刷后端，再疏用户会觉得卡住了。
    while (get().sessionId === sessionId) {
      await new Promise((r) => setTimeout(r, 2000));
      if (get().sessionId !== sessionId) return;

      let still: { run_id: string } | null = null;
      try {
        still = await api.activeRun(sessionId);
      } catch {
        break;
      }
      if (get().sessionId !== sessionId) return;

      try {
        const { items } = await api.listMessages(sessionId);
        if (get().sessionId !== sessionId) return;
        set({ messages: items });
      } catch {
        /* 拉取失败下一轮再试 */
      }

      if (!still) {
        // 跑完了：解锁输入框并清掉那条提示
        set({ pending: false, runId: null, banner: null });
        return;
      }
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
    }));

    // 【所有回调都必须先确认自己还属于当前会话】。
    //
    // 这些回调是闭包，捕获的 sessionId 是发消息那一刻的值。用户切到别的
    // 会话后它们仍会触发（流还在读、或者刚断开），此时无条件 set() 会把
    // 上一个会话的状态写进当前界面：
    //
    //   - onEvent：A 的输出流进 B 的气泡列表
    //   - onClose：清掉 B 正在进行的 pending/streaming
    //   - onClose(network) + openSession(A)：把 A 的历史灌进 store，
    //     而路由还指向 B —— 之后在"B 的页面"上发消息实际发往 A
    const isStillCurrent = () => get().sessionId === sessionId;

    const cancel = startChat(
      { session_id: sessionId, content, images: images ?? [], refs: refs ?? [], agent_id: get().agentId },
      {
        onEvent: (name, data) => {
          if (!isStillCurrent()) return;
          handleEvent(set, get, name, data);
        },
        onNetworkError: (err) => {
          if (!isStillCurrent()) return;
          set({
            banner: {
              kind: "error",
              message: `连接中断：${err.message}`,
              hint: "生成可能仍在后台继续。刷新页面可看到已保存的内容",
              retryable: true,
            },
          });
        },
        onApiError: (err) => {
          if (!isStillCurrent()) return;
          // 把乐观插入的那条用户消息撤掉。
          //
          // 409 发生在后端落库【之前】（prepare 里先查 active run），
          // 所以这条消息在库里不存在。留着它的话用户以为发出去了，
          // 而刷新之后它会凭空消失。
          set((s) => ({
            messages: s.messages.filter((m) => m.id !== tempId),
            banner: toBanner(err),
          }));
          // 撞 409 说明有 run 在后台跑 —— 开始轮询，跑完自动解锁。
          if (err.code === "run_in_progress") {
            void get().watchBackgroundRun(sessionId);
          }
        },
        onClose: (reason) => {
          // 切走之后不要动当前会话的状态。
          //
          // 尤其是 pending/streaming：清掉的话用户在【新】会话里
          // 正在进行的生成会显示成已结束，输入框提前解锁。
          if (!isStillCurrent()) return;
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

  async setWorkModel(pk) {
    const { sessionId } = get();
    if (!sessionId) return;
    // 用后端返回值而不是本地乐观更新：后端会拒绝禁用的模型，
    // 乐观更新的话按钮先变成新模型、请求失败后又变回去，
    // 那种闪回比一开始就不变更让人困惑。
    const s = await api.patchSession(sessionId, { model_pk: pk });
    // 窗口也要跟着变 —— 换成 65K 的模型后，条还按 131K 画的话
    // 占用比例会显示成一半，用户以为还很空。
    set({ modelPk: s.model_pk ?? "", contextWindow: s.context_window ?? 0 });
  },

  async setAgentId(id) {
    const { sessionId } = get();
    if (!sessionId) return;
    // PATCH session 存 agent_id
    const s = await api.patchSession(sessionId, { agent_id: id });
    const update: Partial<ChatState> = { agentId: s.agent_id ?? "" };

    // 如果智能体绑定了模型，自动切换到该模型
    if (id) {
      try {
        const agent = await api.agents.get(id);
        if (agent?.model_id) {
          const m = await api.patchSession(sessionId, { model_pk: agent.model_id });
          update.modelPk = m.model_pk ?? "";
          update.contextWindow = m.context_window ?? 0;
        }
      } catch {
        // 获取智能体信息失败不阻塞切换
      }
    }

    set(update as Pick<ChatState, keyof ChatState>);
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

  async setPrivateMode(on) {
    const { sessionId } = get();
    if (!sessionId) return;
    const previous = get().privateMode;
    set({ privateMode: on });
    try {
      await api.patchSession(sessionId, { private_mode: on });
    } catch (err) {
      // 回滚并显示原因。静默弹回去的话用户会以为开关坏了，
      // 而这个开关关系到"我说的话会不会被记住"——
      // 失败必须让他知道。
      set({ privateMode: previous, banner: toBanner(err) });
    }
  },

  async setAmnesiaMode(on) {
    const { sessionId } = get();
    if (!sessionId) return;
    const previous = get().amnesiaMode;
    set({ amnesiaMode: on });
    try {
      await api.patchSession(sessionId, { amnesia_mode: on });
    } catch (err) {
      set({ amnesiaMode: previous, banner: toBanner(err) });
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

  async setStreamEnabled(on) {
    const { sessionId } = get();
    if (!sessionId) return;
    const previous = get().streamEnabled;
    set({ streamEnabled: on });
    try {
      await api.patchSession(sessionId, { stream_enabled: on });
    } catch (err) {
      set({ streamEnabled: previous, banner: toBanner(err) });
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

        case "refs_expanded": {
          // 引用失败必须提示。
          //
          // 用户打了 @某个文件 却没生效时，不提示的话他只会觉得
          // "AI 没看我给的文件"—— 而真相是那个引用展开失败了
          // （文件不存在、超过 64KB 单文件上限、或者被路径白名单拒绝）。
          //
          // 成功时不提示：那是预期行为，弹一条"引用成功"只是噪音。
          const d = data as SseEventMap["refs_expanded"];
          if (d.failures && d.failures.length > 0) {
            set({
              banner: {
                kind: "warn",
                message: `${d.failures.length} 个引用没能展开`,
                hint: d.failures.slice(0, 3).join("；"),
                retryable: false,
              },
            });
          }
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
            .then(({ items }) => {
              // 【必须再确认一次会话没变】。
              //
              // 这次 HTTP 往返有几十到几百毫秒，用户完全可能在这期间
              // 切走。不校验的话旧会话的消息列表会覆盖新会话的 —— 表现
              // 是"切过去显示了别的会话的对话"。
              if (get().sessionId !== sessionId) return;
              set({ messages: items, streaming: null });
            })
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
