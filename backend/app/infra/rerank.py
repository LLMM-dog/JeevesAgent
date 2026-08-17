"""
Rerank 重排序模块：使用专门的 rerank 模型对检索结果重新打分。

## 为什么需要 Rerank

向量搜索（embedding）是**粗筛**：
- 基于语义相似度，快速找到候选集
- 但 embedding 模型通常是通用的，不针对特定任务优化
- 可能会漏掉一些细节相关性

Rerank 是**精筛**：
- 使用专门训练的 rerank 模型
- 输入：查询 + 文档，输出：相关度分数
- 能捕捉更细粒度的相关性（词序、逻辑关系等）

## 工作流程

```
用户查询 "Python 异步编程最佳实践"
    ↓
向量搜索（embedding）→ 100 个候选
    ↓
Rerank 重排序 → 按真实相关度排序
    ↓
Top 10 返回给用户
```

## 支持的提供商

1. **Cohere** - rerank-v3.5（推荐）
   - 最先进的 rerank 模型
   - 支持多语言
   - API: https://api.cohere.com

2. **Jina AI** - jina-reranker-v2
   - 开源友好
   - 性能优秀
   - API: https://api.jina.ai

3. **Voyage AI** - rerank-1
   - 专注于检索任务
   - API: https://api.voyageai.com

## 参考 OpenViking

OpenViking 的实现在：
- `openviking/models/rerank/base.py` - 基类
- `openviking/models/rerank/cohere_rerank.py` - Cohere 实现
- `openviking/retrieve/hierarchical_retriever.py` - 使用示例
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

import httpx
import structlog

log = structlog.get_logger(__name__)


class RerankProvider(ABC):
    """
    Rerank 提供商的抽象基类。

    所有 rerank 实现都必须实现 `rerank` 方法。
    """

    def __init__(self, provider_name: str):
        self.provider_name = provider_name

    @abstractmethod
    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int | None = None,
    ) -> list[float]:
        """
        对文档列表进行重排序。

        Args:
            query: 查询文本
            documents: 文档列表（已经是候选集）
            top_k: 返回前 k 个结果（None = 返回所有）

        Returns:
            分数列表，与 documents 一一对应
            分数范围：[0, 1]，越高越相关
        """
        pass

    async def close(self) -> None:
        """关闭客户端（如果需要）"""
        # 基类默认不需要清理资源，子类可以覆盖
        return None


class CohereRerankProvider(RerankProvider):
    """Cohere Rerank API 提供商。"""

    def __init__(
        self,
        api_key: str,
        model: str = "rerank-v3.5",
        api_base: str = "https://api.cohere.com",
        timeout: float = 30.0,
    ):
        super().__init__("cohere")
        self.api_key = api_key
        self.model = model
        self.api_base = api_base.rstrip("/")
        self.client = httpx.AsyncClient(timeout=timeout)

        log.info(
            "cohere_rerank_initialized",
            model=model,
            api_base=api_base,
        )

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int | None = None,
    ) -> list[float]:
        """
        调用 Cohere Rerank API。

        API 文档: https://docs.cohere.com/reference/rerank
        """
        if not documents:
            return []

        start_time = time.time()

        try:
            response = await self.client.post(
                f"{self.api_base}/v2/rerank",
                json={
                    "model": self.model,
                    "query": query,
                    "documents": documents,
                    "top_n": top_k or len(documents),
                    "return_documents": False,  # 只要分数，不要文档内容
                },
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )

            response.raise_for_status()
            data = response.json()

            duration = time.time() - start_time

            # Cohere 返回的是排序后的结果，需要还原到原始顺序
            # 格式: {"results": [{"index": 2, "relevance_score": 0.95}, ...]}
            results = data.get("results", [])

            # 初始化所有分数为 0
            scores = [0.0] * len(documents)

            # 填充返回的分数
            for result in results:
                index = result.get("index")
                score = result.get("relevance_score", 0.0)
                if 0 <= index < len(documents):
                    scores[index] = float(score)

            log.info(
                "cohere_rerank_success",
                documents=len(documents),
                duration=f"{duration:.2f}s",
                avg_score=f"{sum(scores) / len(scores):.3f}",
            )

            return scores

        except httpx.HTTPStatusError as e:
            log.error(
                "cohere_rerank_http_error",
                status=e.response.status_code,
                error=e.response.text[:200],
            )
            # 返回原始分数（全 0，表示失败）
            return [0.0] * len(documents)

        except Exception as e:
            log.error(
                "cohere_rerank_error",
                error=str(e),
            )
            return [0.0] * len(documents)

    async def close(self) -> None:
        await self.client.aclose()


class JinaRerankProvider(RerankProvider):
    """Jina AI Rerank API 提供商。"""

    def __init__(
        self,
        api_key: str,
        model: str = "jina-reranker-v2-base-multilingual",
        api_base: str = "https://api.jina.ai",
        timeout: float = 30.0,
    ):
        super().__init__("jina")
        self.api_key = api_key
        self.model = model
        self.api_base = api_base.rstrip("/")
        self.client = httpx.AsyncClient(timeout=timeout)

        log.info(
            "jina_rerank_initialized",
            model=model,
            api_base=api_base,
        )

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int | None = None,
    ) -> list[float]:
        """
        调用 Jina Rerank API。

        API 文档: https://jina.ai/reranker
        """
        if not documents:
            return []

        start_time = time.time()

        try:
            response = await self.client.post(
                f"{self.api_base}/v1/rerank",
                json={
                    "model": self.model,
                    "query": query,
                    "documents": documents,
                    "top_n": top_k or len(documents),
                },
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )

            response.raise_for_status()
            data = response.json()

            duration = time.time() - start_time

            # Jina 返回格式: {"results": [{"index": 0, "relevance_score": 0.95}, ...]}
            results = data.get("results", [])

            scores = [0.0] * len(documents)

            for result in results:
                index = result.get("index")
                score = result.get("relevance_score", 0.0)
                if 0 <= index < len(documents):
                    scores[index] = float(score)

            log.info(
                "jina_rerank_success",
                documents=len(documents),
                duration=f"{duration:.2f}s",
                avg_score=f"{sum(scores) / len(scores):.3f}",
            )

            return scores

        except httpx.HTTPStatusError as e:
            log.error(
                "jina_rerank_http_error",
                status=e.response.status_code,
                error=e.response.text[:200],
            )
            return [0.0] * len(documents)

        except Exception as e:
            log.error(
                "jina_rerank_error",
                error=str(e),
            )
            return [0.0] * len(documents)

    async def close(self) -> None:
        await self.client.aclose()


class VoyageRerankProvider(RerankProvider):
    """Voyage AI Rerank API 提供商。"""

    def __init__(
        self,
        api_key: str,
        model: str = "rerank-1",
        api_base: str = "https://api.voyageai.com",
        timeout: float = 30.0,
    ):
        super().__init__("voyage")
        self.api_key = api_key
        self.model = model
        self.api_base = api_base.rstrip("/")
        self.client = httpx.AsyncClient(timeout=timeout)

        log.info(
            "voyage_rerank_initialized",
            model=model,
            api_base=api_base,
        )

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int | None = None,
    ) -> list[float]:
        """
        调用 Voyage Rerank API。

        API 文档: https://docs.voyageai.com/docs/reranker
        """
        if not documents:
            return []

        start_time = time.time()

        try:
            response = await self.client.post(
                f"{self.api_base}/v1/rerank",
                json={
                    "model": self.model,
                    "query": query,
                    "documents": documents,
                    "top_k": top_k or len(documents),
                },
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )

            response.raise_for_status()
            data = response.json()

            duration = time.time() - start_time

            # Voyage 返回格式: {"data": [{"index": 0, "relevance_score": 0.95}, ...]}
            results = data.get("data", [])

            scores = [0.0] * len(documents)

            for result in results:
                index = result.get("index")
                score = result.get("relevance_score", 0.0)
                if 0 <= index < len(documents):
                    scores[index] = float(score)

            log.info(
                "voyage_rerank_success",
                documents=len(documents),
                duration=f"{duration:.2f}s",
                avg_score=f"{sum(scores) / len(scores):.3f}",
            )

            return scores

        except httpx.HTTPStatusError as e:
            log.error(
                "voyage_rerank_http_error",
                status=e.response.status_code,
                error=e.response.text[:200],
            )
            return [0.0] * len(documents)

        except Exception as e:
            log.error(
                "voyage_rerank_error",
                error=str(e),
            )
            return [0.0] * len(documents)

    async def close(self) -> None:
        await self.client.aclose()


def _detect_rerank_provider(api_base: str) -> str | None:
    """
    根据 API base URL 自动检测 rerank 提供商。

    Args:
        api_base: API 基础 URL

    Returns:
        提供商名称 ("cohere", "jina", "voyage")，或 None（无法识别）
    """
    base_lower = api_base.lower()
    if "cohere" in base_lower:
        return "cohere"
    elif "jina" in base_lower:
        return "jina"
    elif "voyage" in base_lower:
        return "voyage"
    return None


def create_rerank_provider(
    provider: str,
    api_key: str,
    model: str | None = None,
    api_base: str | None = None,
) -> RerankProvider | None:
    """
    工厂函数：根据配置创建 rerank 提供商。

    Args:
        provider: 提供商名称 ("cohere", "jina", "voyage", "auto")
        api_key: API 密钥
        model: 模型名称（可选，使用默认值）
        api_base: API 基础 URL（可选，使用默认值）

    Returns:
        RerankProvider 实例，或 None（如果提供商不支持）
    """
    provider_lower = provider.lower()

    # 如果 provider="auto"，尝试从 api_base 自动检测
    if provider_lower == "auto" and api_base:
        detected = _detect_rerank_provider(api_base)
        if detected:
            provider_lower = detected
            log.info("rerank_provider_auto_detected", provider=detected, api_base=api_base)
        else:
            log.warning("rerank_provider_auto_detect_failed", api_base=api_base)
            return None

    if provider_lower == "cohere":
        return CohereRerankProvider(
            api_key=api_key,
            model=model or "rerank-v3.5",
            api_base=api_base or "https://api.cohere.com",
        )

    elif provider_lower == "jina":
        return JinaRerankProvider(
            api_key=api_key,
            model=model or "jina-reranker-v2-base-multilingual",
            api_base=api_base or "https://api.jina.ai",
        )

    elif provider_lower == "voyage":
        return VoyageRerankProvider(
            api_key=api_key,
            model=model or "rerank-1",
            api_base=api_base or "https://api.voyageai.com",
        )

    else:
        log.warning(
            "unsupported_rerank_provider",
            provider=provider_lower,
            supported=["cohere", "jina", "voyage"],
        )
        return None
