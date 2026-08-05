/**
 * Composer 的语音输入入口。
 *
 * ## 只测一件事：不支持时不能留一个按钮
 *
 * SpeechRecognition 至今不是 Baseline（Firefox 长期不支持）。
 * 做法是不支持时 console.warn 然后 return，
 * 而按钮还在界面上 —— 用户点了没反应，也不知道为什么。
 *
 * 这条断言看起来很小，但它是这个功能唯一的"降级"逻辑。
 * 坏了的话在 Chrome 上完全看不出来 —— 而大部分开发都在 Chrome 上。
 */

import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Composer 依赖一堆 store 和子组件，全部打桩 ——
// 这里只关心麦克风按钮渲不渲染
vi.mock("@/store/chat", () => ({
  useChatStore: (sel: (s: Record<string, unknown>) => unknown) =>
    sel({
      send: vi.fn(),
      visionMode: false,
      setVisionMode: vi.fn(),
      stop: vi.fn(),
      approvalMode: "auto",
      setApprovalMode: vi.fn(),
      pending: false,
      streaming: null,
      usage: null,
    }),
}));
vi.mock("@/components/MacroPicker", () => ({ MacroPicker: () => null }));
vi.mock("@/components/RefPicker", () => ({ RefPicker: () => null }));

class FakeRecognition {
  lang = "";
  continuous = false;
  interimResults = false;
  maxAlternatives = 0;
  onresult: unknown = null;
  onerror: unknown = null;
  onend: unknown = null;
  start() {}
  stop() {}
  abort() {}
}

beforeEach(() => {
  // 每个用例自己 resetModules + import ——
  // supported 是 useState 的初始值，只在首次渲染时读一次 window，
  // 所以必须在装好假实现【之后】才导入模块
  vi.resetModules();
});

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).SpeechRecognition;
  delete (window as unknown as Record<string, unknown>).webkitSpeechRecognition;
});

describe("麦克风按钮", () => {
  it("浏览器不支持时【整个不渲染】", async () => {
    const C = (await import("./Composer")).default;
    render(<C />);
    expect(screen.queryByLabelText("语音输入")).toBeNull();
  });

  it("支持时渲染出来", async () => {
    (window as unknown as Record<string, unknown>).SpeechRecognition =
      FakeRecognition;
    vi.resetModules();
    const C = (await import("./Composer")).default;
    render(<C />);
    expect(screen.queryByLabelText("语音输入")).not.toBeNull();
  });

  it("认 webkit 前缀", async () => {
    (window as unknown as Record<string, unknown>).webkitSpeechRecognition =
      FakeRecognition;
    vi.resetModules();
    const C = (await import("./Composer")).default;
    render(<C />);
    expect(screen.queryByLabelText("语音输入")).not.toBeNull();
  });

  it("title 里说明音频会上传", async () => {
    // 这个项目其它地方都告知了数据流向，
    // 语音是唯一"用户说的话直接离开本机"的入口
    (window as unknown as Record<string, unknown>).SpeechRecognition =
      FakeRecognition;
    vi.resetModules();
    const C = (await import("./Composer")).default;
    render(<C />);
    const btn = screen.getByLabelText("语音输入");
    expect(btn.getAttribute("title")).toContain("音频");
  });

  it("aria-pressed 反映状态", async () => {
    (window as unknown as Record<string, unknown>).SpeechRecognition =
      FakeRecognition;
    vi.resetModules();
    const C = (await import("./Composer")).default;
    render(<C />);
    expect(screen.getByLabelText("语音输入").getAttribute("aria-pressed")).toBe(
      "false",
    );
  });
});
