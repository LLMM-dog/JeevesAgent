"""
提取循环：调 LLM 产出记忆操作，带校验与修复重试。

## 为什么是循环而不是一次调用

一次调用的失败率太高，而且失败有三种不同的原因，各自需要不同的补救：

1. **输出不是合法 JSON** → 告诉它格式错了，重来（最多 1 次）
2. **patch 的 SEARCH 匹配不上** → 把失败的片段和真实原文回给它，重来（最多 1 次）
3. **要改的记忆它没读过** → 补上那条记忆的内容，重来（refetch）

三者都是"给更多信息后再试一次"，所以是循环。控制流照抄 OpenViking 的
extract_loop.py:247-364，但去掉了工具调用 —— 它允许模型在循环里调
search/read 自己找记忆，我们改成一次性预取。

## 工具调用：两种模式

对齐 OpenViking 的 `eager_prefetch` 开关（memory_config.py:58）：

- `eager_prefetch=true` → 预取全部可改的记忆，**不给工具**
  （它的 `get_tools()` 在这个模式下返回 `[]`，
  session_extract_context_provider.py:603）
- `eager_prefetch=false` → 只预取轻量索引，给 `list` / `read` / `search`
  让模型自己按需拉取

两种都要支持，因为它们的适用条件不同：记忆少时全预取省一轮调用；
记忆多到装不下窗口时，全预取会挤掉对话内容 —— 而对话才是提取的原料。

## 工具调用的三个连带处理

照抄 OpenViking 的 extract_loop.py:268-279：

1. **未知工具名** → 下一轮关掉工具（`_disable_tools_for_iteration`）。
   模型持续调不存在的工具会耗尽迭代预算。
2. **调了工具就 +1 迭代预算**。工具调用不是"产出结果"的一轮，
   不该占用正常预算。
3. **并行执行**多个工具调用（它用 `asyncio.gather`）。

## 每一轮都可能扩展上限

`max_iterations` 会因为 refetch / 格式重试 / patch 修复而 +1。
这是刻意的：那三种情况不是"模型不听话"，是"信息不足"，
不该占用正常的迭代预算。OpenViking 同样这么做（extract_loop.py:276）。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.core.config import settings
from app.modules.memory import contract
from app.modules.memory.extract_context import ExtractContext
from app.modules.memory.extract_tools import ToolCall, ToolRunner, tool_schemas
from app.modules.memory.merge import MergeError, StrPatch, apply_str_patch
from app.modules.memory.prefetch import PrefetchResult
from app.modules.memory.schema import MemoryTypeSchema, MergeOp

log = structlog.get_logger(__name__)


@dataclass
class LoopStep:
    """一轮的记录。测试和排错靠它看清循环走了什么路径。"""

    iteration: int
    kind: str  # ok | parse_error | patch_error | refetch | empty
    detail: str = ""


@dataclass
class ExtractOutcome:
    """提取结果。"""

    # 按记忆类型分组的操作。每条是 {page_id, ...fields}
    operations: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    delete_page_ids: list[int] = field(default_factory=list)
    reasoning: str = ""
    steps: list[LoopStep] = field(default_factory=list)
    # 非致命问题。有值不代表失败 —— 部分成功是常态。
    warnings: list[str] = field(default_factory=list)
    # 模型在循环里调过的工具。排错时要能看出它"探索了什么"。
    tools_used: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_items(self) -> int:
        return sum(len(v) for v in self.operations.values())

    @property
    def iterations(self) -> int:
        return len(self.steps)


class ExtractLoop:
    """
    一次提取的编排。

    ## 为什么是类而不是函数

    循环里有六个需要跨轮保持的状态（重试计数、已注入的修复信息、
    步骤记录）。用函数就要么传一个 state 参数，要么用嵌套函数闭包 ——
    前者啰嗦，后者不好测。
    """

    def __init__(
        self,
        *,
        llm_call: Any,
        schemas: list[MemoryTypeSchema],
        prefetched: PrefetchResult,
        extract_context: ExtractContext,
        max_iterations: int | None = None,
        tool_runner: ToolRunner | None = None,
    ):
        self._call = llm_call
        self._schemas = [s for s in schemas if s.llm_fields()]
        self._pre = prefetched
        self._ctx = extract_context
        self._max = max_iterations or settings.memory.extract_max_iterations

        # tool_runner 为 None → eager_prefetch 模式（不给工具）
        self._tools = tool_runner
        self._disable_tools_this_round = False

        self._format_retried = False
        self._patch_repaired = False
        self._refetched: set[str] = set()

    @property
    def _tool_schemas(self) -> list[dict[str, Any]] | None:
        """本轮发给模型的工具定义。None 表示不给工具。"""
        if self._tools is None or self._disable_tools_this_round:
            return None
        return tool_schemas()

    # ── 主循环 ───────────────────────────────────

    async def run(self) -> ExtractOutcome:
        outcome = ExtractOutcome()
        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": self._user_prompt()},
        ]

        iteration = 0
        max_iterations = self._max

        while iteration < max_iterations:
            iteration += 1
            is_last = iteration >= max_iterations
            if is_last:
                # 最后一轮【必须关掉工具】，否则模型可能继续探索而永远不产出结果。
                self._disable_tools_this_round = True
                messages.append({"role": "user", "content": self._final_instruction()})

            reply = await self._call(messages, self._tool_schemas)
            # 每轮开始时恢复工具（关闭只作用于紧接的那一轮），
            # 但最后一轮的关闭要保持。
            if not is_last:
                self._disable_tools_this_round = False

            raw, tool_calls = _split_reply(reply)

            # 分支 A：模型要调工具 → 执行、把结果拼回消息、继续
            if tool_calls and self._tools is not None:
                results = await self._run_tools(messages, tool_calls)
                outcome.tools_used.extend(results)
                outcome.steps.append(
                    LoopStep(iteration, "tool_call", ",".join(c.name for c in tool_calls))
                )
                # 调工具不算"产出结果"的一轮，补一次预算
                if iteration >= max_iterations:
                    max_iterations += 1
                # 未知工具名 → 下一轮关掉工具，防止耗尽预算
                if self._tools.has_unknown_call:
                    self._disable_tools_this_round = True
                continue

            parsed = _parse_json(raw)

            if parsed is None:
                # 情况 1：输出不是合法 JSON
                outcome.steps.append(
                    LoopStep(iteration, "parse_error", _preview(raw))
                )
                # 【打全量预览而非截断】。实测 lazy 模式下真实模型在第 2 轮
                # 产出了 1585 字符的正文却解析失败，而 200 字符的预览
                # 看不出是哪里断的 —— 是被截断、混了解释文字、还是 JSON 本身有语法错。
                log.warning(
                    "memory_extract_parse_failed",
                    iteration=iteration,
                    chars=len(raw),
                    head=raw[:400],
                    tail=raw[-200:] if len(raw) > 400 else "",
                )
                if not self._format_retried:
                    self._format_retried = True
                    max_iterations += 1
                    messages.append({"role": "assistant", "content": _preview(raw, 400)})
                    messages.append({"role": "user", "content": _FORMAT_ERROR})
                    continue
                # 重试过一次仍然失败 → 当作"没有记忆要写"而不是硬失败。
                #
                # 硬失败会让整次 commit 回滚，而对话本身是成功的 ——
                # 提取失败不该影响用户已经完成的工作。
                outcome.warnings.append(f"输出无法解析为 JSON（已重试 1 次）：{_preview(raw)}")
                break

            ops = self._collect(parsed)

            # 情况 3：要改的记忆没被预取过 → 补上再试
            missing = self._unread_targets(ops)
            if missing and not self._refetched.issuperset(missing):
                self._refetched.update(missing)
                max_iterations += 1
                outcome.steps.append(LoopStep(iteration, "refetch", ",".join(sorted(missing))))
                messages.append({"role": "assistant", "content": json.dumps(parsed, ensure_ascii=False)})
                messages.append({"role": "user", "content": self._refetch_message(missing)})
                continue

            # 情况 2：patch 打不上 → 把真实原文回给它
            patch_errors = self._validate_patches(ops)
            if patch_errors and not self._patch_repaired:
                self._patch_repaired = True
                max_iterations += 1
                outcome.steps.append(
                    LoopStep(iteration, "patch_error", f"{len(patch_errors)} 处")
                )
                messages.append({"role": "assistant", "content": json.dumps(parsed, ensure_ascii=False)})
                messages.append({"role": "user", "content": self._repair_message(patch_errors)})
                continue

            if patch_errors:
                # 修复过一次仍然失败 → 丢掉打不上的那几条，保留其余。
                #
                # 【不整批丢弃】：一条 patch 写错不该让其他 9 条正确的记忆也丢掉。
                ops = self._drop_failed(ops, patch_errors)
                outcome.warnings.extend(e["message"] for e in patch_errors)

            outcome.operations = ops
            outcome.delete_page_ids = _int_list(parsed.get("delete_page_ids"))
            outcome.reasoning = str(parsed.get("reasoning") or "")
            outcome.steps.append(
                LoopStep(iteration, "ok", f"{sum(len(v) for v in ops.values())} 条")
            )
            break
        else:
            outcome.warnings.append(f"达到最大迭代次数 {max_iterations} 仍未产出结果")

        # 上限保护：一次提取写太多条说明模型在拆碎片
        outcome.operations = self._cap(outcome.operations, outcome)

        log.info(
            "memory_extract_loop_done",
            iterations=outcome.iterations,
            items=outcome.total_items,
            deletes=len(outcome.delete_page_ids),
            warnings=len(outcome.warnings),
            path=[s.kind for s in outcome.steps],
        )
        return outcome

    # ── 工具执行 ──────────────────────────────────

    async def _run_tools(
        self, messages: list[dict[str, Any]], calls: list[ToolCall]
    ) -> list[dict[str, Any]]:
        """
        并行执行工具调用，把 assistant + tool 消息拼回列表。

        ## 为什么并行

        模型常一次调三四个 read。串行的话每个都要等一次磁盘 I/O，
        而它们互不依赖。OpenViking 同样用 gather（extract_loop.py:582）。

        ## 消息结构必须完整配对

        assistant 消息带 tool_calls，后面【每个 call 都要有】对应的
        tool 消息。缺一条的话下一轮请求会被上游拒绝（400），
        而错误信息只说"messages 格式不对"，看不出是少了哪条。
        """
        assert self._tools is not None

        results = await asyncio.gather(*(self._tools.execute(c) for c in calls))

        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": c.call_id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": c.arguments},
                    }
                    for c in calls
                ],
            }
        )
        used: list[dict[str, Any]] = []
        for call, text in zip(calls, results, strict=True):
            messages.append({"role": "tool", "tool_call_id": call.call_id, "content": text})
            used.append({"name": call.name, "args": call.args(), "result_chars": len(text)})
        return used

    # ── 提示词 ───────────────────────────────────

    def _system_prompt(self) -> str:
        types_desc = "\n\n".join(
            f"### {s.memory_type}\n{s.description.strip()}" for s in self._schemas
        )
        return f"""\
你是一个记忆提取器。从对话中提取值得【长期记住】的内容。

# 总原则
- 宁缺勿滥。不确定是否值得记就不记
- 已经记过的内容要【修改已有记忆】（用 page_id），不要新建重复的
- 只记对话中明确出现的事实，不要推断和演绎
- 用中文书写记忆内容

# 记忆类型
{types_desc}

# page_id
- 修改已有记忆：填它的 page_id（见「已有记忆」部分）
- 新建记忆：page_id 填 null
- 删除过时的记忆：把 page_id 放进 delete_page_ids
{self._tools_section()}

# {contract.PATCH_RULES}

# 输出
只输出一个 JSON 对象，符合下面的 schema。不要输出解释文字、不要用 markdown 代码块包裹。

```json
{json.dumps(contract.operations_schema(self._schemas), ensure_ascii=False, indent=1)}
```"""

    def _tools_section(self) -> str:
        """
        工具说明。eager_prefetch 模式下为空 —— 那时没有工具可用，
        提到它们只会让模型尝试调用一个不存在的东西。
        """
        if self._tools is None:
            return ""
        return """
# 工具
你可以先调用工具查看已有记忆，再决定怎么写：
- `list_memories(memory_type)` —— 列出某类记忆的标题与 page_id
- `search_memories(query)` —— 按关键词找可能已经记过的内容
- `read_memory(page_id)` —— 读一条记忆的完整正文

【要用 patch 修改一条记忆前必须先 read 它】，否则你的 SEARCH 片段会匹配失败。
「已有记忆」部分给出的正文可能被截断，不足以构造 SEARCH 时就 read 一次。
探索完成后，直接输出最终 JSON，不要再调工具。"""

    def _user_prompt(self) -> str:
        convo = self._render_conversation()
        return f"""\
# 已有记忆
{self._pre.render()}

# 本次对话
{convo}

请提取记忆。先在 reasoning 里说明判断，再填各类型的数组。"""

    def _render_conversation(self) -> str:
        """
        渲染对话，【带消息下标】。

        下标是 events 的 ranges 字段的依据 —— 模型要能指出"这个事件对应
        第几到第几条消息"。不编号的话它只能猜。
        """
        lines: list[str] = []
        for i, msg in enumerate(self._ctx.messages):
            for line in ExtractContext._render_msg(msg):
                lines.append(f"[{i}] {line}")
        return "\n".join(lines) if lines else "（无对话）"

    def _final_instruction(self) -> str:
        skeleton = json.dumps(contract.empty_operations(self._schemas), ensure_ascii=False, indent=2)
        return (
            "这是最后一次机会。现在直接输出最终 JSON，不要再解释。\n"
            f"如果没有任何值得记的内容，输出这个空结构：\n{skeleton}"
        )

    def _refetch_message(self, uris: set[str]) -> str:
        """
        补上模型想改但没读过的记忆内容。

        这是防止"把已有记忆当新文件覆盖"的唯一屏障 ——
        模型不知道文件已存在时，会给出一个完整的新内容，
        而那会把已积累的内容整体顶掉。
        """
        blocks = []
        for uri in sorted(uris):
            blocks.append(f"### {uri}\n（这条记忆已存在但你没有看过它，不要凭猜测覆盖）")
        return (
            "你要写入的记忆中，有一些【已经存在】但不在上面的「已有记忆」列表里：\n\n"
            + "\n\n".join(blocks)
            + "\n\n请改用已有记忆列表里的 page_id 来修改它们，或者确认确实要新建。重新输出完整 JSON。"
        )

    def _repair_message(self, errors: list[dict[str, Any]]) -> str:
        blocks = []
        for err in errors:
            blocks.append(
                f"### {err['memory_type']}（page_id={err['page_id']}）\n"
                f"匹配失败的 search：\n```\n{err['search']}\n```\n"
                f"该记忆的真实原文：\n```\n{err['actual']}\n```"
            )
        return (
            "以下 SEARCH 片段在原文中找不到，无法应用：\n\n"
            + "\n\n".join(blocks)
            + "\n\n请对照真实原文重新给出 search（逐字符一致，含缩进），重新输出完整 JSON。"
        )

    # ── 解析与校验 ────────────────────────────────

    def _collect(self, parsed: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        """
        从解析结果里挑出各类型的操作。

        未知的键被忽略 —— 模型偶尔会自造一个记忆类型名。忽略比报错好：
        其余类型的内容仍然能写进去。
        """
        out: dict[str, list[dict[str, Any]]] = {}
        known = {s.memory_type for s in self._schemas}
        for key, value in parsed.items():
            if key not in known or not isinstance(value, list):
                continue
            items = [v for v in value if isinstance(v, dict)]
            if items:
                out[key] = items
        return out

    def _unread_targets(self, ops: dict[str, list[dict[str, Any]]]) -> set[str]:
        """
        找出"填了 page_id 但那个 id 不在预取范围内"的目标。

        page_id 无效意味着模型在引用一条它没见过的记忆 —— 那是幻觉，
        不该按它给的内容去写。
        """
        missing: set[str] = set()
        for items in ops.values():
            for item in items:
                pid = item.get("page_id")
                if pid in (None, 0, ""):
                    continue
                if not self._pre.pages.resolve(pid):
                    missing.add(f"page_id={pid}")
        return missing

    def _validate_patches(self, ops: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        """
        在【写入前】试跑每个 patch。

        ## 为什么要预检而不是等写入时失败

        写入是逐条串行的。第 3 条失败时前 2 条已经落盘了，那时回给模型
        重试会导致前 2 条被重复应用。预检让"要么全对要么全不写"成为可能。

        OpenViking 同样预检（extract_loop.py:782 _validate_patch_operations）。
        """
        errors: list[dict[str, Any]] = []
        by_uri = {item.uri: item for items in self._pre.by_type.values() for item in items}

        for mtype, items in ops.items():
            schema = next((s for s in self._schemas if s.memory_type == mtype), None)
            if schema is None:
                continue
            patch_fields = {
                f.name for f in schema.fields if f.merge_op is MergeOp.PATCH
            }
            for item in items:
                uri = self._pre.pages.resolve(item.get("page_id"))
                if not uri or uri not in by_uri:
                    continue  # 新建，没有原文可匹配
                current = by_uri[uri].merge_source

                for fname in patch_fields:
                    patch = StrPatch.from_raw(item.get(fname))
                    if patch is None or not patch.blocks:
                        continue
                    try:
                        apply_str_patch(current, patch)
                    except MergeError as e:
                        errors.append(
                            {
                                "memory_type": mtype,
                                "page_id": item.get("page_id"),
                                "field": fname,
                                "search": e.search or "",
                                "actual": current[:1200],
                                "message": f"{mtype}.{fname}: {e}",
                            }
                        )
        return errors

    @staticmethod
    def _drop_failed(
        ops: dict[str, list[dict[str, Any]]], errors: list[dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        """丢掉 patch 打不上的那几条，保留其余。"""
        bad = {(e["memory_type"], e["page_id"]) for e in errors}
        return {
            mtype: [i for i in items if (mtype, i.get("page_id")) not in bad]
            for mtype, items in ops.items()
        }

    def _cap(
        self, ops: dict[str, list[dict[str, Any]]], outcome: ExtractOutcome
    ) -> dict[str, list[dict[str, Any]]]:
        """
        总条数上限。

        防的是模型把一段对话拆成 50 个"事件"。真实的一轮对话产出
        3~8 条记忆是正常的，超过 20 条说明它在拆碎片 —— 那些碎片
        召回时没有价值，只会挤占预算。
        """
        limit = settings.memory.max_items_per_extraction
        total = sum(len(v) for v in ops.values())
        if total <= limit:
            return ops

        outcome.warnings.append(f"提取出 {total} 条记忆，超过上限 {limit}，已截断")
        out: dict[str, list[dict[str, Any]]] = {}
        remaining = limit
        # 按类型名排序保证截断结果稳定，而不是依赖 dict 顺序
        for mtype in sorted(ops):
            if remaining <= 0:
                break
            take = ops[mtype][:remaining]
            if take:
                out[mtype] = take
                remaining -= len(take)
        return out


_FORMAT_ERROR = (
    "你上一次的输出无法解析为 JSON。请只输出一个合法的 JSON 对象，"
    "不要包含任何解释文字，不要用 ```json 包裹。"
)


def _split_reply(reply: Any) -> tuple[str, list[ToolCall]]:
    """
    把 llm_call 的返回拆成 (文本, 工具调用)。

    ## 为什么接受两种形状

    - `str` —— 不支持/不需要工具的调用方（含测试里的假 LLM）
    - `(text, tool_calls)` —— 支持工具的调用方

    兼容两种让 eager_prefetch 模式的调用方不必构造一个空列表，
    也让只测解析逻辑的测试能直接返回字符串。
    """
    if isinstance(reply, tuple) and len(reply) == 2:
        text, calls = reply
        return str(text or ""), list(calls or [])
    return str(reply or ""), []


def _parse_json(raw: str) -> dict[str, Any] | None:
    """
    宽容解析 LLM 输出的 JSON。

    ## 为什么要剥 markdown 围栏

    即使 prompt 明确说"不要用代码块"，模型仍然经常包一层 ```json。
    为这件事浪费一轮重试不值得 —— 剥掉它是确定性的字符串操作。
    """
    text = (raw or "").strip()
    if not text:
        return None

    if text.startswith("```"):
        # 去掉首行的 ```json 和末尾的 ```
        lines = text.splitlines()
        if len(lines) >= 2:
            body = lines[1:]
            if body and body[-1].strip().startswith("```"):
                body = body[:-1]
            text = "\n".join(body).strip()

    # 模型偶尔在 JSON 前后带一句话。取第一个 { 到最后一个 } 之间的内容。
    start, end = text.find("{"), text.rfind("}")
    if start > 0 or (end >= 0 and end < len(text) - 1):
        if 0 <= start < end:
            text = text[start : end + 1]

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    out: list[int] = []
    for item in value:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _preview(text: str, limit: int = 200) -> str:
    one_line = " ".join(str(text or "").split())
    return one_line if len(one_line) <= limit else one_line[:limit] + "…"
