import { describe, expect, it } from "vitest";
import { filterModelsForPurpose } from "./purposeMeta";
import type { ModelItem } from "./types";

function mk(overrides: Partial<ModelItem>): ModelItem {
  return {
    id: "m",
    endpoint_id: "e",
    endpoint_name: "端点",
    group_id: "",
    model_id: "m",
    display_name: "模型",
    context_window: 0,
    window_source: "",
    supports_vision: "unknown",
    supports_tools: "unknown",
    model_type: "chat",
    enabled: true,
    price_in_per_1m: null,
    price_out_per_1m: null,
    bindings: [],
    ...overrides,
  };
}

describe("filterModelsForPurpose: vision", () => {
  it("验证通过优先，未验证其次，验证不通过不显示", () => {
    const verified = mk({ id: "v", supports_vision: "true" });
    const unknown = mk({ id: "u", supports_vision: "unknown" });
    const notSupported = mk({ id: "n", supports_vision: "false" });

    const result = filterModelsForPurpose([unknown, notSupported, verified], "vision");
    expect(result.map((m) => m.id)).toEqual(["v", "u"]);
  });

  it("视觉位按 supports_vision 过滤，不受 model_type=unknown 宽松规则影响", () => {
    const falseTypeUnknown = mk({
      id: "f",
      supports_vision: "false",
      model_type: "unknown" as ModelItem["model_type"],
    });
    const trueTypeUnknown = mk({
      id: "t",
      supports_vision: "true",
      model_type: "unknown" as ModelItem["model_type"],
    });

    const result = filterModelsForPurpose([falseTypeUnknown, trueTypeUnknown], "vision");
    expect(result.map((m) => m.id)).toEqual(["t"]);
  });

  it("非视觉位保持原有过滤", () => {
    const chatModel = mk({ id: "c", model_type: "chat" });
    const reasoningModel = mk({ id: "r", model_type: "reasoning" });
    const embedModel = mk({ id: "e", model_type: "embedding" });

    const result = filterModelsForPurpose([chatModel, reasoningModel, embedModel], "chat");
    expect(result.map((m) => m.id)).toEqual(["c", "r"]);
  });
});
