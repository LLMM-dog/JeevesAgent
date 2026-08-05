"""
记忆工具。模型主动决定"这件事值得记"。

## 为什么给模型工具，而不是每轮自动抽取

走的是后台自动提炼（每轮对话 → asyncio.Queue → 后台 LLM Agent），
好处是用户零负担，坏处是**每轮都要额外跑一次 LLM** —— 而绝大多数轮次
没有任何值得记的东西。

这里两条路都留：工具让模型在当场就能记（它最清楚哪句话重要），
后台提炼作为兜底（`extractor.py`）。工具是主路径。

## reason 是必需参数

`update_memory` / `forget_memory` / `merge_memory` 都强制要 reason。
做成可选的话 LLM 一定会省略它 —— 而 reason 进 history，是排查
"AI 为什么以为我喜欢 X"的唯一线索。
"""

from __future__ import annotations

from typing import Any

import structlog

from app.core.exceptions import BadRequestError, NotFoundError
from app.modules.agent.tools.base import ToolContext, ToolResult
from app.modules.memory import service as memory_service

log = structlog.get_logger(__name__)


async def _is_private(ctx: ToolContext) -> bool:
    """
    这个会话是否禁止写记忆。

    ## 为什么写入侧必须单独拦

    召回侧的 amnesia_mode 拦不住写入 —— `remember` 是模型主动调的工具，
    模型看不到会话开关。实测在 private_mode 会话里它照样调了 remember
    并成功写入。

    读写是两个方向，要两处分别拦。这也是为什么 session 表上是两个字段
    而不是一个（`private_mode` 不写 / `amnesia_mode` 不读）。
    """
    from app.modules.session import repo

    try:
        session = await repo.get_session(ctx.db, ctx.session_id)
    except Exception:  # noqa: BLE001
        # 查不到会话时【按不允许写处理】。
        # 反过来（默认允许）会在异常路径上悄悄写入本该保密的内容。
        log.warning("memory_private_check_failed", session_id=ctx.session_id)
        return True
    return bool(session.private_mode)


class RememberTool:
    name = "remember"
    description = (
        "把一件值得长期记住的事写入记忆库。跨会话生效。\n"
        "\n"
        "## 什么该记\n"
        "- 用户的稳定偏好（技术栈选择、代码风格、沟通习惯）\n"
        "- 项目的长期约定（目录结构、命名规则、部署方式）\n"
        "- 用户明确说\"记住\"的事\n"
        "\n"
        "## 什么不要记\n"
        "- 当前任务的临时状态（那是 todo 的事）\n"
        "- 从文件里读到的内容（下次可以再读）\n"
        "- 你自己的推测 —— 只记用户明确表达过的\n"
        "- 一次性的问答（\"这个函数干什么\"这类）\n"
        "\n"
        "一条只记一件事。写成\"用户偏好 X，另外项目用 Y\"的话，"
        "以后想改其中一件就得重写整条。\n"
        "\n"
        "写入前会检查是否已有相似记忆，重复时会告诉你已存在的那条。"
    )
    requires_approval = False

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "记忆内容，一句话说清一件事，不超过 300 字",
                },
                "theme": {
                    "type": "string",
                    "description": (
                        "分类主题，如 技术偏好 / 项目约定 / 沟通习惯 / 个人信息。"
                        "用已有的主题名，除非确实是新类别"
                    ),
                },
            },
            "required": ["content", "theme"],
        }

    async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
        content = str(kw.get("content") or "").strip()
        theme = str(kw.get("theme") or "其他").strip()
        if not content:
            return ToolResult(content="content 不能为空", is_error=True)

        # private_mode：这个会话不写记忆。
        #
        # 【必须在工具层拦】。最初只在召回侧做了 amnesia_mode，
        # 以为写入侧不用管 —— 但 remember 是模型主动调的，它看不到
        # 会话开关，照样会调。实测 private_mode 会话里它真的写进去了。
        #
        # 返回明确文本而不是静默成功：静默的话模型以为记住了，
        # 后面可能基于"已经记下了"做后续判断。
        if await _is_private(ctx):
            return ToolResult(
                content=(
                    "当前会话开启了隐私模式，不写入长期记忆。"
                    "这件事只在本次对话内有效。"
                ),
                display={"skipped": True, "reason": "private_mode"},
            )

        # 写入前去重。
        #
        # 不做的话同一件事会被反复记 —— 尤其在长会话里，模型可能在
        # 第 3 轮和第 20 轮都觉得"用户偏好 Python"值得记。
        similar = await memory_service.find_similar(ctx.db, content)
        if similar:
            existing = similar[0]
            return ToolResult(
                content=(
                    f"已有相似记忆（{existing.id}）：{existing.content}\n"
                    "没有新建。如果新信息与它矛盾，用 update_memory 更新那条；"
                    "如果确实是不同的事，换个更具体的措辞重新调用。"
                ),
                display={"skipped": True, "existing_id": existing.id},
            )

        mem = await memory_service.create(
            ctx.db,
            content=content,
            theme=theme,
            source="tool",
            # 模型主动记的比后台自动提炼的可信 —— 它是在有明确上下文时
            # 判断的，而不是事后从消息流里猜的
            confidence=0.8,
            origin_session_id=ctx.session_id,
        )
        await ctx.db.commit()
        log.info("memory_created", memory_id=mem.id, theme=theme, source="tool")
        return ToolResult(
            content=f"已记住（{mem.id}）：[{theme}] {content}",
            display={"memory_id": mem.id, "theme": theme, "content": content},
        )


class RecallTool:
    name = "recall"
    description = (
        "主动检索长期记忆。\n"
        "\n"
        "每轮对话开始时相关记忆会自动注入，所以**通常不需要调这个工具**。\n"
        "只在下面这些情况用：\n"
        "- 自动注入的记忆不够，你需要换个关键词再找\n"
        "- 用户问\"你还记得……吗\"\n"
        "- 要修改或删除某条记忆前，先找到它的 id"
    )
    requires_approval = False

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词"},
            },
            "required": ["query"],
        }

    async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
        query = str(kw.get("query") or "").strip()
        if not query:
            return ToolResult(content="query 不能为空", is_error=True)
        hits = await memory_service.recall(ctx.db, query)
        if not hits:
            return ToolResult(content=f"没有找到与「{query}」相关的记忆")

        lines = [f"找到 {len(hits)} 条相关记忆："]
        for h in hits:
            m = h.memory
            lines.append(f"- {m.id} [{m.theme}] {m.content}")
        # 命中计数只是统计，失败不该影响这次召回的结果
        try:
            await memory_service.touch_hits(ctx.db, [h.memory.id for h in hits])
            await ctx.db.commit()
        except Exception as e:  # noqa: BLE001
            log.warning("memory_touch_failed", err=str(e))
        return ToolResult(
            content="\n".join(lines),
            display={"count": len(hits), "query": query},
        )


class UpdateMemoryTool:
    name = "update_memory"
    description = (
        "修改一条已有记忆。用在新信息与旧记忆矛盾时"
        "（比如用户之前用 Python，现在改用 Go）。\n"
        "\n"
        "**必须说明修改原因** —— 它会记进变更历史，"
        "以后排查\"这条记忆怎么变成这样的\"要靠它。\n"
        "\n"
        "不知道 id 就先用 recall 找。"
    )
    requires_approval = False

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "要改的记忆 id"},
                "content": {"type": "string", "description": "新的记忆内容"},
                "reason": {
                    "type": "string",
                    "description": "为什么改。例如「用户明确说改用 Go 了」",
                },
            },
            "required": ["memory_id", "content", "reason"],
        }

    async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
        mid = str(kw.get("memory_id") or "").strip()
        content = str(kw.get("content") or "").strip()
        reason = str(kw.get("reason") or "").strip()
        if not reason:
            return ToolResult(
                content="必须给出 reason —— 它会记进变更历史，用于以后追溯",
                is_error=True,
            )
        if await _is_private(ctx):
            return ToolResult(
                content="当前会话开启了隐私模式，不修改长期记忆。",
                display={"skipped": True, "reason": "private_mode"},
            )
        try:
            mem = await memory_service.update(
                ctx.db, mid, reason=reason, content=content,
                # 被明确纠正过的记忆更可信
                confidence=0.9,
            )
            await ctx.db.commit()
        except NotFoundError:
            return ToolResult(
                content=f"记忆 {mid} 不存在。用 recall 先找到正确的 id",
                is_error=True,
            )
        except BadRequestError as e:
            return ToolResult(content=e.message, is_error=True)
        log.info("memory_updated", memory_id=mem.id)
        return ToolResult(
            content=f"已更新（{mem.id}）：{mem.content}",
            display={"memory_id": mem.id, "content": mem.content},
        )


class ForgetMemoryTool:
    name = "forget_memory"
    description = (
        "归档一条记忆（不再参与召回）。用在记忆已过时或记错了的时候。\n"
        "\n"
        "**必须说明原因**。归档不是真删 —— 用户仍能在界面里看到并恢复，"
        "所以不用担心误删。\n"
        "\n"
        "如果只是内容需要更正，用 update_memory 而不是这个。"
    )
    requires_approval = False

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "要归档的记忆 id"},
                "reason": {"type": "string", "description": "为什么不再需要这条记忆"},
            },
            "required": ["memory_id", "reason"],
        }

    async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
        mid = str(kw.get("memory_id") or "").strip()
        reason = str(kw.get("reason") or "").strip()
        if not reason:
            return ToolResult(content="必须给出 reason", is_error=True)
        if await _is_private(ctx):
            return ToolResult(
                content="当前会话开启了隐私模式，不改动长期记忆。",
                display={"skipped": True, "reason": "private_mode"},
            )
        try:
            mem = await memory_service.archive(ctx.db, mid, reason=reason)
            await ctx.db.commit()
        except NotFoundError:
            return ToolResult(content=f"记忆 {mid} 不存在", is_error=True)
        except BadRequestError as e:
            return ToolResult(content=e.message, is_error=True)
        log.info("memory_archived", memory_id=mem.id)
        return ToolResult(
            content=f"已归档（{mem.id}）。用户仍可在设置页恢复它",
            display={"memory_id": mem.id},
        )
