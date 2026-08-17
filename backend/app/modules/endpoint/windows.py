"""
上下文窗口映射与模型名归一化。

## 为什么需要归一化

中转站返回的模型名带各种前缀：
  openai/gpt-4o
  accounts/fireworks/models/qwen-72b
  Pro/deepseek-ai/DeepSeek-V3
  anthropic.claude-3-5-sonnet-20241022

按原名去映射表里查全部查不到，结果所有模型都回落到 32K 默认值。
失败模式是"大窗口模型被过早压缩"——用户只觉得"怎么老是压缩"，不会报错。

## 为什么不靠接口返回

/v1/models 的响应里【几乎从不包含】上下文窗口。OpenAI 官方不给，
绝大多数中转站也不给。所以必须内置映射表 + 手动兜底。
"""

import re

# 匹配是「前缀匹配 + 取最长命中」，不是精确相等 ——
# 模型名常带日期后缀（gpt-4o-2024-11-20）。
# 顺序无关，代码里按 key 长度降序尝试。
CONTEXT_WINDOWS: dict[str, int] = {
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4.1": 1_047_576,
    "gpt-4.1-mini": 1_047_576,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3": 200_000,
    "o3-mini": 200_000,
    "o4-mini": 200_000,
    # Anthropic
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-7-sonnet": 200_000,
    "claude-3-opus": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-opus-4": 200_000,
    # DeepSeek
    # v4 是 1M 窗口（官方 Models & Pricing 页确认，最大输出 384K）。
    # 少写一个 v4 条目的代价很具体：回落到 32K 默认值后，0.75 的压缩阈值
    # 会在 24K 就触发压缩 —— 明明还有 97% 的窗口没用，却开始丢历史。
    "deepseek-v4-flash": 1_000_000,
    "deepseek-v4-pro": 1_000_000,
    "deepseek-v4": 1_000_000,
    "deepseek-chat": 65_536,
    "deepseek-reasoner": 65_536,
    "deepseek-coder": 65_536,
    "deepseek-v3": 65_536,
    "deepseek-r1": 65_536,
    # 月之暗面
    "moonshot-v1-8k": 8_192,
    "moonshot-v1-32k": 32_768,
    "moonshot-v1-128k": 131_072,
    "kimi-k2": 131_072,
    "kimi-latest": 131_072,
    # 智谱
    "glm-4": 131_072,
    "glm-4-plus": 131_072,
    "glm-4-flash": 131_072,
    "glm-4.5": 131_072,
    "glm-4v": 8_192,
    # 通义千问
    "qwen-max": 32_768,
    "qwen-plus": 131_072,
    "qwen-turbo": 1_000_000,
    "qwen-long": 10_000_000,
    "qwen2.5": 131_072,
    "qwen3": 131_072,
    "qwq": 131_072,
    # 其它
    "gemini-1.5-pro": 2_097_152,
    "gemini-1.5-flash": 1_048_576,
    "gemini-2.0-flash": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
    "llama-3.1": 131_072,
    "llama-3.3": 131_072,
    "mistral-large": 131_072,
    "minimax": 245_760,
    "ernie-4.0": 8_192,
    "hunyuan": 32_768,
    "step-1": 8_192,
    "yi-large": 32_768,
    "grok-2": 131_072,
    "grok-3": 131_072,
    "gpt-oss": 131_072,
}

DEFAULT_WINDOW = 32_768

# 已知的中转站前缀形态。剥离时从左往右尝试。
_PREFIX_PATTERNS = (
    # accounts/fireworks/models/xxx
    re.compile(r"^accounts/[^/]+/models/", re.I),
    # openai/xxx, anthropic/xxx, deepseek-ai/xxx, Pro/deepseek-ai/xxx
    re.compile(r"^(pro|free|paid)/", re.I),
    re.compile(r"^[a-z0-9_.-]+/", re.I),
)


def normalize_model_name(raw: str) -> str:
    """
    剥前缀、去日期后缀、转小写。

    注意日期后缀的剥离要谨慎：moonshot-v1-8k 里的 8k 是窗口标识不能丢，
    所以只剥形如 -20241120 / -2024-11-20 / :20241120 的部分。
    """
    name = raw.strip()

    # 反复剥前缀，直到不再变化（Pro/deepseek-ai/DeepSeek-V3 有两层）
    for _ in range(4):
        before = name
        for pat in _PREFIX_PATTERNS:
            m = pat.match(name)
            if m:
                name = name[m.end() :]
                break
        if name == before:
            break

    # 点号分隔的端点前缀：anthropic.claude-3-5-sonnet
    if "." in name:
        head, _, tail = name.partition(".")
        # 只有当 head 看起来是端点名（纯字母）且 tail 还包含连字符时才剥,
        # 否则会把 qwen2.5 / glm-4.5 的版本号切掉
        if head.isalpha() and "-" in tail:
            name = tail

    name = name.lower()
    # 日期后缀。
    # 【不要】在这里加 v\d+ —— 那会把 deepseek-v3 剥成 deepseek、
    # moonshot-v1-8k 剥坏。版本号是模型标识的一部分，必须保留。
    name = re.sub(r"[-:@](\d{8}|\d{4}-\d{2}-\d{2})$", "", name)
    # 常见的 -latest / -preview 后缀
    name = re.sub(r"-(latest|preview|exp)$", "", name)
    return name


def lookup_window(raw_model_id: str) -> tuple[int, str]:
    """
    返回 (窗口大小, 来源)。来源 ∈ matched | default

    匹配用前缀 + 取最长命中：gpt-4o-mini 必须命中 gpt-4o-mini 而不是 gpt-4o。
    """
    name = normalize_model_name(raw_model_id)
    best: tuple[int, int] | None = None  # (key 长度, 窗口)
    for key, window in CONTEXT_WINDOWS.items():
        if name.startswith(key):
            if best is None or len(key) > best[0]:
                best = (len(key), window)
    if best is not None:
        return best[1], "matched"
    return DEFAULT_WINDOW, "default"


# 名字里含这些词的通常不能用于对话，探测后默认不勾选。
# bge / gte / m3e / jina 是常见的中文嵌入模型系列，名字里【没有】embed 字样 ——
# 只匹配 "embed" 会把它们当成对话模型列出来。
NON_CHAT_HINTS = (
    "embed",
    "bge-",
    "gte-",
    "m3e-",
    "jina-",
    "tts",
    "whisper",
    "rerank",
    "moderation",
    "dall-e",
    "stable-diffusion",
    "flux",
)


def looks_non_chat(model_id: str) -> bool:
    low = model_id.lower()
    return any(h in low for h in NON_CHAT_HINTS)


# 模型类型分类。探测/添加时按名字启发式判定一次并落库 —— 前端用图标显示，
# 用户可在编辑里改。
#
# ## 为什么靠名字而不是接口返回
#
# 标准 /v1/models 只返回 {id, object, created, owned_by}，不返回类型/能力。
# 硅基流动等平台的"推理 / 工具"分类只在它们网站上，API 里 type / sub_type
# 只是【过滤参数】不是返回值。supports_vision 之所以能用实测，是因为有
# verify-vision 发真实请求；类型没有对应的廉价探测手段，只能猜。
#
# ## 推理模型为什么单独一类
#
# 它先吐思维链（reasoning_content）再给答案，交互体验和普通对话模型不同
# （前端折叠思维链）。用户配模型时看到"推理"图标能立刻分辨，不用点进去。
_REASONING_HINTS = ("reasoner", "qwq", "thinking", "-r1", "r1-")
_REASONING_EXACT = frozenset(
    {"o1", "o1-mini", "o3", "o3-mini", "o4-mini", "gpt-5", "gpt-5-mini", "gpt-5-nano"}
)
_EMBED_HINTS = ("embed", "bge-", "gte-", "m3e-", "jina-")
_IMAGE_HINTS = ("dall-e", "stable-diffusion", "flux", "sdxl")
_AUDIO_HINTS = ("whisper", "sensevoice", "asr", "parakeet")


def detect_model_type(model_id: str) -> str:
    """
    按名字猜模型类型。返回 chat / reasoning / embedding / rerank / tts /
    audio / image。

    只做粗分类：猜错的代价是图标不准确（用户能改），而不是功能不可用。
    """
    low = model_id.lower()
    if (
        any(h in low for h in _REASONING_HINTS)
        or low in _REASONING_EXACT
        or low.startswith(("o1-", "o3-", "o4-"))
    ):
        return "reasoning"
    if "rerank" in low:
        return "rerank"
    if any(h in low for h in _EMBED_HINTS):
        return "embedding"
    if "tts" in low:
        return "tts"
    if any(h in low for h in _AUDIO_HINTS):
        return "audio"
    if any(h in low for h in _IMAGE_HINTS):
        return "image"
    return "chat"


def detect_vision_support(model_id: str) -> str:
    """
    按名字检测是否【肯定不支持】视觉输入。返回 "false" / "unknown"。

    设计原则：
    - 只判断明确不支持的类型（嵌入、音频、TTS）
    - 其余一律返回 "unknown"，由用户选择或实测
    - 不猜测"可能支持"，避免误导用户

    为什么不启发式判 true：
    - 模型名可能误导（如某些"vision"模型实际是图片生成而非理解）
    - 中转站可能改名、加前缀，导致判断失效
    - 默认 unknown 更安全，用户可以手动标记或通过 verify-vision 实测
    """
    low = model_id.lower()

    # 嵌入、音频、TTS 模型肯定不支持视觉
    if any(h in low for h in (_EMBED_HINTS + _AUDIO_HINTS + ("tts",))):
        return "false"

    # 其他情况一律返回 unknown，不猜测
    return "unknown"
