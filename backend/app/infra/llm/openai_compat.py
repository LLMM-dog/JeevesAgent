"""
OpenAI 兼容协议实现。

本文件承载了几个用真实故障换来的配置，改动前先读注释。
"""

import asyncio
import json
import random
from collections.abc import AsyncIterator
from typing import Any

import httpx
import structlog

from app.core.config import settings
from app.core.exceptions import ProviderError
from app.infra.llm.port import LLMChunk, ResolvedModel, TokenUsage, ToolCallDelta

log = structlog.get_logger(__name__)

# 上游返回 400 时，用关键词判断是不是"上下文超长"。
# 照抄 这份集合—— 它是踩出来的。
# 正常情况下我们有 context_window + 真实 usage 触发压缩，不该走到这里；
# 但模型窗口配错时（window_source=default 猜的 32K，实际只有 8K）就会撞上。
_TOKEN_EXCEED_KEYWORDS = frozenset(
    {
        "maximum context length",
        "context length exceeded",
        "prompt too long",
        "input too long",
        "request too large",
        "too many tokens",
        "reduce the length",
        "reduce tokens",
        "token limit",
        "message too long",
    }
)


_RATE_LIMIT_KEYWORDS = frozenset(
    {
        "too many requests",
        "rate limit",
        "quota",
        "exceeded your current quota",
        "requests per min",
        "tokens per min",
        "rpm",
        "tpm",
        "concurrency",
    }
)

# 二级交叉匹配用的词集。中转站的错误措辞五花八门，一级词组匹配不全时
# 用 对象 × 动作 的交叉来兜。照抄 guess_exception_type
# 的两级结构。
_RATE_OBJECTS = frozenset({"rate", "concurrency", "rpm", "tpm"})
_RATE_ACTIONS = frozenset({"exceed", "limit", "reach"})
_TOKEN_OBJECTS = frozenset({"token", "context", "prompt", "input"})
_TOKEN_ACTIONS = frozenset({"exceed", "long", "limit", "large"})


def is_context_overflow(err_text: str) -> bool:
    low = err_text.lower()
    return any(k in low for k in _TOKEN_EXCEED_KEYWORDS)


def classify_error(err_text: str) -> str:
    """
    把上游错误分成 token_exceed / rate_limit / others。

    三者的正确应对完全不同：
      token_exceed → 必须先压缩再重试（直接重试会再超一次）
      rate_limit   → 退避后重试
      others       → 看是否永久错误

    两级匹配：先整词组，再 对象×动作 交叉。
    """
    low = err_text.lower()

    if any(k in low for k in _RATE_LIMIT_KEYWORDS):
        return "rate_limit"
    if any(k in low for k in _TOKEN_EXCEED_KEYWORDS):
        return "token_exceed"

    if any(o in low for o in _RATE_OBJECTS) and any(a in low for a in _RATE_ACTIONS):
        return "rate_limit"
    if any(o in low for o in _TOKEN_OBJECTS) and any(a in low for a in _TOKEN_ACTIONS):
        return "token_exceed"

    return "others"


def normalize_base_url(raw: str) -> str:
    """
    用户实际会填成各种样子，必须容错。规范化表见
    docs/01-architecture/providers.md#base_url-规范化

    只做了去尾斜杠，用户填
    https://api.openai.com 直接 404 —— 这是它最该改进的地方。
    """
    url = raw.strip().rstrip("/")
    if not url:
        return url
    # 用户可能粘了完整的 chat 端点
    for suffix in ("/chat/completions", "/completions"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    url = url.rstrip("/")
    # 已经以 /v1 或 /v2 等版本段结尾就不动
    last = url.rsplit("/", 1)[-1]
    if last.startswith("v") and last[1:].isdigit():
        return url
    return url + "/v1"


def parse_model_list(payload: Any) -> list[str]:
    """
    兼容三种响应形态。照抄 前端的容错逻辑
    —— 中转站的返回格式确实五花八门。

      裸数组          [{...}, ...]
      OpenAI 标准     {"data": [{...}]}
      Ollama 等       {"models": [{...}]}

    元素里取 id 或 name。
    """
    items: Any
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            items = payload["data"]
        elif isinstance(payload.get("models"), list):
            items = payload["models"]
        else:
            raise ProviderError(
                "端点返回了非标准格式",
                code="provider_probe_failed",
                hint="响应里既没有 data 也没有 models 数组，请手动输入模型名",
            )
    else:
        raise ProviderError(
            "端点返回了非标准格式",
            code="provider_probe_failed",
            hint="请手动输入模型名",
        )

    out: list[str] = []
    for it in items:
        if isinstance(it, str):
            name = it
        elif isinstance(it, dict):
            name = it.get("id") or it.get("name") or ""
        else:
            continue
        if isinstance(name, str) and name:
            out.append(name)
    # 去重保序
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))]


class OpenAICompatLLM:
    """实现 LLMPort。"""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                # httpx 默认会读环境变量与 Windows 注册表里的系统代理。
                # 实测：本机开着 Clash 时，同一个长生成请求走代理 32s 被掐断，
                # 绕过代理 252s 正常完成。代理软件的空闲连接超时远短于一次长推理，
                # 而推理阶段不吐任何字节，连接在代理看来就是"空闲"的。
                trust_env=settings.llm.trust_env,
                timeout=httpx.Timeout(
                    # connect 短、read 长：连不上要快速失败，
                    # 连上之后要能等完整个推理过程。
                    connect=15.0,
                    read=float(settings.llm.request_timeout),
                    write=30.0,
                    pool=30.0,
                ),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def stream_chat(
        self,
        model: ResolvedModel,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = True,
        **kwargs: Any,
    ) -> AsyncIterator[LLMChunk]:
        body: dict[str, Any] = {
            "model": model.model_id,
            "messages": messages,
            "stream": stream,
            # 要求上游在流末尾给真实 usage。压缩的触发依据必须是真实
            # prompt_tokens 而非本地估算 —— 估算与真实值在有工具定义、
            # system 提示词、图片时可差 20% 以上，估高了白压缩，估低了直接 400。
            "stream_options": {"include_usage": True},
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        body.update({k: v for k, v in kwargs.items() if v is not None})

        url = f"{model.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {model.api_key}",
            "Content-Type": "application/json",
        }

        # 重试【只覆盖首个 chunk 到达之前】。
        #
        # 一旦开始 yield 内容，前端已经显示了前半段文字，重试会让用户看到
        # 重复的文本。所以 started 一旦置 True 就绝不再重试，异常直接上抛。
        #
        # 状态码检查发生在任何 yield 之前，所以那一段是安全的重试窗口。
        attempt = 0
        while True:
            started = False
            try:
                client = self._get_client()
                async with client.stream("POST", url, json=body, headers=headers) as resp:
                    if resp.status_code >= 400:
                        raw = (await resp.aread()).decode("utf-8", errors="replace")
                        raise self._make_error(resp.status_code, raw, model)
                    async for chunk in self._iter_sse(resp):
                        started = True
                        yield chunk
                return
            except (httpx.TimeoutException, httpx.RequestError, ProviderError) as e:
                if started or not self._should_retry(e) or attempt >= settings.llm.max_retries:
                    raise self._wrap_stream_error(e, model) from e
                attempt += 1
                delay = min(2.0**attempt, 30.0)
                # 抖动，避免多个并发请求同时重试造成惊群
                delay += random.uniform(0, delay * 0.1)
                log.info(
                    "llm_retry",
                    attempt=attempt,
                    max_retries=settings.llm.max_retries,
                    wait=round(delay, 2),
                    err=str(e)[:200],
                )
                await asyncio.sleep(delay)

    @staticmethod
    def _should_retry(e: Exception) -> bool:
        """
        只重试瞬时错误。永久错误（key 错、模型名错、参数错）重试是纯浪费,
        而且会让用户多等 8 次退避才看到那个本来立刻就能给出的错误提示。
        """
        if isinstance(e, ProviderError):
            # 上下文超长要先压缩，不能在这层干重试
            if e.code == "context_overflow":
                return False
            kind = classify_error(f"{e.message} {e.hint or ''}")
            return kind == "rate_limit"
        # 超时与连接错误都是瞬时的
        return isinstance(e, httpx.TimeoutException | httpx.RequestError)

    @staticmethod
    def _wrap_stream_error(e: Exception, model: ResolvedModel) -> ProviderError:
        if isinstance(e, ProviderError):
            return e
        if isinstance(e, httpx.TimeoutException):
            return ProviderError(
                f"请求超时（{settings.llm.request_timeout}s）",
                hint="推理模型可能需要更长时间，可调大 JEEVES_LLM__REQUEST_TIMEOUT",
            )
        return ProviderError(
            f"无法连接到模型服务：{e}",
            hint=f"检查 base_url 是否正确（当前 {model.base_url}），以及是否需要代理",
        )

    @staticmethod
    def _make_error(status: int, raw: str, model: ResolvedModel) -> ProviderError:
        detail = raw[:800]
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                err = parsed.get("error")
                if isinstance(err, dict):
                    detail = str(err.get("message") or detail)
                elif isinstance(err, str):
                    detail = err
        except (json.JSONDecodeError, TypeError):
            pass

        if status in (401, 403):
            return ProviderError(
                "API Key 无效或无权限", hint=f"模型返回：{detail[:200]}"
            )
        if status == 404:
            return ProviderError(
                f"模型 {model.model_id} 不存在或端点路径不对",
                hint=f"已请求 {model.base_url}/chat/completions",
            )
        if status == 429:
            return ProviderError("触发上游限流", hint=detail[:200])
        if status == 400 and is_context_overflow(detail):
            # 标成专用 code，让 loop 能识别并强制压缩重试一次
            return ProviderError(
                "上下文超出模型窗口",
                code="context_overflow",
                hint=f"该模型的 context_window 配置可能不准（当前按 {model.context_window} 处理）",
            )
        return ProviderError(f"模型服务返回 {status}", hint=detail[:300])

    async def _iter_sse(self, resp: httpx.Response) -> AsyncIterator[LLMChunk]:
        async for line in resp.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                # 一个坏 chunk 不该终止整个流
                log.warning("llm_chunk_parse_failed", raw=data[:200])
                continue
            # async 生成器里不能用 yield from
            for chunk in self._convert(payload):
                yield chunk

    @staticmethod
    def _convert(payload: dict[str, Any]) -> list[LLMChunk]:
        out: list[LLMChunk] = []

        usage = payload.get("usage")
        if isinstance(usage, dict):
            details = usage.get("completion_tokens_details") or {}
            out.append(
                LLMChunk(
                    kind="usage",
                    usage=TokenUsage(
                        prompt_tokens=int(usage.get("prompt_tokens") or 0),
                        completion_tokens=int(usage.get("completion_tokens") or 0),
                        reasoning_tokens=int(
                            (details or {}).get("reasoning_tokens") or 0
                        ),
                    ),
                )
            )

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return out

        choice = choices[0]
        delta = choice.get("delta") or {}

        # 推理内容单独一类。不同模型字段名不一致，在这里归一：
        #   DeepSeek-R1 → reasoning_content
        #   其它         → reasoning
        # 前端要折叠显示思维链，不能混在正文里。
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            out.append(LLMChunk(kind="reasoning", text=reasoning))

        content = delta.get("content")
        if isinstance(content, str) and content:
            out.append(LLMChunk(kind="content", text=content))

        for tc in delta.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            out.append(
                LLMChunk(
                    kind="tool_call",
                    tool_call=ToolCallDelta(
                        # index 是必需的：一轮里多个 tool_call 并行流式返回时,
                        # id 只在第一个 chunk 出现，后续只有 index + arguments 片段。
                        index=int(tc.get("index") or 0),
                        call_id=tc.get("id"),
                        name=fn.get("name"),
                        arguments_delta=fn.get("arguments") or "",
                    ),
                )
            )

        finish = choice.get("finish_reason")
        if finish:
            out.append(LLMChunk(kind="done", finish_reason=str(finish)))

        return out

    async def list_models(self, base_url: str, api_key: str) -> list[str]:
        """
        探测模型列表。

        用 AsyncClient 且超时短（15s）—— 在 async 路由里用同步 httpx.get()
        且没有 timeout，会阻塞整个 event loop，模型域名不可达时能挂很久。

        探测是交互式操作，用户在等着看结果，不能给 300 秒。
        """
        normalized = normalize_base_url(base_url)
        candidates = [normalized]
        # 某些自建服务的路径不含 /v1。规范化后失败要用原始 URL 再试一次。
        raw = base_url.strip().rstrip("/")
        if raw and raw != normalized:
            candidates.append(raw)

        headers = {"Content-Type": "application/json"}
        if api_key.strip():
            # 本地 Ollama 等不需要 key。前端强制要求填 key 是误拦。
            headers["Authorization"] = f"Bearer {api_key}"

        last_err: ProviderError | None = None
        async with httpx.AsyncClient(
            trust_env=settings.llm.trust_env,
            timeout=httpx.Timeout(float(settings.llm.probe_timeout)),
        ) as client:
            for base in candidates:
                url = f"{base}/models"
                try:
                    resp = await client.get(url, headers=headers)
                except httpx.ConnectError as e:
                    last_err = ProviderError(
                        "无法连接到该地址",
                        code="provider_probe_failed",
                        hint=f"已尝试 {url}。检查域名拼写、网络，或是否需要代理",
                    )
                    log.warning("probe_connect_failed", url=url, err=str(e))
                    continue
                except httpx.TimeoutException:
                    last_err = ProviderError(
                        f"连接超时（{settings.llm.probe_timeout}s）",
                        code="provider_probe_failed",
                        hint=f"已尝试 {url}。检查网络或是否需要代理",
                    )
                    continue

                if resp.status_code in (401, 403):
                    # 鉴权失败不必再试第二个候选 —— 地址是对的，是 key 的问题
                    raise ProviderError(
                        "API Key 无效或无权限",
                        code="provider_probe_failed",
                        hint=f"端点 {url} 返回 {resp.status_code}",
                    )
                if resp.status_code == 404:
                    last_err = ProviderError(
                        "端点不存在",
                        code="provider_probe_failed",
                        hint=(
                            f"已尝试 {'、'.join(f'{b}/models' for b in candidates)}。"
                            "该服务可能不提供模型列表接口，可手动输入模型名"
                        ),
                    )
                    continue
                if resp.status_code >= 400:
                    last_err = ProviderError(
                        f"端点返回 {resp.status_code}",
                        code="provider_probe_failed",
                        hint=resp.text[:300],
                    )
                    continue

                try:
                    models = parse_model_list(resp.json())
                except json.JSONDecodeError:
                    last_err = ProviderError(
                        "端点返回的不是 JSON",
                        code="provider_probe_failed",
                        hint=f"{url} 返回：{resp.text[:200]}",
                    )
                    continue

                if not models:
                    raise ProviderError(
                        "该 Key 下没有可用模型",
                        code="provider_probe_failed",
                        hint="列表为空。确认该账号已开通模型权限",
                    )
                return models

        raise last_err or ProviderError(
            "探测失败", code="provider_probe_failed", hint="原因未知"
        )

    async def probe_chat(
        self,
        *,
        base_url: str,
        api_key: str,
        model_id: str,
        messages: list[dict[str, Any]],
    ) -> str:
        """
        一次性非流式请求，返回完整文本。用于能力核验。

        ## 异常原样上抛

        调用方（vision.probe_vision）需要看到上游原话才能给出有用的提示 ——
        "模型不支持图片"和"key 没权限"和"模型名写错"的修复动作完全不同，
        统一包装成"探测失败"等于把有用信息扔掉。

        所以这里不做 ProviderError 包装，把 httpx 的错误和响应体带出去。
        """
        base = normalize_base_url(base_url)
        headers = {"Content-Type": "application/json"}
        if api_key.strip():
            headers["Authorization"] = f"Bearer {api_key}"

        # 核验用短超时。它是交互式操作，用户在设置页等着。
        # 但比 list_models 宽一点 —— 多模态请求要处理图片，
        # 首 token 时间通常比纯文本长。
        timeout = float(settings.llm.probe_timeout) * 2
        async with httpx.AsyncClient(
            trust_env=settings.llm.trust_env, timeout=httpx.Timeout(timeout)
        ) as client:
            resp = await client.post(
                f"{base}/chat/completions",
                headers=headers,
                json={
                    "model": model_id,
                    "messages": messages,
                    # 核验只要确认端点接受这个 content 结构，
                    # 不需要长回答。限死 32 token 省钱且快。
                    "max_tokens": 32,
                    "stream": False,
                },
            )
        if resp.status_code >= 400:
            # 把状态码和响应体一起带出去，让上层能分类
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:400]}")

        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            # 某些模型回数组形式，取其中的 text 片段
            return " ".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            ).strip()
        return str(content or "").strip()

    async def complete_chat(
        self, model: ResolvedModel, messages: list[dict[str, Any]]
    ) -> str:
        """
        非流式完整回复。用于视觉识别等一次性调用（非真实对话）。

        与 probe_chat 的区别：收 ResolvedModel、不限制 max_tokens。
        识别图片需要完整描述，不是探测用的一句话。
        """
        base = normalize_base_url(model.base_url)
        headers = {
            "Authorization": f"Bearer {model.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(
            trust_env=settings.llm.trust_env,
            timeout=httpx.Timeout(float(settings.llm.request_timeout)),
        ) as client:
            resp = await client.post(
                f"{base}/chat/completions",
                headers=headers,
                json={
                    "model": model.model_id,
                    "messages": messages,
                    "stream": False,
                },
            )
        if resp.status_code >= 400:
            raise ProviderError(
                f"视觉模型返回 {resp.status_code}", hint=resp.text[:400]
            )
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            return " ".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            ).strip()
        return str(content or "").strip()


_llm: OpenAICompatLLM | None = None


def get_llm() -> OpenAICompatLLM:
    global _llm
    if _llm is None:
        _llm = OpenAICompatLLM()
    return _llm


async def close_llm() -> None:
    global _llm
    if _llm is not None:
        await _llm.aclose()
        _llm = None
