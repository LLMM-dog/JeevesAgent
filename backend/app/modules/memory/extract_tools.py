"""
提取期可用的工具。

## 为什么提取需要工具

预取有上限。记忆多到装不下窗口时，全量预取会挤掉对话内容 ——
而对话才是提取的原料。这时必须让模型自己决定"我需要看哪几条记忆"。

OpenViking 有两种模式（`eager_prefetch` 开关，memory_config.py:58）：

- `eager_prefetch=true`  → 预取搜索结果 top-N，**不给工具**（get_tools 返回 []）
- `eager_prefetch=false` → 只预取 overview 与单文件，给 `read` 工具让模型按需拉取

两种都要支持。默认走哪种取决于记忆量：少的时候全预取更省一轮调用，
多的时候按需读取才装得下。

## 工具集为什么只有三个

`list` / `read` / `search`。对齐 OpenViking 的工具面（它给 `read`，
prefetch 阶段内部用 `search`）。

不给写入工具：写入是循环【结束后】的最终输出，不是过程中的动作。
让模型边探索边写会失去"要么全对要么全不写"的预检能力。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.core.config import settings
from app.modules.memory import service as memory_service
from app.modules.memory.models import MemoryScope
from app.modules.memory.prefetch import PageMap
from app.modules.memory.tokens import tokenize

log = structlog.get_logger(__name__)

# 单次 read 返回的正文上限。
#
# 比预取的 PREVIEW_CHARS 宽松：模型主动 read 说明它确实要看这条的全文
# 来构造 SEARCH 片段，截断太狠会让它拿不到可匹配的原文。
READ_MAX_CHARS = 4_000

# search 返回条数上限。与 OpenViking 的 search_files(limit=5) 一致。
SEARCH_LIMIT = 5


def tool_schemas() -> list[dict[str, Any]]:
    """
    发给模型的工具定义。

    参数都用 page_id 或记忆类型名，【不用 uri】—— 模型会抄错长路径，
    而抄错的后果是静默读到空内容。
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "list_memories",
                "description": (
                    "列出某个记忆类型下已有的记忆（只给标题和 page_id，不含正文）。"
                    "用来判断某件事是否已经记过。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "memory_type": {
                            "type": "string",
                            "description": "记忆类型名，如 preferences / experiences",
                        }
                    },
                    "required": ["memory_type"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_memory",
                "description": (
                    "读取一条记忆的完整正文。要用 patch 修改一条记忆前，"
                    "【必须】先读它 —— 否则你的 SEARCH 片段会匹配失败。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "page_id": {"type": "integer", "description": "记忆编号"}
                    },
                    "required": ["page_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_memories",
                "description": (
                    "按关键词搜索已有记忆，返回匹配的 page_id 与标题。"
                    "当你想记一件事但不确定是否已经记过时用它。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "关键词，空格分隔"},
                        "memory_type": {
                            "type": "string",
                            "description": "限定记忆类型。留空则搜全部",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
    ]


@dataclass
class ToolCall:
    """一次工具调用。与 LLM 层的 ToolCallDelta 合并后得到。"""

    call_id: str
    name: str
    arguments: str = "{}"

    def args(self) -> dict[str, Any]:
        try:
            v = json.loads(self.arguments or "{}")
            return v if isinstance(v, dict) else {}
        except json.JSONDecodeError:
            # 参数解析失败不抛异常 —— 返回空参数让工具回一个错误文本，
            # 模型能据此自我纠正。抛异常会让整次提取失败。
            return {}


@dataclass
class ToolRunner:
    """
    执行提取期的工具调用。

    持有 scope 和 PageMap —— 工具的全部作用就是"在这个 scope 里按 page_id
    查记忆"，两者缺一不可。
    """

    scope: MemoryScope
    pages: PageMap
    # 模型已经读过的 uri。refetch 检查靠它 —— 模型要改一个没读过的记忆时，
    # 说明它在凭猜测写。
    read_uris: set[str] = field(default_factory=set)
    # 调用记录，供报告与测试
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def execute(self, call: ToolCall) -> str:
        """
        执行一个调用，返回给模型看的文本。

        ## 为什么返回文本而不是结构

        工具结果要拼进 messages 给模型看。返回 dict 的话每个调用点
        都要自己决定怎么序列化，而不同写法会让模型看到不一致的格式。
        """
        args = call.args()
        handler = {
            "list_memories": self._list,
            "read_memory": self._read,
            "search_memories": self._search,
        }.get(call.name)

        if handler is None:
            # 未知工具名。返回错误文本而非抛异常，并让调用方在下一轮关掉工具 ——
            # 模型持续调用不存在的工具会耗尽迭代预算。
            known = ", ".join(t["function"]["name"] for t in tool_schemas())
            result = f"错误：没有名为 {call.name} 的工具。可用的工具：{known}"
            self.calls.append({"name": call.name, "args": args, "unknown": True})
            return result

        try:
            result = await handler(args)
        except Exception as e:  # noqa: BLE001
            log.warning("memory_extract_tool_failed", tool=call.name, error=str(e))
            result = f"错误：工具 {call.name} 执行失败：{e}"

        self.calls.append({"name": call.name, "args": args, "chars": len(result)})
        return result

    @property
    def has_unknown_call(self) -> bool:
        return any(c.get("unknown") for c in self.calls)

    # ── 工具实现 ──────────────────────────────────

    async def _list(self, args: dict[str, Any]) -> str:
        mtype = str(args.get("memory_type") or "").strip()
        if not mtype:
            return "错误：需要 memory_type 参数"

        schema = memory_service.get_schema(mtype)
        if schema is None:
            available = ", ".join(s.memory_type for s in memory_service.visible_types(self.scope))
            return f"错误：没有 {mtype} 这个记忆类型。可用：{available}"

        items = await memory_service.list_items(self.scope, mtype)
        if not items:
            return f"{mtype} 下还没有任何记忆。"

        lines = [f"{mtype} 共 {len(items)} 条："]
        for item in items:
            pid = self.pages.assign(item.uri)
            lines.append(f"- page_id={pid} | {item.title} | v{item.version} | {len(item.merge_source)} 字符")
        return "\n".join(lines)

    async def _read(self, args: dict[str, Any]) -> str:
        pid = args.get("page_id")
        uri = self.pages.resolve(pid)
        if not uri:
            return (
                f"错误：page_id={pid} 不存在。"
                "先用 list_memories 或 search_memories 查到正确的 page_id。"
            )

        item = await memory_service.read_uri(uri)
        if item is None:
            return f"错误：page_id={pid} 对应的记忆已不存在。"

        self.read_uris.add(uri)
        body = item.merge_source
        note = ""
        cap = settings.memory.tool_read_max_chars
        if len(body) > cap:
            body = body[:cap]
            note = f"\n…（正文被截断，完整长度 {len(item.merge_source)} 字符）"

        return (
            f"page_id={pid} | {item.memory_type} | {item.title} | v{item.version}\n"
            f"---\n{body}{note}\n---\n"
            "（要修改它，用上面的原文作为 SEARCH 片段，必须逐字符一致）"
        )

    async def _search(self, args: dict[str, Any]) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            return "错误：需要 query 参数"

        mtype = str(args.get("memory_type") or "").strip()
        if mtype and memory_service.get_schema(mtype) is None:
            return f"错误：没有 {mtype} 这个记忆类型"

        items = await memory_service.list_items(self.scope, mtype)
        scored = _rank(query, items)
        if not scored:
            return f"没有匹配「{query}」的记忆。这件事应该是新的。"

        lines = [f"匹配「{query}」的记忆（按相关度）："]
        for score, item in scored[: settings.memory.tool_search_limit]:
            pid = self.pages.assign(item.uri)
            preview = " ".join(item.merge_source.split())[:120]
            lines.append(f"- page_id={pid} | {item.memory_type} | {item.title} | 相关度 {score:.2f}\n  {preview}")
        lines.append("（用 read_memory 读取完整正文后再决定是否修改）")
        return "\n".join(lines)


def _expand(text: str) -> set[str]:
    """
    分词并拆开 snake_case / 连字符。

    ## 为什么必须拆

    tokenize 把 `_` `.` `-` 当词内字符（它是为代码标识符设计的），
    于是 `alembic_sqlite_batch` 是【一个】token，搜 "alembic" 永远匹配不上。

    而记忆的标题恰好全是 snake_case（experience_name / tool_name / topic
    都要求小写下划线）—— 不拆的话按名字搜索基本失效。实测：
    搜 "alembic" 找不到 alembic_sqlite_batch。

    不改 tokenize 本身：它同时服务召回，那边的语义是"代码标识符要整体匹配"。
    在这里扩展比改共用函数安全。
    """
    tokens = set(tokenize(text))
    for token in list(tokens):
        for part in re.split(r"[_.\-]+", token):
            if len(part) >= 2:
                tokens.add(part)
    return tokens


def _rank(query: str, items: list[Any]) -> list[tuple[float, Any]]:
    """
    关键词打分。

    ## 为什么提取期用关键词而不是向量

    提取发生在 commit 时，此时【向量索引可能还没建】（新记忆刚写入）。
    而且提取要的是"这件事记过没有"这种精确判断，关键词重叠比语义相似
    更适合 —— 语义相似会把"pytest 偏好"和"ruff 偏好"判为相关，
    而那正好是我们要区分的两条。

    召回阶段用向量，那是另一个场景（模糊找相关记忆）。
    """
    q_tokens = _expand(query)
    if not q_tokens:
        return []

    out: list[tuple[float, Any]] = []
    for item in items:
        tokens = _expand(f"{item.title} {item.merge_source}")
        if not tokens:
            continue
        overlap = len(q_tokens & tokens)
        if overlap == 0:
            continue
        # 用 query 侧做分母：命中 query 里几成的词，与记忆本身长短无关。
        # 用并集做分母会让长记忆天然吃亏。
        out.append((overlap / len(q_tokens), item))

    out.sort(key=lambda pair: (-pair[0], pair[1].uri))
    return out
