"""
OpenAI 兼容的嵌入接口。

## 为什么单独一个模块而不是塞进 openai_compat

嵌入与对话的失败语义不同：

- 对话是流式的，失败要能把已产出的部分交给用户
- 嵌入是批量的，一批里有一条失败就整批不可用（维度对不齐没法存）

而且嵌入要处理"批大小"这个对话没有的概念 —— 一次请求塞多少条文本
取决于供应商限制，超了会 400 而不是截断。

## 为什么不用 sentence-transformers 本地兜底

原设计（.env.verify 的注释）提到过。放弃的理由：
它会拉进 torch（~2GB），而这个项目的定位是个人可自部署的轻量工具。
用户没配嵌入模型时的正确行为是【关闭向量召回】、回落关键词搜索，
而不是偷偷装 2GB 依赖。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import httpx
import structlog

from app.core.config import settings
from app.core.exceptions import ProviderError
from app.infra.llm.openai_compat import normalize_base_url
from app.infra.llm.port import ResolvedModel

log = structlog.get_logger(__name__)

# 单次请求最多塞多少条文本。
#
# 32 是保守值。供应商的限制差异很大（OpenAI 2048、部分自建 16），
# 超限的表现是 400 而不是自动截断，所以宁可多发几次请求。
DEFAULT_BATCH_SIZE = 32

# 单条文本的【字节】上限。
#
# 按字节而非字符：供应商的限制是 token 或字节，中文一个字符 3 字节。
# 见 _prepare 的说明。
#
# 24000 字节 ≈ 8000 个中文字符 ≈ 6000 token，在常见嵌入模型的
# 8192 token 上限之内。截断而非报错：一条超长记忆的向量【略有偏差】
# 远好于它完全不可召回。
MAX_TEXT_BYTES = 24_000


@dataclass
class EmbeddingResult:
    """一批文本的向量。vectors 与输入 texts 一一对应。"""

    vectors: list[list[float]]
    model: str
    dim: int

    @property
    def ok(self) -> bool:
        return bool(self.vectors) and self.dim > 0


class OpenAICompatEmbedding:
    """OpenAI 兼容的 /embeddings 客户端。"""

    async def embed(
        self,
        model: ResolvedModel,
        texts: list[str],
        *,
        batch_size: int | None = None,
    ) -> EmbeddingResult:
        """
        批量向量化。返回的向量顺序与输入严格一致。

        ## 为什么必须保证顺序

        调用方靠下标把向量对应回 uri。乱序会让向量【存到错误的记忆上】——
        那是最难发现的 bug：召回照常返回结果，只是结果完全无关。

        供应商的响应里有 index 字段，所以按它排序而不是依赖返回顺序。
        """
        if not texts:
            return EmbeddingResult(vectors=[], model=model.model_id, dim=0)

        size = batch_size or settings.memory.embedding_batch_size or DEFAULT_BATCH_SIZE
        prepared = [_prepare(t) for t in texts]
        out: list[list[float]] = []

        for start in range(0, len(prepared), size):
            chunk = prepared[start : start + size]
            out.extend(await self._embed_batch(model, chunk))

        dims = {len(v) for v in out if v}
        if len(dims) > 1:
            # 同一次调用里维度不一致 —— 供应商有问题，不能存。
            raise ProviderError(f"嵌入维度不一致：{sorted(dims)}", code="embedding_dim_mismatch")

        return EmbeddingResult(
            vectors=out, model=model.model_id, dim=next(iter(dims)) if dims else 0
        )

    async def _embed_batch(self, model: ResolvedModel, texts: list[str]) -> list[list[float]]:
        url = f"{normalize_base_url(model.base_url)}/embeddings"
        headers = {"Content-Type": "application/json"}
        if model.api_key.strip():
            headers["Authorization"] = f"Bearer {model.api_key}"

        body = {"model": model.model_id, "input": texts}

        async with httpx.AsyncClient(
            trust_env=settings.llm.trust_env,
            timeout=httpx.Timeout(float(settings.llm.request_timeout)),
        ) as client:
            try:
                resp = await client.post(url, headers=headers, json=body)
            except httpx.ConnectError as e:
                raise ProviderError(f"嵌入服务不可达：{e}", code="embedding_unreachable") from e
            except httpx.TimeoutException as e:
                raise ProviderError("嵌入请求超时", code="embedding_timeout") from e

        if resp.status_code >= 400:
            raise ProviderError(
                f"嵌入请求失败（{resp.status_code}）：{resp.text[:300]}",
                code="embedding_http_error",
            )

        try:
            payload = resp.json()
        except ValueError as e:
            raise ProviderError("嵌入响应不是合法 JSON", code="embedding_bad_json") from e

        data = payload.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise ProviderError(
                f"嵌入响应条数不匹配：请求 {len(texts)} 条，返回 {len(data or [])} 条",
                code="embedding_count_mismatch",
            )

        # 按 index 排序 —— 不依赖返回顺序。见 embed 的说明。
        ordered = sorted(data, key=lambda d: int(d.get("index", 0)) if isinstance(d, dict) else 0)
        vectors: list[list[float]] = []
        for item in ordered:
            raw = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(raw, list) or not raw:
                raise ProviderError("嵌入响应缺少 embedding 字段", code="embedding_missing")
            vectors.append([float(x) for x in raw])
        return vectors


def _prepare(text: str) -> str:
    """
    截断并保证非空。

    空字符串会被部分供应商拒绝（400），而"这条记忆正文恰好是空的"
    不该让整批失败。用一个占位符让它拿到一个无意义但合法的向量 ——
    反正空记忆本来就不该被召回。

    ## 为什么按【字节】而不是字符截断

    供应商的限制是 token 或字节，而中文一个字符是 3 字节。
    按字符截断时 8000 个中文字符 = 24000 字节，仍然可能超限 ——
    那时报 400，而错误信息只说"input too long"，看不出是哪条记忆。

    OpenViking 同样按字节截（_truncate_memory_abstract，
    memory_updater.py:1458，上限 50000 字节）。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return "(empty)"

    cap = settings.memory.embedding_max_bytes or MAX_TEXT_BYTES
    encoded = cleaned.encode("utf-8")
    if len(encoded) <= cap:
        return cleaned
    # errors="ignore" 丢掉末尾被切断的半个字符，而不是抛 UnicodeDecodeError
    return encoded[:cap].decode("utf-8", errors="ignore")


def cosine(a: list[float], b: list[float]) -> float:
    """
    余弦相似度。维度不等返回 0（视为不相关）而不是报错。

    维度不等意味着两个向量来自不同的嵌入模型 —— 那时任何比较都无意义，
    返回 0 让它自然排到末尾。上层靠 embedding_model 字段发现并触发重算，
    这里只保证不崩。
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


_client = OpenAICompatEmbedding()


async def embed_texts(model: ResolvedModel, texts: list[str]) -> EmbeddingResult:
    return await _client.embed(model, texts)


async def embed_one(model: ResolvedModel, text: str) -> list[float]:
    result = await _client.embed(model, [text])
    return result.vectors[0] if result.vectors else []


async def probe_dim(model: ResolvedModel) -> int:
    """
    探测模型的向量维度。用于配置页显示与切换模型时的兼容判断。

    发一条最短的文本 —— 维度是模型固有属性，与输入长度无关。
    """
    try:
        return (await _client.embed(model, ["dim probe"])).dim
    except (TimeoutError, ProviderError) as e:
        log.warning("embedding_probe_failed", model=model.model_id, error=str(e))
        return 0
