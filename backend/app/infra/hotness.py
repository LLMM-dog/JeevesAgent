"""
热度评分模块：基于访问频率和时间衰减的记忆热度计算。

## 为什么需要热度评分

纯语义搜索的局限：
- 只看相似度，不考虑记忆的实际价值
- 一个"被多次召回"的记忆可能更重要
- 最近更新的记忆通常更相关

热度评分的优势：
- 频率分量：访问次数越多，热度越高
- 时间衰减：越久未访问，热度越低
- 混合策略：热度 + 语义相似度

## OpenViking 的热度公式

```python
hotness_score = freq * recency

freq = 1 / (1 + exp(-log1p(active_count)))
recency = exp(-decay_rate * age_days)
decay_rate = log(2) / half_life_days
```

其中：
- `active_count`: 访问次数（每次召回命中 +1）
- `age_days`: 距离上次更新的天数
- `half_life_days`: 半衰期（默认 7 天）

## 参考资料

- OpenViking: openviking/retrieve/memory_lifecycle.py
- 指数衰减: https://en.wikipedia.org/wiki/Exponential_decay
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import structlog

log = structlog.get_logger(__name__)

# 默认半衰期（天）
DEFAULT_HALF_LIFE_DAYS: float = 7.0


def hotness_score(
    active_count: int,
    updated_at: datetime | None,
    now: datetime | None = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> float:
    """
    计算记忆的热度分数（0.0 - 1.0）。

    热度 = 频率分量 * 时间衰减分量

    频率分量：基于访问次数的 sigmoid 变换
    - active_count = 0  → freq ≈ 0.5
    - active_count = 10 → freq ≈ 0.996
    - active_count = 100 → freq ≈ 1.0

    时间衰减：指数衰减
    - age = 0 天    → recency = 1.0
    - age = 7 天    → recency = 0.5  (半衰期)
    - age = 14 天   → recency = 0.25
    - age = 30 天   → recency ≈ 0.06

    对齐 OpenViking 的实现：
    - openviking/retrieve/memory_lifecycle.py:19-64

    Args:
        active_count: 访问次数（召回命中次数）
        updated_at: 上次更新时间（preferably UTC）
        now: 当前时间（用于测试，默认使用当前 UTC 时间）
        half_life_days: 半衰期（天），默认 7 天

    Returns:
        热度分数，范围 [0.0, 1.0]

    Examples:
        >>> from datetime import datetime, timedelta
        >>> now = datetime(2026, 8, 15, 12, 0, 0)
        >>> updated = datetime(2026, 8, 8, 12, 0, 0)  # 7 天前
        >>> hotness_score(10, updated, now, half_life_days=7.0)
        0.498  # freq≈1.0 * recency=0.5
    """
    if now is None:
        now = datetime.now(UTC)

    # ── 频率分量 ──
    # 使用 sigmoid 变换：1 / (1 + exp(-log1p(active_count)))
    #
    # log1p(x) = log(1 + x)，避免 log(0) 未定义
    # exp(-log1p(x)) = 1 / (1 + x)
    # 所以：freq = 1 / (1 + 1/(1 + active_count)) = (1 + active_count) / (2 + active_count)
    #
    # 特性：
    # - active_count = 0  → freq = 0.5  (新记忆也有基础分)
    # - 随着访问增加，freq 逐渐趋近 1.0
    # - 增长速度递减（边际效应递减）
    freq = 1.0 / (1.0 + math.exp(-math.log1p(active_count)))

    # ── 时间衰减分量 ──
    if updated_at is None:
        # 没有更新时间，返回 0（不应该发生）
        log.warning("hotness_score_no_updated_at")
        return 0.0

    # 确保时区感知（aware UTC）
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    # 计算年龄（天）
    age_days = max((now - updated_at).total_seconds() / 86400.0, 0.0)

    # 指数衰减：recency = exp(-decay_rate * age_days)
    # decay_rate = log(2) / half_life_days
    #
    # 推导：半衰期时 recency = 0.5
    # 0.5 = exp(-decay_rate * half_life_days)
    # log(0.5) = -decay_rate * half_life_days
    # -log(2) = -decay_rate * half_life_days
    # decay_rate = log(2) / half_life_days
    decay_rate = math.log(2) / half_life_days
    recency = math.exp(-decay_rate * age_days)

    # 热度 = 频率 * 时间衰减
    hotness = freq * recency

    return hotness


def blend_with_hotness(
    semantic_score: float,
    hotness: float,
    alpha: float,
) -> float:
    """
    混合语义分数和热度分数。

    公式：blended = (1 - alpha) * semantic + alpha * hotness

    Args:
        semantic_score: 语义相似度分数（向量搜索/rerank 的分数）
        hotness: 热度分数（由 hotness_score 计算）
        alpha: 热度权重（0.0 - 1.0）
            - alpha = 0.0: 纯语义搜索，忽略热度
            - alpha = 0.15: 推荐值（OpenViking 默认）
            - alpha = 1.0: 纯热度排序

    Returns:
        混合后的分数

    Examples:
        >>> blend_with_hotness(0.8, 0.6, 0.15)
        0.77  # (1-0.15)*0.8 + 0.15*0.6 = 0.68 + 0.09
    """
    return (1 - alpha) * semantic_score + alpha * hotness


def batch_hotness_scores(
    items: list[tuple[int, datetime | None]],
    now: datetime | None = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> list[float]:
    """
    批量计算热度分数（优化性能）。

    Args:
        items: (active_count, updated_at) 元组列表
        now: 当前时间
        half_life_days: 半衰期

    Returns:
        热度分数列表，与输入一一对应
    """
    if now is None:
        now = datetime.now(UTC)

    scores = []
    for active_count, updated_at in items:
        score = hotness_score(active_count, updated_at, now, half_life_days)
        scores.append(score)

    return scores


# ── 辅助函数：用于测试和分析 ──


def frequency_component(active_count: int) -> float:
    """单独计算频率分量（用于分析）。"""
    return 1.0 / (1.0 + math.exp(-math.log1p(active_count)))


def recency_component(
    updated_at: datetime,
    now: datetime | None = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> float:
    """单独计算时间衰减分量（用于分析）。"""
    if now is None:
        now = datetime.now(UTC)

    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    age_days = max((now - updated_at).total_seconds() / 86400.0, 0.0)
    decay_rate = math.log(2) / half_life_days
    return math.exp(-decay_rate * age_days)


def estimate_active_count_for_freq(target_freq: float) -> int:
    """
    反向计算：要达到目标频率分量，需要多少访问次数。

    用于分析：多少次访问才能让频率达到 0.9、0.95 等。

    Args:
        target_freq: 目标频率（0.5 - 1.0）

    Returns:
        所需的访问次数（近似值）
    """
    if target_freq <= 0.5:
        return 0

    # freq = 1 / (1 + exp(-log1p(active_count)))
    # 反推：active_count = exp(log(1/freq - 1)) - 1
    # 但 sigmoid 形式更简单：active_count ≈ (1/freq - 1)^(-1) - 1
    #
    # 简化近似：active_count ≈ freq / (1 - freq) - 1
    approx = target_freq / (1 - target_freq) - 1
    return max(0, int(approx))
