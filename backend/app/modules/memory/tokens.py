"""
中英文粗分词。供记忆召回/提取的关键词搜索使用。

参考原 service.py 的 _tokens / _STOPWORDS。
"""

from __future__ import annotations

import re

# 停用词。只挡最高频的 —— 停用词表越大越容易把
# 有效关键词误挡（"我要用 Go" 里的 "用" 挡掉没事，"Go" 不能挡）。
_STOPWORDS = frozenset(
    {
        "的", "了", "是", "在", "我", "你", "他", "她", "它", "们", "这", "那",
        "有", "和", "与", "就", "都", "而", "及", "或", "一个", "什么", "怎么",
        "the", "a", "an", "is", "are", "was", "were", "be", "to", "of", "and",
        "or", "in", "on", "at", "for", "with", "it", "this", "that", "i", "you",
    }
)


def tokenize(text: str) -> list[str]:
    """
    粗分词。中文按 2-gram，英文/数字按单词。

    不引入 jieba 这类分词库：召回是"缩小候选集"，不需要精确分词。
    2-gram 对中文足够 —— "塔罗牌重构" 会切出 "塔罗"/"罗牌"/"牌重"/"重构"，
    与记忆里的 "塔罗牌" 有重叠就能命中。
    """
    text = text.lower()
    out: list[str] = []
    # 英文单词、数字
    for w in re.findall(r"[a-z0-9_.+#-]{2,}", text):
        if w not in _STOPWORDS:
            out.append(w)
    # 中文 2-gram
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        for i in range(len(run) - 1):
            g = run[i : i + 2]
            if g not in _STOPWORDS:
                out.append(g)
    return out
