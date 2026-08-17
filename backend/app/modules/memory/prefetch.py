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
from typing import Any

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
        渲染成给模型看的文本（纯文本格式，向后兼容）。

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

    def render_as_tool_calls(self) -> list[dict[str, Any]]:
        """
        渲染成 tool call/result 对的格式（参考 OpenViking）。

        ## 为什么改用 tool call/result 格式

        参考 OpenViking session_extract_context_provider.py:552-576:
        - 预取内容通过 `list_memories` 和 `read_memory` 的 tool call/result 注入
        - 更符合 LLM 的训练模式（Claude/GPT 都见过大量 tool use 数据）
        - 与 lazy 模式的工具调用风格一致
        - 更容易扩展（添加 search 结果等）

        ## 格式

        每个 dict 包含两个键：
        - "tool_call": {"id": "call_N", "name": "list_memories" | "read_memory", "arguments": {...}}
        - "tool_result": {"call_id": "call_N", "content": ...}
        """
        messages: list[dict[str, Any]] = []
        call_id = 1

        if not self.by_type:
            # 空的情况也要返回一个 list 结果
            messages.append(
                {
                    "tool_call": {
                        "id": f"call_{call_id}",
                        "name": "list_memories",
                        "arguments": {},
                    },
                    "tool_result": {
                        "call_id": f"call_{call_id}",
                        "content": "当前还没有任何记忆，所有内容都是新建。",
                    },
                }
            )
            return messages

        # 按类型组织 list_memories 结果
        for mtype in sorted(self.by_type):
            items = self.by_type[mtype]
            if not items:
                continue

            # list_memories 返回该类型的索引
            list_result = self._render_list_result(mtype, items)
            messages.append(
                {
                    "tool_call": {
                        "id": f"call_{call_id}",
                        "name": "list_memories",
                        "arguments": {"memory_type": mtype},
                    },
                    "tool_result": {
                        "call_id": f"call_{call_id}",
                        "content": list_result,
                    },
                }
            )
            call_id += 1

            # eager 模式：为每条记忆生成 read_memory call/result
            if self.eager:
                for item in items:
                    pid = self.pages.assign(item.uri)
                    read_result = self._render_read_result(item)
                    messages.append(
                        {
                            "tool_call": {
                                "id": f"call_{call_id}",
                                "name": "read_memory",
                                "arguments": {"page_id": pid},
                            },
                            "tool_result": {
                                "call_id": f"call_{call_id}",
                                "content": read_result,
                            },
                        }
                    )
                    call_id += 1

        return messages

    def _render_list_result(self, mtype: str, items: list[MemoryItem]) -> str:
        """渲染 list_memories 的结果。"""
        scope_label = _scope_label(mtype)
        lines = [f"类型：{mtype}（{scope_label}）", f"数量：{len(items)} 条", ""]

        for item in items:
            pid = self.pages.assign(item.uri)
            size_hint = f"{len(item.merge_source)} 字符"
            if not self.eager:
                size_hint += "（需要用 read_memory 读取正文）"
            lines.append(f"- page_id={pid} | {item.title} | v{item.version} | {size_hint}")

        return "\n".join(lines)

    def _render_read_result(self, item: MemoryItem) -> str:
        """渲染 read_memory 的结果。"""
        body = item.merge_source
        cap = settings.memory.prefetch_preview_chars
        truncated = ""
        if len(body) > cap:
            truncated = f"\n\n（省略 {len(body) - cap} 字符，用 read_memory 可读全文）"
            body = body[:cap]

        return f"""\
标题：{item.title}
版本：v{item.version}
内容：
```
{body}
```{truncated}"""


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


def build_search_query(messages: list[Any], timestamps: list[Any] | None = None) -> str:
    """
    从对话消息中构建紧凑的向量搜索查询。

    ## 设计原则（参考 OpenViking session_extract_context_provider.py:347）

    - LLM 已经通过 ExtractContext 看到完整对话，搜索查询不需要重复全文
    - 查询只需要**主题召回信号**：用户在谈什么、关键实体、上下文线索
    - 用户消息权重更高（截断更宽松），助手消息次之

    ## 截断策略

    - 单条用户消息：最多 user_msg_max_chars 字符
    - 单条助手消息：最多 assistant_msg_max_chars 字符
    - 整个查询：最多 query_max_chars 字符

    ## 返回格式

    紧凑文本，空格分隔。嵌入模型会处理语义，不需要结构化格式。
    """
    from app.modules.agent.messages import Msg

    cfg = settings.memory
    user_max = cfg.prefetch_search_user_msg_max_chars
    assistant_max = cfg.prefetch_search_assistant_msg_max_chars
    query_max = cfg.prefetch_search_query_max_chars

    sections: list[str] = []

    for msg in messages:
        if not isinstance(msg, Msg):
            continue

        role = msg.role
        content = msg.content or ""

        # 跳过系统消息和工具消息（对召回无信号价值）
        if role in ("system", "tool"):
            continue

        # 规范化：去掉多余空白
        normalized = " ".join(content.split())
        if not normalized:
            continue

        # 按角色截断
        max_chars = user_max if role == "user" else assistant_max
        if len(normalized) > max_chars:
            normalized = normalized[:max_chars].rstrip() + "..."

        sections.append(normalized)

    # 拼接并截断到总长度上限
    query = " ".join(sections)
    if len(query) > query_max:
        query = query[: query_max - 3].rstrip() + "..."

    return query if query else "conversation"  # 空查询时的 fallback


async def prefetch(
    scope: MemoryScope,
    *,
    messages: list[Any] | None = None,
    db: Any = None,
    model: Any = None,
    eager: bool | None = None,
    topn: int | None = None,
) -> PrefetchResult:
    """
    预取这个 scope 下【可能被修改】的记忆。

    ## 新增参数（向量搜索需要）

    - messages: 对话消息列表，用于构建搜索查询
    - db: 数据库连接，用于向量搜索
    - model: 嵌入模型，用于查询向量化

    ## 流程

    1. **构建查询**：从消息中提取主题摘要
    2. **向量搜索**：在 scope 范围内搜索相关记忆（如果有 messages + db + model）
    3. **预算感知加载**：
       - 高相关（top）：读全文，消耗预算
       - 预算不足：只给 URI + 标题，LLM 按需用 read_memory 工具读取
    4. **回退策略**：没有向量搜索时按 updated_at 倒序取前 N 条

    ## eager 与 lazy 的差别

    - eager（默认）：尽可能读全文（受预算限制）→ 不需要工具
    - lazy：只给标题和 page_id 的索引，正文留空 → 模型用 read 按需拉取

    lazy 模式下 `read_uris` 保持为空 —— 那个集合的语义是"模型已经看过正文"，
    而 lazy 只给了标题。填进去会让 refetch 检查失效。
    """
    from app.modules.memory import vectorize

    eager = settings.memory.eager_prefetch if eager is None else eager
    limit = settings.memory.prefetch_topn if topn is None else topn

    result = PrefetchResult(eager=eager)
    budget = settings.memory.prefetch_max_chars

    # ── 1. 向量搜索（如果可用） ──
    search_hits: list[Any] = []
    if messages and db and model:
        query = build_search_query(messages)
        if query:
            try:
                search_hits = await vectorize.search(
                    db,
                    scope,
                    query,
                    model=model,
                    limit=settings.memory.prefetch_search_limit,
                )
                log.debug("memory_search_done", query_len=len(query), hits=len(search_hits))
            except Exception as e:
                log.warning("memory_search_failed", error=str(e))
                search_hits = []

    # ── 2. 按相关性加载（或回退到时间排序） ──
    if search_hits:
        # 向量搜索成功：按相关性顺序加载
        await _load_from_search(scope, search_hits, result, budget, eager)
    else:
        # 回退：按 updated_at 倒序取前 N 条
        await _load_fallback(scope, result, budget, eager, limit)

    log.info(
        "memory_prefetch_done",
        eager=eager,
        search_used=bool(search_hits),
        types=len(result.by_type),
        items=result.total,
        pages=len(result.pages),
        chars=len(result.render()),
        dropped=result.dropped,
    )
    return result


async def _load_from_search(
    scope: MemoryScope,
    hits: list[Any],
    result: PrefetchResult,
    budget: int,
    eager: bool,
) -> None:
    """
    从搜索结果按相关性顺序加载记忆。

    ## 预算感知策略

    - 按相关性顺序（hits 已排序）逐条加载
    - 每条先读元数据（标题、字段），判断正文长度
    - 预算够：读全文，消耗预算
    - 预算不足：只给 URI + 标题（page_id），不读正文
    - **至少加载 1 条全文**：即使第一条就超预算

    ## 为什么不分类型

    向量搜索已经按相关性排序了，人为分类型会破坏这个顺序。
    而且不同类型的记忆可能相关性差异很大，强行每类取 N 条会浪费预算。
    """
    budget_remaining = budget
    loaded_full_count = 0

    for hit in hits:
        uri = hit.uri
        score = getattr(hit, "score", 0.0)

        # 读取这条记忆
        item = await memory_service.read_item(uri, scope)
        if not item:
            continue

        memory_type = item.memory_type
        if memory_type not in result.by_type:
            result.by_type[memory_type] = []

        # 分配 page_id
        page_id = result.pages.assign(uri)

        # 判断是否加载全文
        content_len = len(item.merge_source)
        load_full = False

        if eager:
            # eager 模式：尽可能加载全文
            if budget_remaining > content_len or loaded_full_count == 0:
                # 预算够，或者这是第一条（保证至少 1 条全文）
                load_full = True
                budget_remaining -= content_len
                loaded_full_count += 1
                result.read_uris.add(uri)

        if load_full:
            # 加载全文
            result.by_type[memory_type].append(item)
        else:
            # 只给标题（lazy item）
            lazy_item = MemoryItem(
                uri=uri,
                memory_type=memory_type,
                title=item.title,
                fields=item.fields,
                version=item.version,
                merge_source="",  # 空正文
                updated_at=item.updated_at,
            )
            result.by_type[memory_type].append(lazy_item)
            result.pages.assign(uri)

        log.debug(
            "memory_item_loaded",
            uri=uri,
            page_id=page_id,
            score=score,
            full=load_full,
            chars=content_len if load_full else 0,
        )


async def _load_fallback(
    scope: MemoryScope,
    result: PrefetchResult,
    budget: int,
    eager: bool,
    limit: int,
) -> None:
    """
    回退策略：没有向量搜索时，按 updated_at 倒序取前 N 条。

    分类型限量，保证每个类型都有机会被看到。
    """
    for schema in memory_service.visible_types(scope):
        if schema.operation_mode is OperationMode.ADD_ONLY:
            continue

        items = await memory_service.list_items(scope, schema.memory_type)
        if not items:
            continue

        items = items[: max(1, limit)]

        result.by_type[schema.memory_type] = items
        for item in items:
            result.pages.assign(item.uri)
            if eager:
                result.read_uris.add(item.uri)

    # 总字符预算截断
    if eager and budget > 0:
        result.trim_to_budget(budget)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 多智能体预取
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class MultiAgentPrefetchResult:
    """
    多智能体预取结果。

    包含：
    1. 共享记忆（global + session）
    2. 每个智能体的私有记忆（agent scope）
    """

    # 共享记忆（global + session），所有智能体都能看到
    shared: PrefetchResult = field(default_factory=PrefetchResult)

    # 每个智能体的私有记忆 {agent_id: PrefetchResult}
    agents: dict[str, PrefetchResult] = field(default_factory=dict)

    def merge_for_agent(self, agent_id: str) -> PrefetchResult:
        """
        为指定智能体合并预取结果：共享记忆 + 该智能体的私有记忆。

        ## 合并策略

        1. 使用智能体的 page_id_map（保证 page_id 唯一）
        2. 先加载共享记忆，再加载私有记忆
        3. 如果有类型重叠，私有记忆的条目追加在后面

        Returns:
            合并后的 PrefetchResult，可直接传给 ExtractLoop
        """
        agent_result = self.agents.get(agent_id)
        if not agent_result:
            # 该智能体没有私有记忆，只返回共享记忆
            return self.shared

        # 创建新的 PrefetchResult，使用智能体的 page_id_map
        merged = PrefetchResult(
            pages=agent_result.pages,  # 使用智能体的 page_id_map
            eager=self.shared.eager or agent_result.eager,
        )

        # 1. 先加载共享记忆
        for memory_type, items in self.shared.by_type.items():
            merged.by_type[memory_type] = list(items)
            for item in items:
                merged.pages.assign(item.uri)
                if item.uri in self.shared.read_uris:
                    merged.read_uris.add(item.uri)

        # 2. 再加载私有记忆（追加或新增）
        for memory_type, items in agent_result.by_type.items():
            if memory_type not in merged.by_type:
                merged.by_type[memory_type] = []
            merged.by_type[memory_type].extend(items)
            for item in items:
                merged.pages.assign(item.uri)
                if item.uri in agent_result.read_uris:
                    merged.read_uris.add(item.uri)

        return merged


async def prefetch_multi_agent(
    session_id: str,
    agent_ids: list[str],
    *,
    messages: list[Any] | None = None,
    db: Any = None,
    model: Any = None,
    eager: bool | None = None,
    topn: int | None = None,
) -> MultiAgentPrefetchResult:
    """
    多智能体场景的两阶段预取。

    ## 流程

    1. **第一阶段**：提取会话共享记忆（global + session）
       - **Global 记忆**：直接全量读取（通常是配置、身份等，量少且都需要）
       - **Session 记忆**：向量搜索（事件、对话摘要等，需要相关性排序）
       - 一次搜索，所有智能体共享结果

    2. **第二阶段**：并行提取每个智能体的私有记忆
       - 每个智能体只读取自己的 agent scope
       - 根据 schema 决定是向量搜索还是直接读取
       - 多个智能体并发执行

    3. **合并**：调用 `merge_for_agent(agent_id)` 为每个智能体组装完整上下文

    ## 为什么区分搜索和直接读取

    - **Global 记忆**（profile, identity, soul）：
      - 量少（通常 < 5 条）
      - 都需要（不存在"不相关"的情况）
      - 直接读取，省去向量化和搜索开销

    - **Session/Agent 记忆**（events, preferences, entities）：
      - 量大（可能几十上百条）
      - 需要相关性排序（只加载与当前对话相关的）
      - 向量搜索，避免预取太多无关内容

    ## 参数

    - session_id: 会话 ID
    - agent_ids: 参与该会话的所有智能体 ID
    - messages: 对话消息列表（用于构建搜索查询）
    - db: 数据库连接
    - model: 嵌入模型
    - eager: 是否预读全文
    - topn: 搜索结果数量

    ## 返回

    MultiAgentPrefetchResult，包含：
    - shared: 共享记忆（global + session）
    - agents: {agent_id: 私有记忆}

    使用 `result.merge_for_agent(agent_id)` 获取该智能体的完整预取结果。
    """
    import asyncio

    result = MultiAgentPrefetchResult()
    _eager = eager if eager is not None else True
    _topn = topn or settings.memory.prefetch_search_limit

    # ── 第一阶段：提取共享记忆（global + session）──
    if not agent_ids:
        log.warning("memory_prefetch_no_agents", session_id=session_id)
        return result

    first_agent = agent_ids[0]
    shared_scope = MemoryScope(agent_id=first_agent, session_id=session_id)
    shared_result = PrefetchResult(eager=_eager)

    # 1.1 Global 记忆：直接全量读取
    for schema in memory_service.visible_types(shared_scope):
        if schema.scope is not MemoryScopeKind.GLOBAL:
            continue
        if schema.operation_mode is OperationMode.ADD_ONLY:
            continue

        items = await memory_service.list_items(shared_scope, schema.memory_type)
        if items:
            shared_result.by_type[schema.memory_type] = items
            for item in items:
                shared_result.pages.assign(item.uri)
                if _eager:
                    shared_result.read_uris.add(item.uri)

            log.debug(
                "memory_prefetch_global_direct",
                memory_type=schema.memory_type,
                items=len(items),
            )

    # 1.2 Session 记忆：向量搜索（如果有 messages + db + model）
    if messages and db and model:
        query_text = build_search_query(messages)

        # 只搜索 session 级别的记忆
        session_types = [
            s.memory_type
            for s in memory_service.visible_types(shared_scope)
            if s.scope is MemoryScopeKind.SESSION
            and s.operation_mode is not OperationMode.ADD_ONLY
        ]

        if session_types:
            try:
                search_results = await memory_service.search_memories(
                    db=db,
                    scope=shared_scope,
                    query=query_text,
                    model=model,
                    limit=_topn,
                )

                # 使用现有的 _load_from_search 逻辑
                budget = settings.memory.prefetch_preview_chars
                await _load_from_search(
                    scope=shared_scope,
                    hits=search_results,
                    result=shared_result,
                    budget=budget,
                    eager=_eager,
                )

                log.info(
                    "memory_prefetch_session_search",
                    query_chars=len(query_text),
                    results=len(search_results),
                    loaded=sum(len(v) for v in shared_result.by_type.values()),
                )
            except Exception as e:
                log.warning("memory_prefetch_session_search_failed", error=str(e))
    else:
        # 没有向量搜索能力，回退到按 updated_at 排序
        for schema in memory_service.visible_types(shared_scope):
            if schema.scope is not MemoryScopeKind.SESSION:
                continue
            if schema.operation_mode is OperationMode.ADD_ONLY:
                continue

            items = await memory_service.list_items(shared_scope, schema.memory_type)
            if items:
                items = items[: max(1, _topn)]
                shared_result.by_type[schema.memory_type] = items
                for item in items:
                    shared_result.pages.assign(item.uri)
                    if _eager:
                        shared_result.read_uris.add(item.uri)

                log.debug(
                    "memory_prefetch_session_fallback",
                    memory_type=schema.memory_type,
                    items=len(items),
                )

    result.shared = shared_result

    log.info(
        "memory_prefetch_shared_done",
        session_id=session_id,
        items=sum(len(v) for v in shared_result.by_type.values()),
        types=len(shared_result.by_type),
    )

    # ── 第二阶段：并行提取每个智能体的私有记忆 ──
    async def prefetch_agent_private(agent_id: str) -> tuple[str, PrefetchResult]:
        """
        提取单个智能体的私有记忆（只读 agent scope）。

        根据记忆类型特性决定是否需要搜索：
        - 静态配置类（profile, identity）：直接读取
        - 动态累积类（preferences, entities）：向量搜索
        """
        agent_scope = MemoryScope(agent_id=agent_id)
        agent_result = PrefetchResult(eager=_eager)

        # 获取 agent 级别的 schema
        agent_schemas = [
            s
            for s in memory_service.visible_types(agent_scope)
            if s.scope is MemoryScopeKind.AGENT
            and s.operation_mode is not OperationMode.ADD_ONLY
        ]

        # 区分静态和动态类型
        static_types = []  # 直接读取
        dynamic_types = []  # 向量搜索

        for schema in agent_schemas:
            # 判断标准：静态文件名的记忆直接读，动态文件名的搜索
            # 参考 OpenViking session_extract_context_provider.py:502-507
            if schema.memory_type in ("profile", "identity", "soul"):
                static_types.append(schema)
            else:
                dynamic_types.append(schema)

        # 直接读取静态类型
        for schema in static_types:
            items = await memory_service.list_items(agent_scope, schema.memory_type)
            if items:
                agent_result.by_type[schema.memory_type] = items
                for item in items:
                    agent_result.pages.assign(item.uri)
                    if _eager:
                        agent_result.read_uris.add(item.uri)

        # 向量搜索动态类型（如果有搜索能力）
        if dynamic_types and messages and db and model:
            query_text = build_search_query(messages)
            try:
                search_results = await memory_service.search_memories(
                    db=db,
                    scope=agent_scope,
                    query=query_text,
                    model=model,
                    limit=_topn,
                )

                budget = settings.memory.prefetch_preview_chars
                await _load_from_search(
                    scope=agent_scope,
                    hits=search_results,
                    result=agent_result,
                    budget=budget,
                    eager=_eager,
                )
            except Exception as e:
                log.warning(
                    "memory_prefetch_agent_search_failed",
                    agent_id=agent_id,
                    error=str(e),
                )
        else:
            # 没有搜索能力，回退到直接读取 top-N
            for schema in dynamic_types:
                items = await memory_service.list_items(agent_scope, schema.memory_type)
                if items:
                    items = items[: max(1, _topn)]
                    agent_result.by_type[schema.memory_type] = items
                    for item in items:
                        agent_result.pages.assign(item.uri)
                        if _eager:
                            agent_result.read_uris.add(item.uri)

        log.debug(
            "memory_prefetch_agent_done",
            agent_id=agent_id,
            items=sum(len(v) for v in agent_result.by_type.values()),
            types=len(agent_result.by_type),
        )
        return agent_id, agent_result

    # 并行提取所有智能体的私有记忆
    tasks = [prefetch_agent_private(aid) for aid in agent_ids]
    agent_results = await asyncio.gather(*tasks)
    result.agents = dict(agent_results)

    log.info(
        "memory_prefetch_multi_agent_done",
        session_id=session_id,
        agents=len(agent_ids),
        shared_items=sum(len(v) for v in result.shared.by_type.values()),
        total_agent_items=sum(
            sum(len(v) for v in ar.by_type.values()) for ar in result.agents.values()
        ),
    )

    return result
