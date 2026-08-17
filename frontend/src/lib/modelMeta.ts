/**
 * 模型类型 → 图标/标签的映射。
 *
 * 类型由后端按名字启发式判定（见 backend/app/modules/endpoint/windows.py 的
 * detect_model_type），前端只负责把它画出来。放在独立模块里让卡片和
 * 探测列表共用一份，避免两处各写一套图标对不上。
 */

import {
  ArrowUpDown,
  Brain,
  Image as ImageIcon,
  Layers,
  MessageSquare,
  Mic,
  Volume2,
  type LucideIcon,
} from "lucide-react";
import type { ModelType } from "./types";

export const MODEL_TYPE_META: Record<ModelType, { label: string; icon: LucideIcon }> = {
  chat: { label: "对话", icon: MessageSquare },
  reasoning: { label: "推理", icon: Brain },
  embedding: { label: "嵌入", icon: Layers },
  rerank: { label: "重排", icon: ArrowUpDown },
  tts: { label: "语音合成", icon: Volume2 },
  audio: { label: "语音识别", icon: Mic },
  image: { label: "生图", icon: ImageIcon },
};

/** 兜底：后端以后加了新类型前端没跟上时，至少能显示个占位图标。 */
export function modelTypeMeta(t: string): { label: string; icon: LucideIcon } {
  return MODEL_TYPE_META[t as ModelType] ?? { label: t, icon: MessageSquare };
}
