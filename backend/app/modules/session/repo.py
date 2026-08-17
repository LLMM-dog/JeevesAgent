"""
会话与消息的持久化。

## 落库时机

不等图跑完再落库。每产生一条消息就写一次，且每条一个【独立短事务】：
- SQLite 同时只允许一个写事务，长事务会让流式对话期间前端拉列表卡住
- 取消和崩溃随时可能发生，只有立即落库才能保证"看到的就是存下的"
"""

import json
from typing import Any

import structlog
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.ids import message_id as new_message_id
from app.core.ids import session_id as new_session_id
from app.core.ids import workspace_id as new_workspace_id
from app.core.time import now_ms
from app.modules.agent.messages import Msg, ToolCall
from app.modules.session.models import Message, Session, Workspace

log = structlog.get_logger(__name__)


def _dumps(v: Any) -> str | None:
    """ensure_ascii=False：中文转义会让存储和传输体积翻倍。"""
    if v is None or v == [] or v == {}:
        return None
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def _loads(v: str | None, default: Any) -> Any:
    if not v:
        return default
    try:
        return json.loads(v)
    except json.JSONDecodeError:
        log.warning("db_json_parse_failed", raw=v[:200])
        return default


# ─────────────────────────── workspace ───────────────────────────


async def ensure_default_workspace(db: AsyncSession, root: str, name: str = "默认工作区") -> Workspace:
    existing = (
        await db.execute(select(Workspace).where(Workspace.is_default == 1))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    # root_path 有唯一约束，可能已存在一条非默认的同路径记录
    same_path = (
        await db.execute(select(Workspace).where(Workspace.root_path == root))
    ).scalar_one_or_none()
    if same_path is not None:
        same_path.is_default = 1
        await db.commit()
        return same_path

    ws = Workspace(id=new_workspace_id(), name=name, root_path=root, is_default=1)
    db.add(ws)
    await db.commit()
    return ws


async def get_workspace(db: AsyncSession, workspace_id: str) -> Workspace:
    ws = (
        await db.execute(select(Workspace).where(Workspace.id == workspace_id))
    ).scalar_one_or_none()
    if ws is None:
        raise NotFoundError("工作区不存在", code="workspace_not_found")
    return ws


# ─────────────────────────── session ───────────────────────────


async def create_session(
    db: AsyncSession, *, workspace_id: str | None = None, title: str = ""
) -> Session:
    if workspace_id is None:
        ws = (
            await db.execute(select(Workspace).where(Workspace.is_default == 1))
        ).scalar_one_or_none()
        if ws is None:
            raise NotFoundError("没有可用的工作区", code="workspace_not_found")
        workspace_id = ws.id
    else:
        await get_workspace(db, workspace_id)

    # last_message_at 初始化为创建时间，不能留 0。
    #
    # 列表排序是 `pinned DESC, last_message_at DESC`。留 0 的话新建的空会话
    # 排在【最后】—— 实测在 100 个会话的库里，点"新对话"建出来的会话
    # 落在第 99 位。用户被导航到这个会话，一旦离开就再也找不回来了。
    #
    # 语义上"还没有消息却有 last_message_at"略微牵强，但列表要的是
    # "最近活跃时间"，而刚创建本身就是一次活跃。
    now = now_ms()
    s = Session(
        id=new_session_id(),
        title=title,
        workspace_id=workspace_id,
        last_message_at=now,
    )
    db.add(s)
    await db.commit()
    return s


async def get_session(db: AsyncSession, session_id: str) -> Session:
    s = (await db.execute(select(Session).where(Session.id == session_id))).scalar_one_or_none()
    if s is None:
        raise NotFoundError("会话不存在", code="session_not_found")
    return s


async def list_sessions(
    db: AsyncSession, *, page: int = 1, size: int = 20, q: str | None = None
) -> tuple[list[Session], int]:
    stmt = select(Session)
    count_stmt = select(func.count()).select_from(Session)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(Session.title.like(pattern))
        count_stmt = count_stmt.where(Session.title.like(pattern))

    total = (await db.execute(count_stmt)).scalar_one()
    # created_at / id 是【必需的兜底排序键】，不是可选的美化。
    #
    # now_ms() 只有毫秒精度，同一毫秒内建的会话 last_message_at 完全相同。
    # 只按 last_message_at 排的话，SQLite 返回的顺序是任意的 ——
    #
    #   - 分页会出问题：第 1 页和第 2 页可能重复或漏掉同一条记录
    #   - 列表顺序会在两次刷新之间无理由地跳动
    #
    # 实测：测试里连续建 6 个会话（同一毫秒内），断言"最新的排第一"
    # 会随机失败 —— 单独跑通过、全量跑失败，因为并行度不同导致落在
    # 同一毫秒的概率不同。
    stmt = (
        stmt.order_by(
            Session.pinned.desc(),
            Session.last_message_at.desc(),
            Session.created_at.desc(),
            Session.id.desc(),
        )
        .limit(size)
        .offset((page - 1) * size)
    )
    rows = list((await db.execute(stmt)).scalars())
    return rows, total


async def delete_session(db: AsyncSession, session_id: str) -> None:
    await get_session(db, session_id)
    # message 靠 ON DELETE CASCADE 级联（PRAGMA foreign_keys=ON 已在
    # infra/db/session.py 里对每个连接开启，否则级联静默失效）
    await db.execute(delete(Session).where(Session.id == session_id))
    await db.commit()

    from app.core.runtime_state import drop_session

    drop_session(session_id)


async def delete_sessions_batch(db: AsyncSession, session_ids: list[str]) -> dict[str, Any]:
    """
    批量删除会话。

    返回删除结果统计：成功、失败、跳过（不存在）。
    """
    from app.core.runtime_state import drop_session

    succeeded = []
    failed = []
    not_found = []

    for session_id in session_ids:
        try:
            # 检查会话是否存在
            session = await db.execute(select(Session).where(Session.id == session_id))
            if session.scalar_one_or_none() is None:
                not_found.append(session_id)
                continue

            # 删除会话（消息会级联删除）
            await db.execute(delete(Session).where(Session.id == session_id))
            succeeded.append(session_id)

            # 清理运行时状态
            drop_session(session_id)

        except Exception as e:
            log.error("session_delete_failed", session_id=session_id, error=str(e))
            failed.append({"session_id": session_id, "error": str(e)})

    await db.commit()

    log.info(
        "sessions_batch_deleted",
        total=len(session_ids),
        succeeded=len(succeeded),
        failed=len(failed),
        not_found=len(not_found),
    )

    return {
        "total": len(session_ids),
        "succeeded": succeeded,
        "failed": failed,
        "not_found": not_found,
    }


async def update_session_title(db: AsyncSession, session_id: str, title: str) -> None:
    await db.execute(update(Session).where(Session.id == session_id).values(title=title))
    await db.commit()


# ─────────────────────────── message ───────────────────────────


async def _next_seq(db: AsyncSession, session_id: str) -> int:
    """
    seq 在会话内严格递增，是唯一可靠的排序依据。

    不能靠 id（随机 base62 的字典序与生成顺序无关），
    也不能靠 created_at（同一毫秒内会产生多条：assistant + 多个 tool 结果）。
    """
    cur = (
        await db.execute(
            select(func.max(Message.seq)).where(Message.session_id == session_id)
        )
    ).scalar()
    return int(cur or 0) + 1


async def append_message(
    db: AsyncSession,
    session_id: str,
    msg: Msg,
    *,
    run_id: str | None = None,
    span_id: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    artifact_kind: str | None = None,
    artifact_path: str | None = None,
    refs: list[dict[str, Any]] | None = None,
    attachments: list[str] | None = None,
    display: dict[str, Any] | None = None,
) -> str:
    """
    写一条消息，返回 message_id。同时更新会话的冗余计数字段。

    artifact 走 upsert：每个 (session_id, agent_name) 只保留最新一版。
    """
    if msg.role == "artifact":
        # 先删旧版。DB 层有部分唯一索引兜底，但显式删除能让"替换"语义明确,
        # 且避免依赖 IntegrityError 做控制流。
        await db.execute(
            delete(Message).where(
                Message.session_id == session_id,
                Message.agent_name == msg.agent_name,
                Message.role == "artifact",
            )
        )

    mid = new_message_id()
    seq = await _next_seq(db, session_id)
    ts = now_ms()

    row = Message(
        id=mid,
        session_id=session_id,
        seq=seq,
        role=msg.role,
        agent_name=msg.agent_name,
        content=msg.content,
        reasoning=msg.reasoning,
        tool_calls=_dumps(
            [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in msg.tool_calls]
        ),
        tool_call_id=msg.tool_call_id,
        tool_name=msg.tool_name,
        tool_display=_dumps(display),
        is_error=1 if msg.is_error else 0,
        refs=_dumps(refs),
        attachments=_dumps(attachments),
        artifact_kind=artifact_kind,
        artifact_path=artifact_path,
        run_id=run_id,
        span_id=span_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    db.add(row)

    # 冗余字段同步更新，必须在同一事务里。
    # 只有 user/assistant 计入 message_count —— 用户看到的"24 条消息"
    # 不应该包含工具结果和摘要。
    if msg.role in ("user", "assistant"):
        await db.execute(
            update(Session)
            .where(Session.id == session_id)
            .values(last_message_at=ts, message_count=Session.message_count + 1)
        )
    else:
        await db.execute(
            update(Session).where(Session.id == session_id).values(last_message_at=ts)
        )

    await db.commit()
    msg.message_id = mid
    return mid


def row_to_msg(row: Message) -> Msg:
    """
    库行还原成 Msg。

    ## 【故意不还原 images】

    这不是遗漏。图片只在它被发送的那一轮进入请求，之后的轮次不重发。

    理由是 token 成本：一张 1024x1024 的图折算 700~1500 token，而
    历史消息【每一轮都会重新发送】。一个 20 轮的会话里，第 1 轮发的图
    会被发 20 次 —— 3 张图就能吃掉 60K token，而模型早在第一轮就已经
    描述过它们了。

    代价是模型在后续轮次"看不见"那张图，只能依赖自己第一轮的描述。
    这个取舍是对的：描述是文本，几百 token；重发是图片，每轮几千。
    需要再看一次的话让用户重新上传。

    图片的 data URL 仍然存在 message.attachments 里，前端能正常回显 ——
    只是不进 LLM 请求。
    """
    raw_calls = _loads(row.tool_calls, [])
    return Msg(
        role=row.role,  # type: ignore[arg-type]
        content=row.content,
        reasoning=row.reasoning,
        tool_calls=[
            ToolCall(id=c["id"], name=c["name"], arguments=c.get("arguments", "{}"))
            for c in raw_calls
            if isinstance(c, dict) and "id" in c and "name" in c
        ],
        tool_call_id=row.tool_call_id,
        tool_name=row.tool_name,
        is_error=bool(row.is_error),
        agent_name=row.agent_name,
        message_id=row.id,
    )


async def get_participating_agents(db: AsyncSession, session_id: str) -> list[str]:
    """
    查询会话中参与过的所有智能体（按首次出现顺序）。

    只统计 agent_name 非空的消息。空串表示用户可见主线，不算智能体。

    ## 为什么不从 session 表读

    session.agent_id 只是主智能体（创建会话时绑定的），多智能体对话时
    message.agent_name 可以是任意智能体。会话中可以随时加入/退出智能体，
    维护一个"当前活跃列表"需要额外字段和同步逻辑，不如直接从消息扫。

    ## 性能

    用 DISTINCT + MIN(seq) 去重并排序，只传输 agent_name 列，
    不加载消息正文。实测 1000 条消息耗时 <5ms（SQLite）。
    """
    from sqlalchemy import func, select

    # SELECT DISTINCT agent_name, MIN(seq) as first_seq
    # FROM message
    # WHERE session_id = ? AND agent_name != ''
    # GROUP BY agent_name
    # ORDER BY first_seq
    stmt = (
        select(Message.agent_name, func.min(Message.seq).label("first_seq"))
        .where(Message.session_id == session_id, Message.agent_name != "")
        .group_by(Message.agent_name)
        .order_by(text("first_seq"))
    )
    rows = (await db.execute(stmt)).all()
    return [r[0] for r in rows]


async def load_messages(
    db: AsyncSession,
    session_id: str,
    *,
    agent_name: str | None = "",
    after_seq: int | None = None,
    limit: int | None = None,
) -> list[Message]:
    """
    按 seq 升序拉消息。agent_name=None 表示所有记忆线。

    after_seq 用于增量读取：只返回 seq > after_seq 的消息。
    【在 SQL 层过滤】而不是取回全部再在 Python 里筛 —— 记忆提取只关心
    新消息，而一个长会话可能有上千条历史，全量传输后丢掉 99% 是纯浪费。

    limit 用于只取最近的 N 条（按 seq 升序后取尾部）—— 召回记忆时
    只需要最后几条消息找用户查询，不用把整个会话拉出来。
    """
    stmt = select(Message).where(Message.session_id == session_id)
    if agent_name is not None:
        stmt = stmt.where(Message.agent_name == agent_name)
    if after_seq is not None:
        stmt = stmt.where(Message.seq > after_seq)
    if limit is not None:
        # 取最近的 N 条：子查询降序取前 N，外层升序还原时间顺序。
        sub = select(Message.id).where(Message.session_id == session_id)
        if agent_name is not None:
            sub = sub.where(Message.agent_name == agent_name)
        if after_seq is not None:
            sub = sub.where(Message.seq > after_seq)
        sub = sub.order_by(Message.seq.desc()).limit(limit)
        stmt = stmt.where(Message.id.in_(sub))
    stmt = stmt.order_by(Message.seq)
    return list((await db.execute(stmt)).scalars())


async def truncate_from(db: AsyncSession, session_id: str, message_id: str) -> int:
    """
    删除该消息及其之后的全部消息（用于"从某条消息处重发"）。
    返回删除条数。
    """
    target = (
        await db.execute(
            select(Message).where(Message.id == message_id, Message.session_id == session_id)
        )
    ).scalar_one_or_none()
    if target is None:
        raise NotFoundError("消息不存在", code="message_not_found")

    rows = list(
        (
            await db.execute(
                select(Message.id, Message.role).where(
                    Message.session_id == session_id, Message.seq >= target.seq
                )
            )
        ).all()
    )
    visible = sum(1 for _, role in rows if role in ("user", "assistant"))

    await db.execute(
        delete(Message).where(Message.session_id == session_id, Message.seq >= target.seq)
    )

    remaining_ts = (
        await db.execute(
            select(func.max(Message.created_at)).where(Message.session_id == session_id)
        )
    ).scalar() or 0
    await db.execute(
        update(Session)
        .where(Session.id == session_id)
        .values(
            message_count=func.max(Session.message_count - visible, 0),
            last_message_at=remaining_ts,
        )
    )
    await db.commit()
    return len(rows)
