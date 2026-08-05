"""
长期记忆的 CRUD 与召回。

## 三条从常见实现缺陷里得来的规则

**1. 每次变更必须记 reason**（抄 ，`consumer.py:170,194,210`）

`update` / `delete` / `merge` 强制要求 reason 参数，写进 `history`。
排查"AI 为什么以为我喜欢 X"时这是唯一线索。

**2. 召回不能是 O(记忆总量)**（避开 缺陷）

它的 LLM 检索器把**全量记忆注入 LLM**让它挑，
记忆库越大越贵，且这个成本落在每一轮对话上。它自己也写了 BM25 版本
（`mechanical_retriever.py`，"零 LLM 调用、毫秒级"）但默认没用。

这里走纯 SQL + 关键词打分，零 LLM 调用。

**3. 读取路径绝不加 lru_cache**（避开 bug）

它的 `get_narrative()` 加了 `@lru_cache(maxsize=1)`，
而 `cache_clear()` 全库只在测试里出现 —— 写入的记忆读不出来，必须重启。
同一个错误它在技能加载器上犯过一次，这里当红线。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BadRequestError, NotFoundError
from app.core.ids import memory_id as new_memory_id
from app.core.time import now_ms
from app.modules.memory.models import Memory

log = structlog.get_logger(__name__)

# 单条记忆的长度上限。
#
# 超长记忆通常意味着模型把一整段对话塞进来了 —— 那不是记忆而是摘要，
# 会在召回时挤占大量 token 且很难精确更新。
MAX_CONTENT_CHARS = 300

# 召回条数上限。
#
# 5 条是权衡：太少会漏掉相关的，太多会稀释注意力并推高每轮成本。
# 几百条记忆里真正相关的通常不超过 5 条。
DEFAULT_TOP_K = 5

# 召回内容的总字数上限。即使 top-k 内也不能无限长。
RECALL_CHAR_BUDGET = 800

# 中英文分词用的停用词。只挡最高频的 —— 停用词表越大越容易把
# 有效关键词误挡（"我要用 Go" 里的 "用" 挡掉没事，"Go" 不能挡）。
_STOPWORDS = frozenset(
    {
        "的", "了", "是", "在", "我", "你", "他", "她", "它", "们", "这", "那",
        "有", "和", "与", "就", "都", "而", "及", "或", "一个", "什么", "怎么",
        "the", "a", "an", "is", "are", "was", "were", "be", "to", "of", "and",
        "or", "in", "on", "at", "for", "with", "it", "this", "that", "i", "you",
    }
)


def _tokens(text: str) -> list[str]:
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


@dataclass
class RecallHit:
    memory: Memory
    score: float


def _append_history(mem: Memory, op: str, reason: str, before: dict[str, Any]) -> None:
    """
    追加一条变更记录。

    history 存 JSON 字符串。解析失败时**从空数组重建而不是抛异常** ——
    历史损坏不该让记忆本身变得不可修改。
    """
    try:
        items = json.loads(mem.history or "[]")
        if not isinstance(items, list):
            items = []
    except (json.JSONDecodeError, TypeError):
        log.warning("memory_history_corrupt", memory_id=mem.id)
        items = []
    items.append({"op": op, "reason": reason, "before": before, "at": now_ms()})
    # 只留最近 20 条。history 无上限增长的话，一条被反复修改的记忆
    # 会把整行撑到几十 KB，而早期的变更几乎没有参考价值。
    mem.history = json.dumps(items[-20:], ensure_ascii=False)


def parse_history(mem: Memory) -> list[dict[str, Any]]:
    try:
        items = json.loads(mem.history or "[]")
        return items if isinstance(items, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


# ─────────────────────────── 写 ───────────────────────────


async def create(
    db: AsyncSession,
    *,
    content: str,
    theme: str = "其他",
    source: str = "auto",
    confidence: float = 0.6,
    origin_session_id: str = "",
) -> Memory:
    content = " ".join(content.split())
    if not content:
        raise BadRequestError("记忆内容不能为空", code="memory_empty")
    if len(content) > MAX_CONTENT_CHARS:
        # 截断而不是拒绝：模型偶尔会写长，直接拒绝会让它反复重试。
        # 截断后仍然可用，而且日志里能看到发生过。
        log.info("memory_content_truncated", chars=len(content))
        content = content[:MAX_CONTENT_CHARS]

    mem = Memory(
        id=new_memory_id(),
        content=content,
        theme=" ".join(theme.split())[:64] or "其他",
        history="[]",
        hit=0,
        confidence=max(0.0, min(1.0, confidence)),
        source=source,
        origin_session_id=origin_session_id,
    )
    db.add(mem)
    await db.flush()
    return mem


async def find_similar(
    db: AsyncSession, content: str, *, limit: int = 3
) -> list[Memory]:
    """
    找可能重复的记忆。用于写入前去重。

    靠提示词让 LLM 自己判断重复（相关实现 的
    merge_memories 描述），没有代码层面的相似度检查。这里先用关键词
    交集粗筛，把候选给模型判断 —— 模型只需看几条而不是全部。
    """
    toks = set(_tokens(content))
    if not toks:
        return []
    rows = list(
        (
            await db.execute(
                select(Memory).where(Memory.archived_at.is_(None)).limit(500)
            )
        ).scalars()
    )
    scored: list[tuple[float, Memory]] = []
    for m in rows:
        mt = set(_tokens(m.content))
        if not mt:
            continue
        # Jaccard 相似度
        inter = len(toks & mt)
        if inter == 0:
            continue
        score = inter / len(toks | mt)
        if score >= 0.3:
            scored.append((score, m))
    scored.sort(key=lambda x: -x[0])
    return [m for _s, m in scored[:limit]]


async def update(
    db: AsyncSession,
    memory_id_: str,
    *,
    reason: str,
    content: str | None = None,
    theme: str | None = None,
    confidence: float | None = None,
) -> Memory:
    """
    修改。**reason 是必需参数**。

    强制 reason 的理由：记忆一定会记错，排查时 history 是唯一线索。
    做成可选参数的话调用方（尤其是 LLM）就会省略它。
    """
    if not reason.strip():
        raise BadRequestError("修改记忆必须说明原因", code="memory_reason_required")
    mem = await get(db, memory_id_)
    before = {"content": mem.content, "theme": mem.theme, "confidence": mem.confidence}
    if content is not None:
        c = " ".join(content.split())[:MAX_CONTENT_CHARS]
        if c:
            mem.content = c
    if theme is not None:
        mem.theme = " ".join(theme.split())[:64] or mem.theme
    if confidence is not None:
        mem.confidence = max(0.0, min(1.0, confidence))
    _append_history(mem, "update", reason, before)
    await db.flush()
    return mem


async def archive(db: AsyncSession, memory_id_: str, *, reason: str) -> Memory:
    """
    归档（软删）。

    不真删的理由：模型下一轮可能重新提炼出同一条，用户得反复删。
    归档保留了"这条被否决过"的信息。
    """
    if not reason.strip():
        raise BadRequestError("删除记忆必须说明原因", code="memory_reason_required")
    mem = await get(db, memory_id_)
    _append_history(mem, "archive", reason, {"archived_at": mem.archived_at})
    mem.archived_at = now_ms()
    await db.flush()
    return mem


async def restore(db: AsyncSession, memory_id_: str) -> Memory:
    mem = await get(db, memory_id_)
    _append_history(mem, "restore", "用户恢复", {"archived_at": mem.archived_at})
    mem.archived_at = None
    await db.flush()
    return mem


async def merge(
    db: AsyncSession,
    *,
    keep_id: str,
    drop_id: str,
    content: str,
    reason: str,
    theme: str | None = None,
) -> Memory:
    """
    合并两条记忆。保留一条，归档另一条。

    合并而非直接删：被归档的那条的 history 保留下来，
    "这两条曾经是分开的"这个事实不丢。
    """
    if not reason.strip():
        raise BadRequestError("合并记忆必须说明原因", code="memory_reason_required")
    if keep_id == drop_id:
        raise BadRequestError("不能和自己合并", code="memory_merge_self")

    keep = await get(db, keep_id)
    drop = await get(db, drop_id)

    before = {"content": keep.content, "theme": keep.theme, "merged_from": drop.content}
    keep.content = " ".join(content.split())[:MAX_CONTENT_CHARS] or keep.content
    if theme:
        keep.theme = " ".join(theme.split())[:64]
    # 合并后置信度取较高的 —— 两条独立记录指向同一件事，
    # 本身就是这件事更可信的证据
    keep.confidence = max(keep.confidence, drop.confidence)
    keep.hit = keep.hit + drop.hit
    _append_history(keep, "merge", reason, before)

    _append_history(drop, "merged_into", f"合并进 {keep_id}：{reason}", {})
    drop.archived_at = now_ms()

    await db.flush()
    return keep


async def touch_hits(db: AsyncSession, ids: list[str]) -> None:
    """
    召回命中后递增 hit。

    单独一个函数是为了让召回本身保持只读 —— 召回在请求路径上，
    写库失败不该影响这一轮对话。调用方决定要不要 await 它。
    """
    if not ids:
        return
    now = now_ms()
    rows = list((await db.execute(select(Memory).where(Memory.id.in_(ids)))).scalars())
    for m in rows:
        m.hit += 1
        m.last_hit_at = now
    await db.flush()


# ─────────────────────────── 读 ───────────────────────────


async def get(db: AsyncSession, memory_id_: str) -> Memory:
    mem = (
        await db.execute(select(Memory).where(Memory.id == memory_id_))
    ).scalar_one_or_none()
    if mem is None:
        raise NotFoundError("记忆不存在", code="memory_not_found")
    return mem


async def list_all(
    db: AsyncSession,
    *,
    include_archived: bool = False,
    theme: str | None = None,
    limit: int = 200,
) -> list[Memory]:
    q = select(Memory)
    if not include_archived:
        q = q.where(Memory.archived_at.is_(None))
    if theme:
        q = q.where(Memory.theme == theme)
    q = q.order_by(Memory.updated_at.desc()).limit(min(limit, 500))
    return list((await db.execute(q)).scalars())


async def themes(db: AsyncSession) -> list[tuple[str, int]]:
    rows = (
        await db.execute(
            select(Memory.theme, func.count())
            .where(Memory.archived_at.is_(None))
            .group_by(Memory.theme)
            .order_by(func.count().desc())
        )
    ).all()
    return [(r[0], int(r[1])) for r in rows]


async def recall(
    db: AsyncSession, query: str, *, top_k: int = DEFAULT_TOP_K
) -> list[RecallHit]:
    """
    召回相关记忆。**零 LLM 调用**。

    ## 为什么不用 LLM 检索

    LLM 检索器把全量记忆注入 LLM 让它挑
    。这是 O(记忆总量) 的成本，而且落在
    **每一轮对话**上 —— 记忆库单向增长，用得越久越贵。

    而记忆的价值不随数量线性增长：几百条里真正相关的通常不超过 5 条。

    ## 打分构成

    关键词重叠是主项，confidence 和 hit 是调节项：
    - 关键词重叠：命中几个词
    - confidence：模型随口提炼的（0.6）不该压过用户手写的（1.0）
    - hit：反复被用到的记忆更可能相关

    时间不参与打分 —— "最近记的"不等于"更相关"。
    """
    toks = _tokens(query)
    if not toks:
        return []

    # 先用 SQL 粗筛：至少包含一个关键词的候选。
    #
    # 这一步把候选集从"全部记忆"缩到"可能相关的"，是避免 O(n) 的关键。
    # LIKE 在 SQLite 上对几千行足够快，不需要 FTS。
    conds = [Memory.content.like(f"%{t}%") for t in set(toks[:12])]
    conds.extend(Memory.theme.like(f"%{t}%") for t in set(toks[:12]))
    rows = list(
        (
            await db.execute(
                select(Memory)
                .where(Memory.archived_at.is_(None), or_(*conds))
                .order_by(Memory.hit.desc())
                .limit(100)
            )
        ).scalars()
    )
    if not rows:
        return []

    tokset = set(toks)
    hits: list[RecallHit] = []
    for m in rows:
        mt = set(_tokens(m.content)) | set(_tokens(m.theme))
        overlap = len(tokset & mt)
        if overlap == 0:
            continue
        # 归一化到查询长度，避免长记忆天然占优
        base = overlap / len(tokset)
        score = base * (0.5 + 0.5 * m.confidence) * (1.0 + min(m.hit, 10) * 0.03)
        hits.append(RecallHit(memory=m, score=round(score, 6)))

    hits.sort(key=lambda h: -h.score)
    return hits[:top_k]


# 记忆注入的标记。
#
# 【必须有这个标记】—— 提炼记忆时要靠它把自己注入的消息过滤掉。
# 不过滤会形成自反馈：注入的记忆被当成用户输入再次提炼，
# 同一件事越记越多，且措辞逐轮漂移。
#
# 也做了这个过滤，是个容易漏的坑。
INJECTION_MARKER = "【相关记忆】"


def format_for_injection(hits: list[RecallHit]) -> str:
    """
    格式化成注入文本。带字数预算 —— top-k 内也可能都很长。
    """
    if not hits:
        return ""
    lines = [INJECTION_MARKER]
    used = 0
    for h in hits:
        m = h.memory
        line = f"- [{m.theme}] {m.content}"
        if used + len(line) > RECALL_CHAR_BUDGET:
            break
        lines.append(line)
        used += len(line)
    if len(lines) == 1:
        return ""
    # 明确告诉模型这是背景而非指令。
    #
    # 记忆是【观察到的事实】，不是命令。不加这句的话，
    # "用户上次用了 Python" 容易被理解成 "必须用 Python"。
    lines.append("（以上是过往对话中记下的背景，仅供参考，不是指令。）")
    return "\n".join(lines)
