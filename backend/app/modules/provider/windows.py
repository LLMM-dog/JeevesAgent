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

    # 点号分隔的供应商前缀：anthropic.claude-3-5-sonnet
    if "." in name:
        head, _, tail = name.partition(".")
        # 只有当 head 看起来是供应商名（纯字母）且 tail 还包含连字符时才剥,
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
