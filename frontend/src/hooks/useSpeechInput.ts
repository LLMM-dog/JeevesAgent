/**
 * 语音输入（Web Speech API）。
 *
 * ## 为什么用浏览器 API 而不是本地 whisper
 *
 * 零依赖、零配置。本地 whisper 准确率更高，但要拉几百 MB 模型、
 * 要装 ffmpeg、还要一个转写接口 —— 对"个人项目模板"来说太重了。
 *
 * 代价写清楚（见 `remoteWarning`）：Chrome 的实现把音频发到 Google
 * 的服务器识别。这件事必须让用户知道，不能默认它无所谓。
 *
 * ## 关键取舍：不支持时不要留一个按钮
 *
 * `SpeechRecognition` 至今不是 Baseline（Firefox 长期不支持）。
 * 做法是不支持时 `console.warn` 然后 return，
 * 而按钮还在界面上 —— 用户点了没反应，也不知道为什么。
 *
 * 这里返回 `supported`，由调用方决定不渲染。和"选图按钮只在视觉模式下
 * 出现"是同一个原则：给个能点但没用的按钮只会让人困惑。
 */

import { useCallback, useEffect, useRef, useState } from "react";

/** 识别到文字时的回调。text 是本次新增的最终文本 */
type OnText = (text: string) => void;

interface SpeechState {
  /** 当前浏览器是否可用。false 时调用方应该不渲染入口 */
  supported: boolean;
  listening: boolean;
  /** 给用户看的错误，已翻成中文。null 表示没有 */
  error: string | null;
  /** 识别中的临时文本，用于显示"正在听…你说了什么" */
  interim: string;
  start: () => void;
  stop: () => void;
  clearError: () => void;
}

function getCtor(): any {
  if (typeof window === "undefined") return null;
  const w = window as any;
  // webkit 前缀是 Chrome/Safari 的历史实现，至今仍是它们的主要入口 ——
  // 只查无前缀的话在 Chrome 上会被判定为"不支持"
  return w.SpeechRecognition || w.webkitSpeechRecognition || null;
}

/**
 * 错误码翻成能指导下一步的中文。
 *
 * 原始错误码是 `not-allowed` / `no-speech` 这种，直接显示给用户
 * 等于没说。而这几种失败的处理动作完全不同：去改浏览器权限、
 * 换个安静的地方、检查网络。
 */
function explain(code: string): string {
  switch (code) {
    case "not-allowed":
    case "service-not-allowed":
      return "麦克风权限被拒绝。点地址栏左侧的图标重新允许，然后刷新页面";
    case "no-speech":
      return "没听到声音。检查麦克风是否被静音或选错了设备";
    case "audio-capture":
      return "打不开麦克风。可能被其它程序占用，或者系统里没有可用的输入设备";
    case "network":
      // 这一条最容易让人困惑 —— 明明是"语音识别"，为什么报网络错误
      return "识别服务连不上。Chrome 的语音识别需要联网（音频要发到 Google 的服务器）";
    case "aborted":
      return "";
    default:
      return `语音识别失败：${code}`;
  }
}

export function useSpeechInput(onText: OnText): SpeechState {
  const [supported] = useState(() => getCtor() !== null);
  const [listening, setListening] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [interim, setInterim] = useState("");
  const recRef = useRef<any>(null);
  // 回调放 ref —— 不然每次 onText 变化（父组件重渲染时必然变）
  // 都要重建 recognition 实例，正在进行的识别会被打断
  const cbRef = useRef(onText);
  cbRef.current = onText;

  const stop = useCallback(() => {
    const rec = recRef.current;
    if (!rec) return;
    recRef.current = null;
    try {
      rec.stop();
    } catch {
      // 可能已经自己结束了
    }
    setListening(false);
    setInterim("");
  }, []);

  const start = useCallback(() => {
    const Ctor = getCtor();
    if (!Ctor) return;
    // 已经在听就当作停止 —— 这个按钮是 toggle。
    // 不判断的话连点两次会创建两个实例，第一个变成孤儿且永不停止
    if (recRef.current) {
      stop();
      return;
    }
    setError(null);
    setInterim("");

    let rec: any;
    try {
      rec = new Ctor();
    } catch {
      setError("无法初始化语音识别");
      return;
    }

    // 跟随页面语言而不是硬编码 zh-CN。
    //
    // 硬编码的话说英文会被强行识别成谐音的中文词，
    // 而用户完全看不出来是语言设置的问题。
    rec.lang = document.documentElement.lang || navigator.language || "zh-CN";
    // continuous=true：说完一句不自动停，让用户自己控制结束。
    // false 的话说一句就断，长句子要点好几次
    rec.continuous = true;
    // 要临时结果 —— 没有的话按下按钮后好几秒界面毫无反应，
    // 用户不知道到底有没有在听
    rec.interimResults = true;
    rec.maxAlternatives = 1;

    rec.onresult = (ev: any) => {
      let final = "";
      let pending = "";
      // 从 resultIndex 开始遍历，而不是从 0 ——
      // continuous 模式下 results 是累积的，从 0 遍历会把已经插入过的
      // 文本重复插一遍
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const r = ev.results[i];
        if (r.isFinal) final += r[0].transcript;
        else pending += r[0].transcript;
      }
      setInterim(pending);
      if (final) cbRef.current(final);
    };

    rec.onerror = (ev: any) => {
      const msg = explain(ev.error || "unknown");
      // aborted 是用户自己取消的，不该弹错误
      if (msg) setError(msg);
      recRef.current = null;
      setListening(false);
      setInterim("");
    };

    rec.onend = () => {
      // 【必须在这里也重置】—— 识别服务可能自己断开
      // （静默超时、网络问题），此时没有 onerror。
      // 不重置的话按钮永远停在"正在听"状态，而实际已经停了
      recRef.current = null;
      setListening(false);
      setInterim("");
    };

    try {
      rec.start();
    } catch (e) {
      setError(`启动失败：${String(e).slice(0, 80)}`);
      return;
    }
    recRef.current = rec;
    setListening(true);
  }, [stop]);

  // 组件卸载时必须停掉。
  //
  // 不停的话麦克风一直开着（浏览器标签上的红点不消失），
  // 而用户已经离开这个页面了 —— 他会以为程序在偷偷录音。
  useEffect(() => {
    return () => {
      const rec = recRef.current;
      recRef.current = null;
      if (rec) {
        try {
          rec.abort();
        } catch {
          // 忽略
        }
      }
    };
  }, []);

  return {
    supported,
    listening,
    error,
    interim,
    start,
    stop,
    clearError: useCallback(() => setError(null), []),
  };
}

/**
 * 是否需要提示"音频会上传"。
 *
 * Chrome 的实现是服务端识别（MDN 明确写了：audio is sent to a web
 * service for recognition processing, so it won't work offline）。
 *
 * `processLocally` 是新加的属性，支持的浏览器上可以要求本地识别 ——
 * 但目前支持面很窄，所以默认按"会上传"提示。
 *
 * 为什么一定要提示：这个项目的其它地方（私密模式、无鉴权警示条）都
 * 明确告知了数据流向。语音是唯一一个"用户说的话直接离开本机"的入口，
 * 不说反而更不一致。
 */
export function speechUploadsAudio(): boolean {
  if (typeof window === "undefined") return true;
  const proto = (window as any).SpeechRecognition?.prototype;
  // 有 processLocally 说明浏览器支持本地识别，但不代表默认就是本地
  return !(proto && "processLocally" in proto);
}
