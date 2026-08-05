"""
Todo 工具。

Todo 是【给用户看进度的】，不是给模型做规划的。模型自己会规划（思维链里就在
规划），Todo 的价值在于把规划外化成用户可见、可验收的清单。

所以粒度应该是用户关心的粒度："实现登录接口"是一条，
不是"读文件/写文件/跑测试"三条。
"""

from typing import Any

import structlog
from sqlalchemy import delete, select

from app.core.events import Ev, emit
from app.core.ids import todo_id as new_todo_id
from app.core.time import now_ms
from app.modules.agent.tools.base import ToolContext, ToolResult
from app.modules.todo.models import Todo

log = structlog.get_logger(__name__)

_STATUSES = ("pending", "in_progress", "completed", "cancelled")
_PRIORITIES = ("high", "medium", "low")


def _stats(rows: list[Todo]) -> dict[str, int]:
    out = {"total": 0, "pending": 0, "in_progress": 0, "completed": 0, "cancelled": 0}
    for r in rows:
        # cancelled 不算进分母
        if r.status != "cancelled":
            out["total"] += 1
        out[r.status] = out.get(r.status, 0) + 1
    return out


def _serialize(rows: list[Todo]) -> list[dict[str, Any]]:
    return [
        {
            "id": r.id,
            "content": r.content,
            "status": r.status,
            "priority": r.priority,
            "order_index": r.order_index,
            "archived_at": r.archived_at,
            "created_at": r.created_at,
        }
        for r in rows
    ]


async def load_active(ctx_db: Any, session_id: str) -> list[Todo]:
    stmt = (
        select(Todo)
        .where(Todo.session_id == session_id, Todo.archived_at.is_(None))
        .order_by(Todo.order_index)
    )
    return list((await ctx_db.execute(stmt)).scalars())


async def emit_updated(db: Any, session_id: str) -> None:
    rows = await load_active(db, session_id)
    await emit(Ev.TODO_UPDATED, items=_serialize(rows), stats=_stats(rows))


class TodoWriteTool:
    name = "todo_write"
    description = (
        "写入任务清单（全量替换：传入的列表会覆盖当前会话的全部任务）。\n"
        "在开始一个需要 3 步以上的任务时，先用本工具列出计划。\n"
        "每完成一步立即更新状态——不要等全部做完才一次性标记，"
        "否则用户看到的进度会一直是 0% 然后突然 100%，等于没有。\n"
        "只列用户关心的步骤，不要把每个文件读写都列成一条。单条不超过 20 字。\n"
        "同一时刻只能有一个 in_progress。"
    )
    requires_approval = False

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "完整的任务列表，按执行顺序排列",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "任务描述，20 字内"},
                            "status": {
                                "type": "string",
                                "enum": list(_STATUSES),
                                "description": "默认 pending",
                            },
                            "priority": {
                                "type": "string",
                                "enum": list(_PRIORITIES),
                                "description": "默认 medium",
                            },
                        },
                        "required": ["content"],
                    },
                }
            },
            "required": ["todos"],
        }

    async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
        raw = kw.get("todos")
        if not isinstance(raw, list):
            return ToolResult(content="todos 必须是数组", is_error=True)

        items: list[dict[str, Any]] = []
        notes: list[str] = []
        seen_in_progress = False

        for i, it in enumerate(raw):
            if not isinstance(it, dict):
                continue
            content = str(it.get("content", "")).strip()
            if not content:
                continue
            status = str(it.get("status", "pending"))
            if status not in _STATUSES:
                status = "pending"
            priority = str(it.get("priority", "medium"))
            if priority not in _PRIORITIES:
                priority = "medium"

            # 一个会话同时只能有一个 in_progress。多余的降为 pending 并【告知模型】,
            # 不报错 —— 这是模型的常见失误，纠正它比让它重试有效。
            if status == "in_progress":
                if seen_in_progress:
                    status = "pending"
                    notes.append(f"「{content}」已降为 pending（同时只能有一个进行中）")
                else:
                    seen_in_progress = True

            items.append(
                {"content": content, "status": status, "priority": priority, "order_index": i}
            )

        if not items:
            return ToolResult(content="没有有效的任务项", is_error=True)

        # 按 content 匹配已有记录来保留 id 和 created_at ——
        # 这样前端不会看到条目闪烁重建
        existing = await load_active(ctx.db, ctx.session_id)
        by_content = {r.content: r for r in existing}

        await ctx.db.execute(
            delete(Todo).where(Todo.session_id == ctx.session_id, Todo.archived_at.is_(None))
        )

        for item in items:
            old = by_content.get(item["content"])
            ctx.db.add(
                Todo(
                    id=old.id if old else new_todo_id(),
                    session_id=ctx.session_id,
                    content=item["content"],
                    status=item["status"],
                    priority=item["priority"],
                    order_index=item["order_index"],
                    created_at=old.created_at if old else now_ms(),
                )
            )
        await ctx.db.commit()

        rows = await load_active(ctx.db, ctx.session_id)
        await emit_updated(ctx.db, ctx.session_id)

        st = _stats(rows)
        summary = f"已更新任务清单：{st['completed']}/{st['total']} 完成"
        if notes:
            summary += "\n" + "\n".join(notes)
        lines = [
            f"{'✓' if r.status == 'completed' else '▸' if r.status == 'in_progress' else '·'} {r.content}"
            for r in rows
        ]
        return ToolResult(
            content=summary + "\n" + "\n".join(lines),
            display={"items": _serialize(rows), "stats": st},
        )


class TodoReadTool:
    name = "todo_read"
    description = (
        "读取当前会话的任务清单。"
        "多数情况不需要调用它——清单就在上下文里。"
        "它的用处是在上下文被压缩之后重新确认进度。"
    )
    requires_approval = False

    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
        rows = await load_active(ctx.db, ctx.session_id)
        if not rows:
            return ToolResult(content="当前没有任务清单", display={"items": [], "stats": _stats([])})
        st = _stats(rows)
        lines = [f"[{r.status}] {r.content}（{r.priority}）" for r in rows]
        return ToolResult(
            content=f"{st['completed']}/{st['total']} 完成\n" + "\n".join(lines),
            display={"items": _serialize(rows), "stats": st},
        )
