/**
 * SSE 客户端。
 *
 * ## 为什么不能用 EventSource
 *
 * `/api/chat` 是 POST（请求体里有消息内容、引用列表、附件 ID），
 * 而 EventSource 只能发 GET。必须用 fetch + ReadableStream.getReader()。
 *
 * ## 三个必须自己处理的问题
 *
 * 1. 事件可能跨 chunk 断开。网络 chunk 与 SSE 事件边界无关，
 *    一个事件的 `data:` 行可能被切成两半。必须用缓冲区累积到
 *    遇见空行（`\n\n`）才解析。
 * 2. UTF-8 多字节字符可能跨 chunk 断开。中文一个字 3 字节，
 *    切在中间会解码出乱码。用 `TextDecoder` 的 `{ stream: true }`。
 * 3. 取消要区分「用户主动取消」和「网络断开」。前者不该报错。
 */

import type { SseEventMap, SseEventName } from "./types";

export interface SseHandlers {
  /** 收到任意事件。返回值忽略 */
  onEvent: <K extends SseEventName>(name: K, data: SseEventMap[K]) => void;
  /** 流正常结束或出错结束后调用一次，无论哪种情况 */
  onClose?: (reason: "done" | "aborted" | "network") => void;
  /** 网络层错误（不是后端发的 error 事件） */
  onNetworkError?: (err: Error) => void;
}

export interface StartChatOptions {
  session_id: string;
  content: string;
  refs?: Record<string, unknown>[];
  /** 图片的 data URL。只在这一轮进 LLM 请求，不进历史 */
  images?: string[];
  attachment_ids?: string[];
}

/** 后端返回的结构化错误 */
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public hint: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function readErrorDetail(resp: Response): Promise<ApiError> {
  let code = "http_error";
  let message = `HTTP ${resp.status}`;
  let hint: string | null = null;
  try {
    const body = await resp.json();
    const d = body?.detail;
    if (d && typeof d === "object") {
      code = d.code ?? code;
      message = d.message ?? message;
      hint = d.hint ?? null;
    }
  } catch {
    // 响应体不是 JSON，保留默认值
  }
  return new ApiError(resp.status, code, message, hint);
}

/**
 * 发起一次对话并消费事件流。
 *
 * 返回一个 `cancel()`。它只中断【本地读取】，不会取消服务端的生成 ——
 * 取消生成要另外调 `POST /api/runs/{run_id}/cancel`。
 *
 * 这个区分是有意的：连接断开不应该终止生成。生成继续跑完并正常落库，
 * 用户刷新页面能看到完整结果。
 */
export function startChat(
  opts: StartChatOptions,
  handlers: SseHandlers,
): () => void {
  const ctrl = new AbortController();
  let aborted = false;

  void (async () => {
    let closeReason: "done" | "aborted" | "network" = "network";
    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: opts.session_id,
          content: opts.content,
          refs: opts.refs ?? [],
          attachment_ids: opts.attachment_ids ?? [],
        }),
        signal: ctrl.signal,
      });

      if (!resp.ok) {
        // 校验错误在流开始前返回（后端的 prepare 阶段），
        // 所以这里能拿到正常的 HTTP 错误码和结构化 detail
        throw await readErrorDetail(resp);
      }
      if (!resp.body) {
        throw new Error("响应没有 body，无法读取流");
      }

      const reader = resp.body.getReader();
      // stream: true 让跨 chunk 的多字节字符正确拼接。
      // 不加的话中文会在 chunk 边界处变成乱码。
      const decoder = new TextDecoder("utf-8");
      let buffer = "";

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // 按空行切分事件。最后一段可能不完整，留在 buffer 里等下一个 chunk。
        let sepIndex: number;
        while ((sepIndex = buffer.indexOf("\n\n")) !== -1) {
          const block = buffer.slice(0, sepIndex);
          buffer = buffer.slice(sepIndex + 2);
          const parsed = parseBlock(block);
          if (parsed) {
            handlers.onEvent(
              parsed.name as SseEventName,
              parsed.data as never,
            );
            if (parsed.name === "done") closeReason = "done";
          }
        }
      }
      // 冲刷解码器里可能残留的字节
      buffer += decoder.decode();
      const tail = parseBlock(buffer);
      if (tail) {
        handlers.onEvent(tail.name as SseEventName, tail.data as never);
        if (tail.name === "done") closeReason = "done";
      }
    } catch (err) {
      if (aborted || (err instanceof DOMException && err.name === "AbortError")) {
        closeReason = "aborted";
      } else {
        closeReason = "network";
        handlers.onNetworkError?.(
          err instanceof Error ? err : new Error(String(err)),
        );
      }
    } finally {
      handlers.onClose?.(closeReason);
    }
  })();

  return () => {
    aborted = true;
    ctrl.abort();
  };
}

interface ParsedBlock {
  name: string;
  data: unknown;
}

function parseBlock(block: string): ParsedBlock | null {
  const trimmed = block.trim();
  if (!trimmed) return null;

  let name = "";
  const dataLines: string[] = [];
  for (const rawLine of trimmed.split("\n")) {
    const line = rawLine.trimEnd();
    if (line.startsWith(":")) continue; // SSE 注释
    if (line.startsWith("event:")) {
      name = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trim());
    }
  }
  if (!name || dataLines.length === 0) return null;

  try {
    return { name, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    // 单条事件解析失败不应中断整个流 —— 后面的事件可能是好的
    console.warn("[sse] 事件解析失败", name, dataLines.join("\n").slice(0, 200));
    return null;
  }
}
