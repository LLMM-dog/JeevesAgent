/**
 * 语音输入。
 *
 * ## 为什么值得测
 *
 * 这段代码的分支几乎全是"出错时怎么办"：不支持、权限被拒、没听到声音、
 * 服务自己断开、组件卸载。手工验证只能覆盖顺利路径 ——
 * 而顺利路径恰恰是最不容易坏的那条。
 *
 * SpeechRecognition 在 jsdom 里不存在，所以全部用假实现驱动。
 */

import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { speechUploadsAudio, useSpeechInput } from "./useSpeechInput";

/** 假的 SpeechRecognition，能手动触发各种事件 */
class FakeRecognition {
  static instances: FakeRecognition[] = [];
  lang = "";
  continuous = false;
  interimResults = false;
  maxAlternatives = 0;
  onresult: ((e: unknown) => void) | null = null;
  onerror: ((e: unknown) => void) | null = null;
  onend: (() => void) | null = null;
  started = 0;
  stopped = 0;
  aborted = 0;
  throwOnStart = false;

  constructor() {
    FakeRecognition.instances.push(this);
  }

  start() {
    if (this.throwOnStart) throw new Error("boom");
    this.started++;
  }
  stop() {
    this.stopped++;
  }
  abort() {
    this.aborted++;
  }

  /** 模拟识别出结果 */
  emit(items: { text: string; final: boolean }[], resultIndex = 0) {
    const results: unknown[] = items.map((it) => {
      const r: Record<string, unknown> = { 0: { transcript: it.text }, isFinal: it.final };
      return r;
    });
    (results as unknown as { length: number }).length = items.length;
    this.onresult?.({ resultIndex, results });
  }
}

function install(ctor: unknown = FakeRecognition) {
  (window as unknown as Record<string, unknown>).SpeechRecognition = ctor;
}

afterEach(() => {
  FakeRecognition.instances = [];
  delete (window as unknown as Record<string, unknown>).SpeechRecognition;
  delete (window as unknown as Record<string, unknown>).webkitSpeechRecognition;
  vi.restoreAllMocks();
});

describe("能力探测", () => {
  it("没有 SpeechRecognition 时 supported=false", () => {
    const { result } = renderHook(() => useSpeechInput(() => {}));
    expect(result.current.supported).toBe(false);
  });

  it("认 webkit 前缀", () => {
    // 只查无前缀的话在 Chrome 上会被判定为"不支持" ——
    // 而 Chrome 恰恰是主要的支持方
    (window as unknown as Record<string, unknown>).webkitSpeechRecognition =
      FakeRecognition;
    const { result } = renderHook(() => useSpeechInput(() => {}));
    expect(result.current.supported).toBe(true);
  });

  it("不支持时 start 不抛异常", () => {
    const { result } = renderHook(() => useSpeechInput(() => {}));
    act(() => result.current.start());
    expect(result.current.listening).toBe(false);
  });
});

describe("识别流程", () => {
  it("start 后进入 listening 并设好参数", () => {
    install();
    const { result } = renderHook(() => useSpeechInput(() => {}));
    act(() => result.current.start());

    expect(result.current.listening).toBe(true);
    const rec = FakeRecognition.instances[0];
    expect(rec.started).toBe(1);
    // continuous：说完一句不自动停，否则长句子要点好几次
    expect(rec.continuous).toBe(true);
    // interimResults：没有的话按下按钮后界面好几秒毫无反应
    expect(rec.interimResults).toBe(true);
  });

  it("lang 跟随页面语言而不是硬编码", () => {
    // 硬编码 zh-CN 的话说英文会被强行识别成谐音的中文词，
    // 而用户完全看不出是语言设置的问题
    install();
    document.documentElement.lang = "en-US";
    const { result } = renderHook(() => useSpeechInput(() => {}));
    act(() => result.current.start());
    expect(FakeRecognition.instances[0].lang).toBe("en-US");
    document.documentElement.lang = "";
  });

  it("只把 final 结果交给回调", () => {
    install();
    const got: string[] = [];
    const { result } = renderHook(() => useSpeechInput((t) => got.push(t)));
    act(() => result.current.start());

    act(() =>
      FakeRecognition.instances[0].emit([
        { text: "你好", final: true },
        { text: "世界", final: false },
      ]),
    );
    expect(got).toEqual(["你好"]);
    // 临时结果单独暴露，用于显示"正在听…你说了什么"
    expect(result.current.interim).toBe("世界");
  });

  it("从 resultIndex 开始遍历，不重复插入已处理的文本", () => {
    // continuous 模式下 results 是累积的 ——
    // 从 0 遍历会把已经插入过的文本重复插一遍
    install();
    const got: string[] = [];
    const { result } = renderHook(() => useSpeechInput((t) => got.push(t)));
    act(() => result.current.start());

    const rec = FakeRecognition.instances[0];
    act(() => rec.emit([{ text: "第一句", final: true }], 0));
    // 第二次事件里 results 仍含第一句，但 resultIndex=1
    act(() =>
      rec.emit(
        [
          { text: "第一句", final: true },
          { text: "第二句", final: true },
        ],
        1,
      ),
    );
    expect(got).toEqual(["第一句", "第二句"]);
  });

  it("stop 退出 listening 并清掉临时文本", () => {
    install();
    const { result } = renderHook(() => useSpeechInput(() => {}));
    act(() => result.current.start());
    act(() => FakeRecognition.instances[0].emit([{ text: "半句", final: false }]));
    expect(result.current.interim).toBe("半句");

    act(() => result.current.stop());
    expect(result.current.listening).toBe(false);
    expect(result.current.interim).toBe("");
    expect(FakeRecognition.instances[0].stopped).toBe(1);
  });

  it("重复 start 视为 toggle，不创建第二个实例", () => {
    // 不判断的话连点两次会创建两个实例，
    // 第一个变成孤儿且永不停止（麦克风一直开着）
    install();
    const { result } = renderHook(() => useSpeechInput(() => {}));
    act(() => result.current.start());
    act(() => result.current.start());

    expect(FakeRecognition.instances).toHaveLength(1);
    expect(result.current.listening).toBe(false);
    expect(FakeRecognition.instances[0].stopped).toBe(1);
  });

  it("服务自己断开时也要退出 listening", () => {
    // 静默超时、网络问题都会走 onend 而不是 onerror。
    // 不重置的话按钮永远停在"正在听"，而实际已经停了
    install();
    const { result } = renderHook(() => useSpeechInput(() => {}));
    act(() => result.current.start());
    act(() => FakeRecognition.instances[0].onend?.());

    expect(result.current.listening).toBe(false);
  });

  it("start 抛异常时不进入 listening", () => {
    install(
      class extends FakeRecognition {
        constructor() {
          super();
          this.throwOnStart = true;
        }
      },
    );
    const { result } = renderHook(() => useSpeechInput(() => {}));
    act(() => result.current.start());
    expect(result.current.listening).toBe(false);
    expect(result.current.error).toContain("启动失败");
  });
});

describe("错误提示", () => {
  it.each([
    ["not-allowed", "权限"],
    ["no-speech", "没听到声音"],
    ["audio-capture", "打不开麦克风"],
    ["network", "联网"],
  ])("%s 翻成能指导下一步的中文", (code, expected) => {
    // 直接显示 not-allowed 等于没说。而这几种失败的处理动作完全不同：
    // 去改浏览器权限、换个安静的地方、检查网络
    install();
    const { result } = renderHook(() => useSpeechInput(() => {}));
    act(() => result.current.start());
    act(() => FakeRecognition.instances[0].onerror?.({ error: code }));

    expect(result.current.error).toContain(expected);
    expect(result.current.listening).toBe(false);
  });

  it("aborted 是用户自己取消的，不弹错误", () => {
    install();
    const { result } = renderHook(() => useSpeechInput(() => {}));
    act(() => result.current.start());
    act(() => FakeRecognition.instances[0].onerror?.({ error: "aborted" }));

    expect(result.current.error).toBeNull();
    expect(result.current.listening).toBe(false);
  });

  it("未知错误码也要有可读文案", () => {
    install();
    const { result } = renderHook(() => useSpeechInput(() => {}));
    act(() => result.current.start());
    act(() => FakeRecognition.instances[0].onerror?.({ error: "weird-thing" }));
    expect(result.current.error).toContain("weird-thing");
  });

  it("clearError 能清掉", () => {
    install();
    const { result } = renderHook(() => useSpeechInput(() => {}));
    act(() => result.current.start());
    act(() => FakeRecognition.instances[0].onerror?.({ error: "no-speech" }));
    act(() => result.current.clearError());
    expect(result.current.error).toBeNull();
  });

  it("再次 start 会清掉上次的错误", () => {
    install();
    const { result } = renderHook(() => useSpeechInput(() => {}));
    act(() => result.current.start());
    act(() => FakeRecognition.instances[0].onerror?.({ error: "no-speech" }));
    act(() => result.current.start());
    expect(result.current.error).toBeNull();
  });
});

describe("清理", () => {
  it("卸载时 abort 掉正在进行的识别", () => {
    // 不停的话麦克风一直开着（标签上的红点不消失），
    // 而用户已经离开这个页面了 —— 他会以为程序在偷偷录音
    install();
    const { result, unmount } = renderHook(() => useSpeechInput(() => {}));
    act(() => result.current.start());
    const rec = FakeRecognition.instances[0];

    unmount();
    expect(rec.aborted).toBe(1);
  });

  it("没在听时卸载不报错", () => {
    install();
    const { unmount } = renderHook(() => useSpeechInput(() => {}));
    expect(() => unmount()).not.toThrow();
  });

  it("回调变化不重建实例", () => {
    // 父组件重渲染时 onText 必然是新函数。
    // 放进 deps 的话正在进行的识别会被打断
    install();
    const { result, rerender } = renderHook(
      ({ cb }: { cb: (t: string) => void }) => useSpeechInput(cb),
      { initialProps: { cb: () => {} } },
    );
    act(() => result.current.start());
    rerender({ cb: () => {} });
    act(() => result.current.start());
    // 第二次 start 是 toggle-stop，不该有第二个实例
    expect(FakeRecognition.instances).toHaveLength(1);
  });

  it("回调更新后用的是最新那个", () => {
    install();
    const a: string[] = [];
    const b: string[] = [];
    const { result, rerender } = renderHook(
      ({ cb }: { cb: (t: string) => void }) => useSpeechInput(cb),
      { initialProps: { cb: (t: string) => a.push(t) } },
    );
    act(() => result.current.start());
    rerender({ cb: (t: string) => b.push(t) });
    act(() => FakeRecognition.instances[0].emit([{ text: "话", final: true }]));

    expect(a).toEqual([]);
    expect(b).toEqual(["话"]);
  });
});

describe("音频上传提示", () => {
  it("没有 processLocally 时按会上传提示", () => {
    // Chrome 的实现是服务端识别 —— MDN 明确写了音频会发到 web service。
    // 这个项目其它地方（私密模式、无鉴权警示条）都告知了数据流向，
    // 语音是唯一"用户说的话直接离开本机"的入口，不说反而更不一致
    install();
    expect(speechUploadsAudio()).toBe(true);
  });

  it("支持 processLocally 时不提示", () => {
    class Local extends FakeRecognition {}
    (Local.prototype as unknown as Record<string, unknown>).processLocally = false;
    install(Local);
    expect(speechUploadsAudio()).toBe(false);
  });
});
