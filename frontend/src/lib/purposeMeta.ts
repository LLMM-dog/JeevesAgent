/**
 * 功能位（purpose）的展示元数据。
 *
 * 后端 service.py 的 PURPOSES = chat/vision/title/compact/embedding/memory。
 * 前端硬编码这份列表的话后端加功能位就不同步了，但功能位的【顺序】和
 * 【文案】目前只在这里维护 —— 顺序决定界面排列，文案决定用户能否看懂。
 */

import {
  MessageSquare,
  Eye,
  Heading,
  Minimize2,
  Layers,
  MemoryStick,
  ArrowUpDown,
  type LucideIcon,
} from "lucide-react";
import type { ModelItem, Purpose } from "./types";

export interface PurposeMeta {
  label: string;
  /** 一句说明：这个功能位是干什么的，用户据此选便宜还是贵的模型 */
  hint: string;
  icon: LucideIcon;
  /**
   * 是否【不回落到对话模型】。embedding / memory_rerank 的 API 协议和
   * chat 不同（/embeddings、/rerank），未绑定时不是回落而是功能不可用。
   */
  noChatFallback?: boolean;
}

export const PURPOSE_META: Record<Purpose, PurposeMeta> = {
  chat: {
    label: "对话",
    hint: "主对话模型，所有对话都用它。",
    icon: MessageSquare,
  },
  vision: {
    label: "看图",
    hint: "视觉模式/图片输入用，需要先核验模型的看图能力。",
    icon: Eye,
  },
  title: {
    label: "标题",
    hint: "生成会话标题。后台动作，建议配便宜模型。",
    icon: Heading,
  },
  compact: {
    label: "压缩",
    hint: "上下文压缩。高频后台动作，建议配便宜模型。",
    icon: Minimize2,
  },
  embedding: {
    label: "嵌入",
    hint: "记忆向量化。未绑定则记忆召回不可用。",
    icon: Layers,
    noChatFallback: true,
  },
  memory: {
    label: "记忆",
    hint: "记忆提取。未绑定会回落到「压缩」用的模型。",
    icon: MemoryStick,
  },
  memory_rerank: {
    label: "重排序",
    hint: "记忆召回时对候选记忆重新打分排序。需专门的 rerank 模型，不回落到对话模型。",
    icon: ArrowUpDown,
    noChatFallback: true,
  },
};

/** 界面显示顺序。 */
export const PURPOSE_ORDER: Purpose[] = [
  "chat",
  "vision",
  "title",
  "compact",
  "embedding",
  "memory",
  "memory_rerank",
];

/**
 * 按功能位过滤可选模型。
 *
 * 过滤的边界要【宽松】—— 宁可多给几个让用户自己选，也不要误藏掉一个
 * 能用的模型：
 * - 类型没探测到（unknown）的模型总是出现，让用户自己判断；
 * - 推理（reasoning）就是对话，所以对话类功能位同时接受 chat 和 reasoning；
 * - 视觉位只看 supports_vision：验证通过(true)排前面，未验证(unknown)排后面，
 *   验证不通过(false)不显示 —— 绑定时会提示核验。
 */
export function filterModelsForPurpose(
  models: ModelItem[],
  purpose: Purpose,
): ModelItem[] {
  // 看图位单独处理：只认 supports_vision 三态 ——
  // 验证通过(true)优先、未验证(unknown)其次、验证不通过(false)不显示。
  // 不受下面"类型 unknown 总是出现"的宽松规则影响，因为视觉位要的是
  // "能不能看图"，不是"模型类型是否已知"。
  if (purpose === "vision") {
    const rank = (v: ModelItem["supports_vision"]) => (v === "true" ? 0 : 1);
    return models
      .filter(
        (m) => m.supports_vision === "true" || m.supports_vision === "unknown",
      )
      .sort((a, b) => rank(a.supports_vision) - rank(b.supports_vision));
  }

  return models.filter((m) => {
    if ((m.model_type as string) === "unknown") return true;

    switch (purpose) {
      case "chat":
      case "title":
      case "compact":
      case "memory":
        return m.model_type === "chat" || m.model_type === "reasoning";
      case "embedding":
        return m.model_type === "embedding";
      case "memory_rerank":
        return m.model_type === "rerank";
      default:
        return true;
    }
  });
}
