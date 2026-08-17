"""
BM25 稀疏向量搜索：关键词精确匹配。

## 为什么需要 BM25

向量搜索（密集向量）的局限：
- 基于语义相似度，可能忽略精确关键词匹配
- 例如：查询 "Python 3.11 新特性"，密集向量可能返回 "Python 教程"
  但我们更想要包含 "3.11" 的记忆

BM25（稀疏向量）的优势：
- 基于关键词频率和文档频率
- 精确匹配：查询中的 "3.11" 必须在文档中出现
- 对专有名词、版本号、代码片段等效果好

## BM25 算法简介

BM25 (Best Matching 25) 是经典的信息检索算法，公式：

```
score(D, Q) = Σ IDF(qi) * (f(qi, D) * (k1 + 1)) / (f(qi, D) + k1 * (1 - b + b * |D| / avgdl))
```

其中：
- D: 文档
- Q: 查询
- qi: 查询中的第 i 个词
- f(qi, D): qi 在 D 中的频率
- IDF(qi): 逆文档频率（qi 的稀有程度）
- |D|: 文档长度
- avgdl: 平均文档长度
- k1, b: 调优参数

## 实现策略

因为 Jeeves 使用 SQLite（不是向量数据库），我们需要：
1. 构建 BM25 索引（倒排索引）
2. 在内存中计算 BM25 分数
3. 混合密集向量分数和 BM25 分数

## 参考 OpenViking

OpenViking 依赖 VikingDB 的原生稀疏向量支持，直接传递 sparse_query_vector。
Jeeves 需要自己实现 BM25，但混合策略类似。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)


@dataclass
class BM25Config:
    """BM25 算法配置参数。"""

    k1: float = 1.5
    """
    词频饱和参数。

    - k1 = 0: 忽略词频（只看是否出现）
    - k1 = 1.2-2.0: 平衡词频的影响（推荐范围）
    - k1 = ∞: 词频线性增长

    OpenSearch 默认 1.2，我们用 1.5（稍微更重视词频）。
    """

    b: float = 0.75
    """
    文档长度归一化参数。

    - b = 0: 完全忽略文档长度
    - b = 1: 完全按长度归一化（长文档惩罚更重）
    - b = 0.75: 平衡（标准值）

    防止长文档仅因为包含更多词就得高分。
    """

    min_token_length: int = 2
    """最短 token 长度（过滤噪音）。"""

    max_token_length: int = 50
    """最长 token 长度（防止异常长字符串）。"""


class BM25Index:
    """
    BM25 索引：用于快速计算 BM25 分数。

    内存索引，支持增量更新。
    """

    def __init__(self, config: BM25Config | None = None):
        self.config = config or BM25Config()

        # 文档 ID -> token 频率
        self.doc_freqs: dict[str, Counter[str]] = {}

        # 文档 ID -> 文档长度（token 数量）
        self.doc_lengths: dict[str, int] = {}

        # token -> 包含该 token 的文档数
        self.doc_counts: Counter[str] = Counter()

        # 总文档数
        self.num_docs: int = 0

        # 平均文档长度
        self.avg_doc_length: float = 0.0

    def add_document(self, doc_id: str, text: str):
        """
        添加或更新文档到索引。

        Args:
            doc_id: 文档唯一标识（如 URI）
            text: 文档文本（标题 + 正文）
        """
        # 如果文档已存在，先删除
        if doc_id in self.doc_freqs:
            self.remove_document(doc_id)

        # 分词
        tokens = self._tokenize(text)

        # 计算词频
        token_freqs = Counter(tokens)

        # 更新索引
        self.doc_freqs[doc_id] = token_freqs
        self.doc_lengths[doc_id] = len(tokens)

        # 更新文档计数
        for token in token_freqs:
            self.doc_counts[token] += 1

        # 更新总文档数和平均长度
        self.num_docs += 1
        self._update_avg_length()

        log.debug(
            "bm25_document_added",
            doc_id=doc_id,
            tokens=len(tokens),
            unique_tokens=len(token_freqs),
        )

    def remove_document(self, doc_id: str):
        """从索引中删除文档。"""
        if doc_id not in self.doc_freqs:
            return

        # 更新文档计数
        for token in self.doc_freqs[doc_id]:
            self.doc_counts[token] -= 1
            if self.doc_counts[token] <= 0:
                del self.doc_counts[token]

        # 删除文档数据
        del self.doc_freqs[doc_id]
        del self.doc_lengths[doc_id]

        # 更新总文档数和平均长度
        self.num_docs -= 1
        self._update_avg_length()

        log.debug("bm25_document_removed", doc_id=doc_id)

    def search(self, query: str, doc_ids: list[str] | None = None) -> dict[str, float]:
        """
        搜索文档并返回 BM25 分数。

        Args:
            query: 查询文本
            doc_ids: 限制搜索的文档 ID 列表（None = 搜索所有文档）

        Returns:
            文档 ID -> BM25 分数的字典
        """
        # 分词
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return {}

        # 限制搜索范围
        if doc_ids is None:
            doc_ids = list(self.doc_freqs.keys())

        # 计算每个文档的 BM25 分数
        scores: dict[str, float] = {}

        for doc_id in doc_ids:
            if doc_id not in self.doc_freqs:
                continue

            score = self._calculate_bm25(query_tokens, doc_id)
            if score > 0:
                scores[doc_id] = score

        log.debug(
            "bm25_search_completed",
            query_tokens=len(query_tokens),
            candidates=len(doc_ids),
            hits=len(scores),
        )

        return scores

    def _calculate_bm25(self, query_tokens: list[str], doc_id: str) -> float:
        """计算单个文档的 BM25 分数。"""
        doc_freqs = self.doc_freqs[doc_id]
        doc_length = self.doc_lengths[doc_id]

        score = 0.0

        for token in query_tokens:
            if token not in doc_freqs:
                continue

            # 词频（在文档中出现的次数）
            freq = doc_freqs[token]

            # IDF（逆文档频率）
            idf = self._calculate_idf(token)

            # BM25 公式
            numerator = freq * (self.config.k1 + 1)
            denominator = freq + self.config.k1 * (
                1 - self.config.b + self.config.b * doc_length / self.avg_doc_length
            )

            score += idf * (numerator / denominator)

        return score

    def _calculate_idf(self, token: str) -> float:
        """
        计算 IDF（逆文档频率）。

        IDF = log((N - df + 0.5) / (df + 0.5) + 1)

        其中：
        - N: 总文档数
        - df: 包含该 token 的文档数

        +0.5 和 +1 是平滑项，防止除零和负数。
        """
        doc_count = self.doc_counts.get(token, 0)

        # 平滑的 IDF 公式（避免负数）
        idf = math.log(
            (self.num_docs - doc_count + 0.5) / (doc_count + 0.5) + 1.0
        )

        return max(0.0, idf)  # 确保非负

    def _tokenize(self, text: str) -> list[str]:
        """
        分词：将文本切分为 token 列表。

        简化版分词器：
        1. 转小写
        2. 用正则提取字母数字序列
        3. 过滤过短和过长的 token
        """
        # 转小写
        text = text.lower()

        # 提取字母数字序列（包括下划线）
        # 例如：Python3.11 -> ["python3", "11"]
        tokens = re.findall(r'\b\w+\b', text)

        # 过滤长度
        tokens = [
            t for t in tokens
            if self.config.min_token_length <= len(t) <= self.config.max_token_length
        ]

        return tokens

    def _update_avg_length(self):
        """更新平均文档长度。"""
        if self.num_docs == 0:
            self.avg_doc_length = 0.0
        else:
            total_length = sum(self.doc_lengths.values())
            self.avg_doc_length = total_length / self.num_docs


def normalize_scores(scores: dict[str, float]) -> dict[str, float]:
    """
    归一化分数到 [0, 1] 区间。

    使用 min-max 归一化：
    normalized = (score - min_score) / (max_score - min_score)

    Args:
        scores: 原始分数字典

    Returns:
        归一化后的分数字典
    """
    if not scores:
        return {}

    min_score = min(scores.values())
    max_score = max(scores.values())

    # 所有分数相同（或只有一个）
    if max_score == min_score:
        return {doc_id: 1.0 for doc_id in scores}

    # Min-max 归一化
    normalized = {
        doc_id: (score - min_score) / (max_score - min_score)
        for doc_id, score in scores.items()
    }

    return normalized
