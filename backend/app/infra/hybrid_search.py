"""
混合搜索：密集向量 + BM25 稀疏向量。

## 混合搜索策略

密集向量（语义搜索）:
- 优点：理解语义相似度，处理同义词、概念
- 缺点：可能忽略精确关键词

BM25（关键词搜索）:
- 优点：精确匹配，对专有名词、版本号、代码片段好
- 缺点：不理解语义，同义词无法匹配

混合搜索：
- 结合两者优势
- 根据查询类型自适应调整权重

## 权重策略

**默认权重**（平衡）:
- 密集向量：0.7
- BM25：0.3

**查询包含特殊词时**（关键词为主）:
- 版本号（3.11、v2.0）
- 代码片段（`func()`)
- 专有名词（全大写）
- 权重：密集 0.5 / BM25 0.5

**纯语义查询时**（语义为主）:
- 问句（如何、为什么）
- 抽象概念（最佳实践、原理）
- 权重：密集 0.8 / BM25 0.2

## 与 OpenViking 的对齐

OpenViking 依赖 VikingDB 的原生混合搜索：
```python
await index.search(
    query_vector=dense_vector,
    sparse_query_vector=sparse_vector,
    # VikingDB 内部处理混合
)
```

Jeeves 使用 SQLite，需要自己混合：
```python
dense_scores = vectorize.search(...)
sparse_scores = bm25_index.search(...)
mixed_scores = hybrid_search.mix(dense_scores, sparse_scores)
```

## 参考资料

- OpenViking: openviking/retrieve/hierarchical_retriever.py:149-163
- Elasticsearch Hybrid Search: https://www.elastic.co/guide/en/elasticsearch/reference/current/rrf.html
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)


@dataclass
class HybridSearchConfig:
    """混合搜索配置。"""

    # 默认权重
    default_dense_weight: float = 0.7
    default_sparse_weight: float = 0.3

    # 关键词查询权重（检测到精确匹配需求）
    keyword_dense_weight: float = 0.5
    keyword_sparse_weight: float = 0.5

    # 语义查询权重（检测到纯语义查询）
    semantic_dense_weight: float = 0.8
    semantic_sparse_weight: float = 0.2


class QueryAnalyzer:
    """
    查询分析器：分析查询类型，决定混合权重。

    对齐 OpenViking 的智能查询理解。
    """

    # 版本号模式：1.0, v2.3.4, 3.11 等
    VERSION_PATTERN = re.compile(r'\b\d+\.\d+(\.\d+)?\b|v\d+(\.\d+)*\b', re.IGNORECASE)

    # 代码片段：`code`, function(), Class.method
    CODE_PATTERN = re.compile(r'`[^`]+`|\w+\(\)|[A-Z]\w*\.\w+')

    # 专有名词：连续大写字母（缩写）
    ACRONYM_PATTERN = re.compile(r'\b[A-Z]{2,}\b')

    # 语义问句：如何、为什么、什么是
    SEMANTIC_PATTERNS = [
        re.compile(r'\b(how|why|what|when|where|which)\b', re.IGNORECASE),
        re.compile(r'(如何|为什么|什么是|怎么|为啥|怎样)'),
        re.compile(r'\b(best practice|principle|concept|understand|原理|概念|理解|最佳实践)\b', re.IGNORECASE),
    ]

    @staticmethod
    def analyze(query: str) -> str:
        """
        分析查询类型。

        Returns:
            "keyword" - 关键词查询（精确匹配为主）
            "semantic" - 语义查询（概念理解为主）
            "balanced" - 平衡查询（默认）
        """
        # 检查版本号
        if QueryAnalyzer.VERSION_PATTERN.search(query):
            log.debug("query_analysis", type="keyword", reason="version_number")
            return "keyword"

        # 检查代码片段
        if QueryAnalyzer.CODE_PATTERN.search(query):
            log.debug("query_analysis", type="keyword", reason="code_snippet")
            return "keyword"

        # 检查缩写（如 API, SQL, HTTP）
        if QueryAnalyzer.ACRONYM_PATTERN.search(query):
            log.debug("query_analysis", type="keyword", reason="acronym")
            return "keyword"

        # 检查语义问句
        for pattern in QueryAnalyzer.SEMANTIC_PATTERNS:
            if pattern.search(query):
                log.debug("query_analysis", type="semantic", reason="question")
                return "semantic"

        # 默认平衡
        log.debug("query_analysis", type="balanced", reason="default")
        return "balanced"


def mix_scores(
    dense_scores: dict[str, float],
    sparse_scores: dict[str, float],
    dense_weight: float,
    sparse_weight: float,
) -> dict[str, float]:
    """
    混合密集向量分数和 BM25 分数。

    公式：
    mixed_score = dense_weight * dense_score + sparse_weight * sparse_score

    Args:
        dense_scores: 密集向量分数 (URI -> score)
        sparse_scores: BM25 分数 (URI -> score)
        dense_weight: 密集向量权重
        sparse_weight: BM25 权重

    Returns:
        混合分数 (URI -> score)
    """
    # 合并所有文档 URI
    all_uris = set(dense_scores.keys()) | set(sparse_scores.keys())

    mixed: dict[str, float] = {}

    for uri in all_uris:
        dense_score = dense_scores.get(uri, 0.0)
        sparse_score = sparse_scores.get(uri, 0.0)

        # 混合分数
        mixed_score = dense_weight * dense_score + sparse_weight * sparse_score

        mixed[uri] = mixed_score

    return mixed


async def hybrid_search(
    query: str,
    dense_scores: dict[str, float],
    sparse_scores: dict[str, float],
    config: HybridSearchConfig | None = None,
) -> dict[str, float]:
    """
    执行混合搜索：分析查询类型，智能混合分数。

    Args:
        query: 查询文本
        dense_scores: 密集向量分数
        sparse_scores: BM25 分数
        config: 混合搜索配置

    Returns:
        混合后的分数字典
    """
    config = config or HybridSearchConfig()

    # 分析查询类型
    query_type = QueryAnalyzer.analyze(query)

    # 选择权重
    if query_type == "keyword":
        dense_weight = config.keyword_dense_weight
        sparse_weight = config.keyword_sparse_weight
    elif query_type == "semantic":
        dense_weight = config.semantic_dense_weight
        sparse_weight = config.semantic_sparse_weight
    else:  # balanced
        dense_weight = config.default_dense_weight
        sparse_weight = config.default_sparse_weight

    # 混合分数
    mixed = mix_scores(dense_scores, sparse_scores, dense_weight, sparse_weight)

    log.info(
        "hybrid_search_completed",
        query_type=query_type,
        dense_weight=dense_weight,
        sparse_weight=sparse_weight,
        dense_hits=len(dense_scores),
        sparse_hits=len(sparse_scores),
        mixed_hits=len(mixed),
    )

    return mixed


async def adaptive_hybrid_search(
    query: str,
    dense_scores: dict[str, float],
    sparse_scores: dict[str, float],
) -> dict[str, float]:
    """
    自适应混合搜索：根据结果分布动态调整权重。

    策略：
    - 如果密集向量和 BM25 结果重叠多 → 增加密集向量权重（两者一致）
    - 如果重叠少 → 平衡权重（互补）

    Args:
        query: 查询文本
        dense_scores: 密集向量分数
        sparse_scores: BM25 分数

    Returns:
        自适应混合后的分数字典
    """
    # 计算重叠率
    dense_uris = set(dense_scores.keys())
    sparse_uris = set(sparse_scores.keys())

    if not dense_uris or not sparse_uris:
        # 只有一种搜索有结果，直接返回
        return {**dense_scores, **sparse_scores}

    intersection = dense_uris & sparse_uris
    union = dense_uris | sparse_uris

    overlap_ratio = len(intersection) / len(union) if union else 0

    # 根据重叠率调整权重
    if overlap_ratio > 0.7:
        # 高重叠：两者一致，更信任密集向量（语义理解更好）
        dense_weight = 0.8
        sparse_weight = 0.2
    elif overlap_ratio < 0.3:
        # 低重叠：互补，平衡权重
        dense_weight = 0.5
        sparse_weight = 0.5
    else:
        # 中等重叠：默认权重
        dense_weight = 0.7
        sparse_weight = 0.3

    mixed = mix_scores(dense_scores, sparse_scores, dense_weight, sparse_weight)

    log.info(
        "adaptive_hybrid_search_completed",
        overlap_ratio=f"{overlap_ratio:.2f}",
        dense_weight=dense_weight,
        sparse_weight=sparse_weight,
        mixed_hits=len(mixed),
    )

    return mixed
