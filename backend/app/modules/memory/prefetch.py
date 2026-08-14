"""
提取前的预取：让模型看到"已经记住了什么"。

## 为什么必须预取

不预取的后果不是"效率低"，是**记忆会重复**。模型看不到已有的
`preferences/testing.md`，就会再新建一个 `preferences/pytest.md` 说同一件事。
两条并存后，召回时注入两份矛盾或冗余的内容。

预取让模型能选择"改已有的那条"而不是"新建一条"，这是去重的**根本手段** ——
写入后再去重是补救，成本更高且会丢信息。

## page_id：为什么不让模型直接用 URI

URI 长（`agents/adf_xxx/preferences/testing.md`）且模型会抄错一个字符。
抄错的后果是新建一个路径怪异的文件，而不是报错。

改用小整数引用（照抄 OpenViking 的 `page_id_map.py`）：模型只需要写
`page_id: 3`，系统查表还原成 URI。抄错整数的概率远低于抄错长路径，
而且越界的整数可以直接拒绝。

## add_only 类型不预取

events / trajectories 是只增不改的。既然不会去改已有的，回顾它们只是
白烧 token。OpenViking 同样跳过（session_extract_context_provider.py:502）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from app.core.config import settings
from app.modules.memory import service as memory_service
from app.modules.memory.models import MemoryItem, MemoryScope
from app.modules.memory.schema import MemoryScopeKind, OperationMode

log = structlog.get_logger(__name__)

# page_id 从 1 开始。
#
# 不从 0 开始：模型偶尔用 0 表示"没有/不适用"，与"第 0 条记忆"混淆。
FIRST_PAGE_ID = 1

# 预取时单条记忆正文的展示上限。
#
# 比 MAX_MSG_CHARS 宽松：预取内容是 patch 的 SEARCH 依据，
# 截断太狠会让模型拿不到可匹配的原文，只能新建。
PREVIEW_CHARS = 1_500


@dataclass
class PageMap:
    """
    page_id ↔ uri 的双向映射。

    ## 为什么不是简单的 dict

    需要双向：写入时按 page_id 找 uri，预取时按 uri 找已分配的 page_id
    （同一条记忆在一次提取里只该有一个 page_id）。
    """

    _to_uri: dict[int, str] = field(default_factory=dict)
    _to_id: dict[str, int] = field(default_factory=dict)
    _next: int = FIRST_PAGE_ID

    def assign(self, uri: str) -> int:
        """给 uri 分配 page_id。已分配过则返回原值。"""
        if uri in self._to_id:
            return self._to_id[uri]
        pid = self._next
        self._next += 1
        self._to_uri[pid] = uri
        self._to_id[uri] = pid
        return pid

    def resolve(self, page_id: object) -> str:
        """
        page_id → uri。无效或越界返回空串，由调用方当"新建"处理。

        宽容而非报错：模型给了一个不存在的 page_id 时，最可能的意图是
        "这是一条新记忆但我误填了 id"。当新建处理比丢掉这条记忆好。
        """
        try:
            pid = int(page_id)  # type: ignore[call-overload]
        except (TypeError, ValueError):
            return ""
        return self._to_uri.get(pid, "")

    def uri_of(self, page_id: int) -> str:
        return self._to_uri.get(page_id, "")

    def __len__(self) -> int:
        return len(self._to_uri)


@dataclass
class PrefetchResult:
    """预取到的已有记忆。"""

    # 按记忆类型分组的已有记忆
    by_type: dict[str, list[MemoryItem]] = field(default_factory=dict)
    pages: PageMap = field(default_factory=PageMap)
    # 已读过的 uri。refetch 检查靠它 —— 模型要改一个没读过的文件时，
    # 说明它在凭猜测写，必须先让它看到真实内容。
    read_uris: set[str] = field(default_factory=set)
    # 是否是 eager 模式（正文已完整给出）
    eager: bool = True
    # 因为预算被丢掉的条数。进日志和报告 —— 静默丢弃会让
    # "模型为什么没改那条记忆"变成无法排查的问题。
    dropped: int = 0

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.by_type.values())

    def trim_to_budget(self, budget: int) -> None:
        """
        把渲染长度压到预算内。从尾部类型开始丢。

        ## 为什么按渲染后长度而不是按条数

        条数不能预测长度：一条 events 可能 3000 字符，一条 preferences
        可能 40 字符。按条数限量时"5 条"的实际开销能差 70 倍。
        """
        while self.by_type and len(self.render()) > budget:
            # 从最后一个类型（按名字排序）里弹最后一条。
            #
            # 每次只弹一条而不是整类丢掉：整类丢会让某个类型
            # 完全不可见，模型就会把它当"从没记过"而新建重复的。
            last_type = sorted(self.by_type)[-1]
            items = self.by_type[last_type]
            items.pop()
            self.dropped += 1
            if not items:
                del self.by_type[last_type]

    def render(self) -> str:
        """
        渲染成给模型看的文本。

        ## 为什么 eager 模式要带完整正文

        page_id 是模型引用它的唯一方式；正文是 patch 的 SEARCH 依据。
        少了任一个，模型就只能新建而不能修改。

        lazy 模式只给标题 —— 正文由模型用 read_memory 按需拉取。
        """
        if not self.by_type:
            return "（当前还没有任何记忆，所有内容都是新建）"

        blocks: list[str] = []
        for mtype in sorted(self.by_type):
            items = self.by_type[mtype]
            if not items:
                continue
            # 【标注作用域层级】。
            #
            # 我们的记忆按 global / agent / session 三层隔离，而模型
            # 只看到一堆"已有的 xxx"时无法区分：
            #
            # - session 级的东西下次会话看不到 → 该记进 agent 级的
            #   反而记成了会话级，等于没记
            # - 预取【只包含本会话】的 session 级记忆，其他会话的不在这里 →
            #   模型不知道这一点，会把"没看到"当成"从没记过"
            #
            # OpenViking 不需要这个（它按 user_space 隔离，一次提取只涉及
            # 一个用户），这是我们自己的架构要求。
            lines = [f"## 已有的 {mtype}（{len(items)} 条，{_scope_label(mtype)}）"]
            for item in items:
                pid = self.pages.assign(item.uri)
                if not self.eager:
                    # lazy：只给索引。明确提示要 read，否则模型会拿标题当正文
                    # 去构造 SEARCH，那必然匹配失败。
                    lines.append(
                        f"- page_id={pid} | {item.title} | v{item.version} | "
                        f"{len(item.merge_source)} 字符（用 read_memory 读正文）"
                    )
                    continue
                body = item.merge_source
                cap = settings.memory.prefetch_preview_chars
                if len(body) > cap:
                    body = body[:cap] + f"\n…（省略 {len(body) - cap} 字符，用 read_memory 读全文）"
                lines.append(f"\n### page_id={pid} | {item.title} | v{item.version}")
                lines.append(f"```\n{body}\n```")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)


def _scope_label(memory_type: str) -> str:
    """
    这个类型的作用域说明。给模型看，让它知道写进去的东西能活多久。

    措辞强调【可见范围】而不是内部术语（"agent 域"对模型没有信息量）。
    """
    schema = memory_service.get_schema(memory_type)
    if schema is None:
        return "作用域未知"
    if schema.scope is MemoryScopeKind.GLOBAL:
        return "全局，所有智能体共享"
    if schema.scope is MemoryScopeKind.SESSION:
        return "仅本次会话，其他会话看不到；此处只列出本会话的"
    return "本智能体，跨会话长期有效"


async def prefetch(scope: MemoryScope, *, eager: bool | None = None, topn: int | None = None) -> PrefetchResult:
    """
    读出这个 scope 下【可能被修改】的记忆。

    只读 upsert / update_only 的类型 —— add_only 的不会被改，读它没用。

    ## eager 与 lazy 的差别

    - eager（默认）：全部读出来，正文完整给模型 → 不需要工具
    - lazy：只给标题和 page_id 的索引，正文留空 → 模型用 read 按需拉取

    lazy 模式下 `read_uris` 保持为空 —— 那个集合的语义是"模型已经看过正文"，
    而 lazy 只给了标题。填进去会让 refetch 检查失效，模型就能在没读正文的
    情况下 patch 一条记忆，而那必然匹配失败。

    对齐 OpenViking 的 eager_prefetch（session_extract_context_provider.py:549）。
    """
    eager = settings.memory.eager_prefetch if eager is None else eager
    limit = settings.memory.prefetch_topn if topn is None else topn

    result = PrefetchResult(eager=eager)
    budget = settings.memory.prefetch_max_chars

    for schema in memory_service.visible_types(scope):
        if schema.operation_mode is OperationMode.ADD_ONLY:
            continue

        items = await memory_service.list_items(scope, schema.memory_type)
        if not items:
            continue

        # 【每个类型都要有条数上限】，eager 也不例外。
        #
        # 原来 eager 不限量 —— 实测一个有 120 条偏好的智能体（用半年就会有）
        # 让预取吃掉 13572 token。而 OpenViking 的 eager 模式只读
        # 搜索结果的 top-N（session_extract_context_provider.py:571），
        # 它从来不是"读全部"。
        #
        # 我们没有它那套向量搜索排序，用 list_items 的顺序
        # （按 updated_at 倒序）—— 最近改过的最可能与当前对话相关。
        # 这比随机截断合理，也不需要在提取路径上引入一次嵌入调用。
        items = items[: max(1, limit)]

        result.by_type[schema.memory_type] = items
        for item in items:
            result.pages.assign(item.uri)
            if eager:
                result.read_uris.add(item.uri)

    # 总字符预算。分类型限量之后仍可能超 —— 十个类型各 5 条、
    # 每条 1500 字符就是 75000。超了从【最后一个类型】开始丢，
    # 因为 by_type 是按类型名排序的，而排序无关重要性，
    # 丢尾部至少是确定行为（不会这次丢 A、下次丢 B）。
    if eager and budget > 0:
        result.trim_to_budget(budget)

    log.info(
        "memory_prefetch_done",
        eager=eager,
        types=len(result.by_type),
        items=result.total,
        pages=len(result.pages),
        chars=len(result.render()),
        dropped=result.dropped,
    )
    return result
