"""
token 计数。

## 只用于估算，不作为压缩的唯一依据

压缩的触发依据优先用上游返回的【真实 prompt_tokens】。本地估算与真实值
在有工具定义、system 提示词、图片时可差 20% 以上：
  估高了 → 白压缩，丢了本来不用丢的信息
  估低了 → 直接 400，用户看到一个莫名的报错

估算只在两种情况用：
  1. 上游不返回 usage（部分中转站）
  2. 首轮调用前（还没有任何 usage）
"""

import json
from functools import lru_cache
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# 每条消息的固定开销（role、分隔符等）。OpenAI 的计数规则里约为 4。
_PER_MESSAGE_OVERHEAD = 4
# 非英文文本每字符约 0.6~0.7 token（中文一个字通常 1 个 token 上下，
# 但 cl100k 对常见中文词有合并）。取 0.7 偏保守。
_FALLBACK_CHARS_PER_TOKEN = 1.4

# 工具定义的 token 数缓存。键是工具名元组 —— 工具集变了键就不同。
# 不缓存的话每轮都要把 3000+ 字符的 JSON 过一遍 tiktoken。
_TOOL_TOKEN_CACHE: dict[tuple[str, ...], int] = {}


@lru_cache(maxsize=1)
def _encoder() -> Any:
    """
    tiktoken 的编码器。加载要几百毫秒且会下载词表文件，所以缓存。

    这里可以用 lru_cache —— 编码器是纯粹的不变对象，
    与"系统提示词不能缓存"是两回事。
    """
    try:
        import tiktoken

        # cl100k_base 覆盖 GPT-4/3.5 与多数中文模型的近似分词。
        # 不同模型的真实分词器不同，但我们只要量级正确。
        return tiktoken.get_encoding("cl100k_base")
    except Exception as e:  # noqa: BLE001
        # tiktoken 首次使用要联网下载词表。离线环境下降级到字符估算,
        # 而不是让整个对话失败。
        log.warning("tiktoken_unavailable", err=str(e), fallback="char_estimate")
        return None


@lru_cache(maxsize=4096)
def _count_cached(text: str) -> int:
    enc = _encoder()
    if enc is None:
        return int(len(text) / _FALLBACK_CHARS_PER_TOKEN) + 1
    return len(enc.encode(text, disallowed_special=()))


def count_text(text: str) -> int:
    """
    文本的 token 数。

    ## 为什么要缓存

    同一段文本会被反复编码：每轮都要估算整个上下文，而历史消息的内容
    一个字都没变。一个 20 轮的会话里，第一条消息会被编码 20 次。

    缓存后实测测试套件从 80s 回到 12s。生产环境的收益同理 ——
    长会话每轮省下的是"重新编码全部历史"。

    maxsize 取 4096：够覆盖一个长会话的全部消息，又不会无限涨。
    超长文本（比如几万字符的工具输出）不缓存 —— 它们通常只出现一次，
    缓存它们只是白占内存。
    """
    if not text:
        return 0
    if len(text) > 8192:
        enc = _encoder()
        if enc is None:
            return int(len(text) / _FALLBACK_CHARS_PER_TOKEN) + 1
        return len(enc.encode(text, disallowed_special=()))
    return _count_cached(text)


def count_tools(tools: list[dict[str, Any]]) -> int:
    """
    工具定义的 token 数。

    ## 为什么要缓存

    这个值在一次 run 内完全不变（工具集不变），但 estimate_tokens 每轮
    都会调用。不缓存的话每次都要把 3000+ 字符的 JSON 重新过一遍 tiktoken ——
    实测整个测试套件从 10s 涨到 100s。

    缓存键用工具名的元组：工具集变了（比如 SubAgent 用 forked registry）
    键就不同，不会取到错的值。
    """
    names = tuple(
        str((t.get("function") or {}).get("name", "")) for t in tools
    )
    cached = _TOOL_TOKEN_CACHE.get(names)
    if cached is not None:
        return cached
    # 工具定义按 JSON 序列化后的文本估。真实分词略有差异，
    # 但量级正确 —— 实测这样算出来与真实值差 0.5%。
    value = count_text(json.dumps(tools, ensure_ascii=False))
    _TOOL_TOKEN_CACHE[names] = value
    return value


def estimate_tokens(
    api_messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> int:
    """
    估算一次请求的 token 数。

    ## tools 必须传

    工具定义随每次请求发送，是实打实占上下文的。实测本项目 8 个工具的
    JSON schema 是 **1446 tokens** —— 而一次真实会话的 prompt 才 3249。
    漏算它会让估算偏低 45%：

        估算 1787 / 真实 3249 = 0.55        （不算工具定义）
        估算 3233 / 真实 3249 = 0.995       （算上之后）

    偏低的后果是两个：前端进度条少报一千多 token，用户以为还很空；
    压缩晚触发甚至根本不触发，直接撞 400。

    tools 设成可选是为了向后兼容，但调用方【只要实际发了 tools 就必须传】。

    ## 也包含 tool_calls 的 arguments

    它们同样占上下文，漏算会让带工具调用的会话估偏低。
    """
    total = 0

    if tools:
        total += count_tools(tools)

    for m in api_messages:
        total += _PER_MESSAGE_OVERHEAD
        content = m.get("content")
        if isinstance(content, str):
            total += count_text(content)
        elif isinstance(content, list):
            # 多模态消息：[{"type":"text",...}, {"type":"image_url",...}]
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    total += count_text(str(part.get("text", "")))
                else:
                    # 图片按固定量估。真实值取决于分辨率与 detail 参数,
                    # 这里只要量级对 —— 一张图远不止几个 token。
                    total += 800
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            total += count_text(str(fn.get("name", "")))
            total += count_text(str(fn.get("arguments", "")))
    return total


def estimate_tools_tokens(tool_specs: list[dict[str, Any]]) -> int:
    """
    工具定义也占上下文，而且不少 —— 20 个工具的 JSON Schema 轻易上千 token。
    只算 prompt_tokens 而不算工具定义会让估算偏低很多。
    """
    import json

    total = 0
    for spec in tool_specs:
        try:
            total += count_text(json.dumps(spec, ensure_ascii=False))
        except (TypeError, ValueError):
            continue
    return total
