"""
搜索 provider。

## 为什么用能力标志声明

抄 `BaseSearchProvider`：每个 provider
声明自己需不需要 key、支不支持某种能力，路由层只看标志位。

这样加 provider 不用改路由，也不会出现 `if name == "tavily"` 这种硬编码。

## 只做两个 provider

做了 9 个（bing/google/bocha/unifuncs/duckduckgo/searxng/jina/
crawl4ai/tavily），因为它要面向不同地区的用户。个人项目用不上。

- **DuckDuckGo**：免费无 key，让功能开箱可用
- **Tavily**：有 key 时质量更好（snippet 是为 LLM 优化过的）

SearXNG 要自建实例，先不做。
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)

# 单条摘要的长度上限。
#
# 搜索结果的 snippet 由搜索引擎决定长度，Tavily 的 content 字段可能
# 几千字符。10 条结果 × 几千字符就是白烧的 token ——
# 而搜索的目的是【发现】，读正文是 web_fetch 的事。
MAX_SNIPPET_CHARS = 400

# 结果条数上限。
#
# 给模型 20 条结果它也读不完，只会挑前几个。
MAX_RESULTS = 10


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str


class Provider:
    """
    搜索 provider 基类。

    能力用类属性声明，不用方法探测 —— 路由层需要在【不构造实例】的情况下
    知道能力（构造可能因缺 key 而失败）。
    """

    name = "base"
    requires_api_key = False

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        raise NotImplementedError


class DuckDuckGo(Provider):
    """
    免费无 key。

    ## 为什么放在第一位

    它让联网搜索【开箱可用】。要求用户先去注册 Tavily 账号才能用搜索，
    大多数人不会去做 —— 功能就等于不存在。
    """

    name = "duckduckgo"
    requires_api_key = False

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        import asyncio

        def _sync() -> list[dict]:
            from ddgs import DDGS

            with DDGS() as d:
                return list(d.text(query, max_results=limit))

        # ddgs 是同步库，必须丢线程池。
        #
        # 直接 await 不了，而在事件循环里跑同步 HTTP 会【阻塞所有其它请求】
        # —— 包括其它会话的 SSE 流。
        #
        # Tavily 工具就是同步 _run 且没有 _arun
        # ，靠 LangChain 兜底丢线程池 ——
        # 依赖框架行为而不是显式设计。
        rows = await asyncio.to_thread(_sync)
        return [
            SearchHit(
                title=str(r.get("title") or "")[:200],
                url=str(r.get("href") or r.get("url") or ""),
                snippet=str(r.get("body") or "")[:MAX_SNIPPET_CHARS],
            )
            for r in rows
            if r.get("href") or r.get("url")
        ]


class Tavily(Provider):
    """
    需要 API key。snippet 质量比 DuckDuckGo 好（为 LLM 优化过）。

    ## 为什么不开 include_raw_content

    Tavily 支持一次性返回全文，但那会让返回量失控 ——
    `max_results=10` × 整页正文。

    暴露了这个参数且不做任何截断，
    工具描述里写着"应该先搜索再用 extract 深入阅读"，但提示挡不住模型
    图省事。

    **更强的做法是不给这个选项**：工具不支持返回正文，模型就只能分两步。
    """

    name = "tavily"
    requires_api_key = True

    def __init__(self, api_key: str) -> None:
        if not api_key:
            # 早失败：不这么做的话错误会推迟到第一次搜索时才出现，
            # 而那时用户已经在等结果了
            raise ValueError("Tavily 需要 API key")
        self._key = api_key

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        import asyncio

        def _sync() -> dict:
            from tavily import TavilyClient

            return TavilyClient(api_key=self._key).search(
                query=query,
                max_results=limit,
                search_depth="basic",
                # 不要全文 —— 见类 docstring
                include_raw_content=False,
                include_answer=False,
            )

        res = await asyncio.to_thread(_sync)
        return [
            SearchHit(
                title=str(r.get("title") or "")[:200],
                url=str(r.get("url") or ""),
                snippet=str(r.get("content") or "")[:MAX_SNIPPET_CHARS],
            )
            for r in (res.get("results") or [])
            if r.get("url")
        ]


def pick_provider() -> Provider | None:
    """
    按配置选 provider。没有可用的返回 None。

    ## 为什么由配置显式决定而不是自动探测

    `JEEVES_WEBSEARCH__BACKEND` 默认是 `none` —— 联网搜索**默认关闭**。

    自动探测（"装了 ddgs 就启用"）会让功能悄悄打开。而联网是个有副作用的
    能力：它会把用户的查询词发给第三方搜索引擎。这种事必须用户显式同意，
    不能因为装了个包就默认开。

    ## 为什么返回 None 而不抛异常

    调用方（工具注册）需要据此决定【是否注册 web_search 工具】。

    注册了但用不了的工具，模型会去调它、拿到"未配置"错误、可能反复重试。
    而它的工具定义每轮都在烧 token。

    这是本项目已有的原则（`开发笔记`）：没有 websearch 后端时不注册
    `web_search`。
    """
    from app.core.config import settings

    backend = (settings.websearch.backend or "none").strip().lower()
    if backend in ("", "none", "off", "disabled"):
        return None

    if backend == "tavily":
        key = (settings.websearch.tavily_api_key or "").strip()
        if not key:
            # 配了 backend=tavily 却没给 key —— 这是配置错误，要说清楚。
            # 静默回落到 duckduckgo 的话，用户以为在用 Tavily
            # （付费、质量更好），实际不是。
            log.warning(
                "websearch_misconfigured",
                detail="backend=tavily 但 TAVILY_API_KEY 为空，web_search 不注册",
            )
            return None
        try:
            return Tavily(key)
        except (ValueError, ImportError) as e:
            log.warning("tavily_unavailable", err=str(e)[:150])
            return None

    if backend in ("duckduckgo", "ddg"):
        try:
            import ddgs  # noqa: F401

            return DuckDuckGo()
        except ImportError:
            log.warning(
                "websearch_unavailable",
                detail="backend=duckduckgo 但 ddgs 未安装。装：uv sync --extra search",
            )
            return None

    if backend == "searxng":
        # SearXNG 需要自建实例，暂未实现。
        #
        # 明确说"未实现"而不是静默回落 —— 配了它的用户需要知道
        # 为什么搜索不工作。
        log.warning(
            "websearch_unavailable",
            detail="searxng 后端尚未实现，可改用 duckduckgo（免费）或 tavily",
        )
        return None

    log.warning("websearch_unknown_backend", backend=backend)
    return None
