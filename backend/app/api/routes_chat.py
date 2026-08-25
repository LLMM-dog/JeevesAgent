"""
会话与对话路由。
"""

import json
from typing import Any

import structlog
from app.api import deps
from app.api.schemas import (
    AnswerRequest,
    ApproveRequest,
    CancelResponse,
    ChatRequest,
    CreateSessionRequest,
    MessageListResponse,
    MessageOut,
    PatchSessionRequest,
    SessionBrief,
    SessionDetail,
    SessionListResponse,
)
from app.core import runtime_state
from app.core.exceptions import (
    AppError,
    BadRequestError,
    ConflictError,
    NotFoundError,
)
from app.infra.db.session import get_db
from app.infra.sandbox.factory import get_sandbox
from app.modules.agent import run_registry
from app.modules.agent.chat_service import ChatService
from app.modules.endpoint import service as provider_service
from app.modules.endpoint.models import Model
from app.modules.session import repo
from app.modules.session.models import Message, Session
from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

log = structlog.get_logger(__name__)
router = APIRouter()


def _brief(s: Session) -> SessionBrief:
    return SessionBrief(
        id=s.id,
        title=s.title,
        workspace_id=s.workspace_id,
        pinned=bool(s.pinned),
        message_count=s.message_count,
        last_message_at=s.last_message_at,
        created_at=s.created_at,
    )


async def _detail_with_window(db: AsyncSession, s: Session) -> SessionDetail:
    """
    带上实际生效的模型窗口。

    ## 为什么要查库

    _detail 是同步的、拿不到 db。而窗口大小取决于会话选了哪个模型
    （model_pk），没选则取 chat 功能位绑定的那个 —— 两种情况都要查。

    ## 为什么值得多一次查询

    前端要用它算上下文占用比例。前端自己猜一个 32K 默认值的话，
    实际是 128K 时进度条虚高四倍，用户以为快满了、开始手动开新会话。
    """
    d = _detail(s)
    win = 0
    try:
        m = await provider_service.resolve(
            db, purpose="chat", override_pk=s.model_pk or ""
        )
        win = m.context_window
    except AppError:
        # 没配模型时窗口未知。返回 0 让前端回落到默认值 ——
        # 这时它连对话都发不出去，进度条准不准无关紧要。
        win = 0
    d.context_window = win
    return d


def _detail(s: Session) -> SessionDetail:
    # 获取第一个智能体 ID（兼容旧的单智能体模式）
    agent_ids = s.get_agent_ids()
    agent_id = agent_ids[0] if agent_ids else ""

    return SessionDetail(
        **_brief(s).model_dump(),
        approval_mode=s.approval_mode,
        private_mode=bool(s.private_mode),
        amnesia_mode=bool(s.amnesia_mode),
        vision_mode=bool(s.vision_mode),
        stream_enabled=bool(s.stream_enabled),
        model_pk=s.model_pk or "",
        agent_id=agent_id,
    )


def _loads(v: str | None) -> Any:
    if not v:
        return None
    try:
        return json.loads(v)
    except json.JSONDecodeError:
        return None


def _msg_out(m: Message) -> MessageOut:
    return MessageOut(
        id=m.id,
        seq=m.seq,
        role=m.role,
        agent_name=m.agent_name,
        content=m.content,
        reasoning=m.reasoning,
        tool_calls=_loads(m.tool_calls),
        tool_call_id=m.tool_call_id,
        tool_name=m.tool_name,
        tool_display=_loads(m.tool_display),
        is_error=bool(m.is_error),
        refs=_loads(m.refs),
        attachments=_loads(m.attachments),
        run_id=m.run_id,
        span_id=m.span_id,
        prompt_tokens=m.prompt_tokens,
        completion_tokens=m.completion_tokens,
        created_at=m.created_at,
    )


# ─────────────────────────── sessions ───────────────────────────


@router.get("/sessions", response_model=SessionListResponse, summary="会话列表")
async def list_sessions(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    q: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> SessionListResponse:
    rows, total = await repo.list_sessions(db, page=page, size=size, q=q)
    return SessionListResponse(
        items=[_brief(s) for s in rows],
        total=total,
        page=page,
        size=size,
        pages=(total + size - 1) // size,
    )


@router.post("/sessions", response_model=SessionDetail, status_code=201, summary="新建会话")
async def create_session(
    body: CreateSessionRequest, db: AsyncSession = Depends(get_db)
) -> SessionDetail:
    s = await repo.create_session(db, workspace_id=body.workspace_id, title=body.title)
    return await _detail_with_window(db, s)


@router.get("/sessions/{session_id}", response_model=SessionDetail, summary="会话详情")
async def get_session(session_id: str, db: AsyncSession = Depends(get_db)) -> SessionDetail:
    s = await repo.get_session(db, session_id)
    # 审批模式有两份：DB 是持久化真值，runtime_state 是运行时可改状态。
    # 应用重启后 runtime_state 是空的，会回落到默认 "manual"，导致用户
    # 设过的"自动模式"界面显示 auto、实际却仍弹确认框。这里从 DB 恢复。
    runtime_state.restore_approval_mode(session_id, s.approval_mode)
    return await _detail_with_window(db, s)


@router.get("/sessions/{session_id}/memory-status", summary="记忆提取状态")
async def get_memory_extraction_status(session_id: str) -> dict[str, Any]:
    """
    获取会话的记忆提取状态。

    返回：
    {
        "extracting": bool,        // 是否正在提取
        "extraction_id": str       // 当前提取的 ID（如果正在提取）
    }

    前端可以轮询此接口（例如每 2 秒），在 UI 上显示"正在记忆..."提示。
    也可以在对话 SSE 流中推送此状态变更事件。
    """
    from app.core.runtime_state import get_memory_extraction_status

    return get_memory_extraction_status(session_id)


@router.post("/sessions/{session_id}/extract-memory", summary="手动触发记忆提取")
async def trigger_memory_extraction(
    session_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    手动触发记忆提取。

    用户可以在对话中主动点击"整理记忆"按钮来触发。

    与自动触发的区别：
    - 不检查阈值，立即提取
    - 返回详细的提取结果
    - 防止重复提取（如果正在提取中，返回 409 错误）

    返回：
    {
        "success": bool,
        "message": str,
        "extraction_id": str,
        "summary": {              // 如果成功
            "total_adds": int,
            "total_updates": int,
            "total_deletes": int,
            ...
        }
    }

    错误：
    - 409 Conflict - 该会话正在进行记忆提取
    """
    from app.modules.memory import auto_commit

    return await auto_commit.trigger_manual_extraction(db, session_id)


@router.get("/sessions/{session_id}/export", summary="导出会话")
async def export_session(
    session_id: str,
    fmt: str = Query("markdown", pattern="^(markdown|md|json)$"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    导出会话为 Markdown 或 JSON。

    ## 为什么要用 Response 而不是返回 dict

    需要设 `Content-Disposition` 让浏览器【下载】而不是显示。
    返回 dict 的话 FastAPI 会当成 JSON 响应体，浏览器直接渲染在页面上。

    ## 文件名必须用 RFC 5987 编码

    标题是模型生成的，几乎总是中文。而 HTTP 头只能放 latin-1 ——
    直接写 `filename="中文.md"` 会让 uvicorn 在编码响应头时抛
    UnicodeEncodeError，表现是 500 而错误信息完全不指向文件名。

    所以要给两个：`filename=` 放 ASCII 兜底名（老浏览器用），
    `filename*=UTF-8''<percent-encoded>` 放真名（现代浏览器优先用这个）。
    """
    from urllib.parse import quote

    from app.modules.session import export as exp

    s = await repo.get_session(db, session_id)
    # agent_name=None 表示取所有记忆线 —— 子智能体的消息也要导出，
    # 否则导出的对话里会出现"助手说要派子智能体"然后突然有了结论
    msgs = await repo.load_messages(db, session_id, agent_name=None)

    # 序列化放线程池 —— 【它是纯 CPU 的同步操作】。
    #
    # 直接在 event loop 上做的话，整个进程在这期间无法处理任何请求。
    # 而这个应用的核心是 SSE 流式对话：某人导出大会话时，所有正在
    # 进行的对话会一起卡住不吐字。
    #
    # 用户看到的是"模型突然卡死了"，而排查方向会跑到 API 端点、
    # 网络上去，完全不指向导出。
    #
    # 实测：3000 条消息（正文合计 71MB）的会话，json.dumps 阻塞
    # event loop 0.39 秒，产物 73MB。单次不致命但会累积 ——
    # 而挪到线程池的成本几乎是零。
    def _render() -> tuple[str, str, str]:
        if fmt == "json":
            return (
                json.dumps(exp.to_json(s, msgs), ensure_ascii=False, indent=2),
                "application/json; charset=utf-8",
                ".json",
            )
        return exp.to_markdown(s, msgs), "text/markdown; charset=utf-8", ".md"

    body, media, ext = await run_in_threadpool(_render)

    fname = exp.safe_filename(s.title, session_id, ext)
    # ASCII 兜底名：非 ASCII 字符全替掉，保证 latin-1 能编码
    ascii_name = fname.encode("ascii", errors="replace").decode("ascii").replace("?", "_")
    disp = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(fname)}"

    return Response(
        content=body,
        media_type=media,
        headers={"Content-Disposition": disp},
    )


async def _check_model_pk(db: AsyncSession, raw: str) -> str:
    """
    校验会话选的模型。空串 = 回到功能位绑定的默认模型。

    ## 为什么禁用的模型要拒绝

    禁用的模型不出现在切换菜单里，所以能传上来说明前端拿的是过期
    数据。这时报错比静默接受好 —— 否则用户以为切成功了，
    实际下一轮又回到默认模型，而没有任何提示。
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    m = (await db.execute(select(Model).where(Model.id == raw))).scalars().first()
    if m is None:
        raise BadRequestError("模型不存在", code="model_not_found")
    if not m.enabled:
        raise BadRequestError(
            f"{m.display_name or m.model_id} 已被禁用，先在设置里启用它",
            code="model_disabled",
        )
    return raw


@router.patch("/sessions/{session_id}", response_model=SessionDetail, summary="改会话")
async def patch_session(
    session_id: str, body: PatchSessionRequest, db: AsyncSession = Depends(get_db)
) -> SessionDetail:
    s = await repo.get_session(db, session_id)
    # 只更新请求体里出现的字段（model_fields_set 区分"没传"和"传了 null"）
    data = body.model_dump(exclude_unset=True)

    if "title" in data and data["title"] is not None:
        s.title = data["title"]
    if "workspace_id" in data and data["workspace_id"] is not None:
        ws = await repo.get_workspace(db, data["workspace_id"])
        if ws.id != s.workspace_id:
            s.workspace_id = ws.id
    if "pinned" in data and data["pinned"] is not None:
        s.pinned = 1 if data["pinned"] else 0
    if "model_pk" in data and data["model_pk"] is not None:
        s.model_pk = await _check_model_pk(db, data["model_pk"])
    if "approval_mode" in data and data["approval_mode"] is not None:
        s.approval_mode = data["approval_mode"]
        # 必须【立即】对正在运行的 run 生效。走模块级 dict 而非 ContextVar ——
        # ContextVar 在 task 创建时快照，已运行的 task 读不到新值，
        # 用户会觉得"我都切成自动了它还在弹框"。
        runtime_state.set_approval_mode(session_id, data["approval_mode"])
    # 开视觉模式前先确认模型支持。
    #
    # 不拦的话用户开了开关、发了图，得到的是上游 400，而错误信息通常是
    # "Invalid content type" 这类 —— 完全不指向"你的模型不支持图片"。
    # 排查方向会跑到网络、图片格式、base64 编码上去。
    #
    # 在这里拦掉，把真因和下一步动作直接说清。
    if data.get("vision_mode"):
        model = await provider_service.resolve(db, purpose="chat")
        if not model.supports_vision:
            raise BadRequestError(
                "当前对话模型未确认支持图片输入",
                code="vision_unverified",
                hint=(
                    f"模型 {model.model_id} 的图片能力尚未核验，或核验结果为不支持。"
                    "去设置页对该模型点「核验视觉」，通过后才能开启此开关"
                ),
            )

    for flag in ("private_mode", "amnesia_mode", "vision_mode", "stream_enabled"):
        if flag in data and data[flag] is not None:
            setattr(s, flag, 1 if data[flag] else 0)

    if "agent_id" in data and data["agent_id"] is not None:
        # agent_id 是单个智能体 ID（前端传的），但数据库存的是 agent_ids（JSON 数组）
        # 切换智能体 = 替换整个列表为单个智能体
        agent_id = data["agent_id"]
        if agent_id:
            # 设置为单个智能体
            s.set_agent_ids([agent_id])
        else:
            # 清空智能体列表
            s.set_agent_ids([])

    await db.commit()
    return await _detail_with_window(db, s)


@router.delete("/sessions/{session_id}", summary="删除会话")
async def delete_session(session_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, bool]:
    # 删除会话记忆（events/entities 等 session 级记忆）。
    #
    # 记忆在文件系统、会话在 DB，两者独立。记忆删除失败不该阻断会话删除 ——
    # 残留的记忆文件只是占空间，会话删不掉才是 bug。
    from app.modules.memory import service as memory_service

    try:
        await memory_service.drop_session(session_id, db=db)
    except Exception as e:  # noqa: BLE001
        log.warning("memory_session_drop_failed", session=session_id, err=str(e)[:200])

    await repo.delete_session(db, session_id)

    # 清掉该会话的沙箱资源。
    #
    # Docker 后端下这是删容器 —— 不删的话每个删掉的会话都留一个容器
    # 在跑（保活命令不会自己停），跑一天宿主上几十个容器占着内存。
    #
    # 放在删会话【之后】：删库失败时不该白清容器。
    # 而清理失败不该让接口报错 —— 会话已经删了，返回 500 会让前端
    # 以为没删成功而重试。
    try:
        sandbox = await get_sandbox()
        await sandbox.cleanup_session(session_id)
    except Exception as e:  # noqa: BLE001
        log.warning("sandbox_cleanup_failed", session=session_id, err=str(e)[:200])

    return {"ok": True}


@router.post("/sessions/batch-delete", summary="批量删除会话")
async def batch_delete_sessions(
    body: dict[str, list[str]],
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    批量删除会话。

    请求体：
    {
        "session_ids": ["ses_xxx", "ses_yyy", ...]
    }

    响应：
    {
        "total": 10,
        "succeeded": ["ses_xxx", "ses_yyy", ...],
        "failed": [{"session_id": "ses_zzz", "error": "..."}],
        "not_found": ["ses_aaa", ...]
    }

    注意：
    - 会话删除会级联删除所有消息（ON DELETE CASCADE）
    - 会清理运行时状态和沙箱资源
    - 部分失败不会影响其他会话的删除
    """
    session_ids = body.get("session_ids", [])
    if not session_ids:
        raise BadRequestError("session_ids 不能为空")

    if len(session_ids) > 100:
        raise BadRequestError("单次最多删除 100 个会话")

    # 批量删除会话（数据库层面）
    result = await repo.delete_sessions_batch(db, session_ids)

    # 删除会话记忆 + 清理沙箱资源（只处理成功删除的）
    from app.modules.memory import service as memory_service

    sandbox = await get_sandbox()
    for session_id in result["succeeded"]:
        try:
            await memory_service.drop_session(session_id, db=db)
        except Exception as e:  # noqa: BLE001
            log.warning("memory_session_drop_failed", session=session_id, err=str(e)[:200])
        try:
            await sandbox.cleanup_session(session_id)
        except Exception as e:  # noqa: BLE001
            log.warning("sandbox_cleanup_failed", session=session_id, err=str(e)[:200])

    return result


@router.get(
    "/sessions/{session_id}/messages",
    response_model=MessageListResponse,
    summary="会话消息（不分页）",
)
async def list_messages(
    session_id: str,
    agent_name: str = Query("", description='智能体记忆线，"*" 表示全部'),
    db: AsyncSession = Depends(get_db),
) -> MessageListResponse:
    await repo.get_session(db, session_id)
    rows = await repo.load_messages(
        db, session_id, agent_name=None if agent_name == "*" else agent_name
    )

    # 归档水位线：前端用它跳过已归档消息，恢复"归档后的上下文占用"。
    # 归档后的实际上下文 = system + watermark 之后的消息 + overview 摘要，
    # 而历史消息的 prompt_tokens 是"发出时的上下文"（归档前的大值），已过时。
    watermark = -1
    try:
        from app.modules.memory.commit import get_latest_archive_summary

        latest = await get_latest_archive_summary(session_id)
        if latest is not None:
            watermark = latest.last_seq
    except Exception:  # noqa: BLE001
        pass

    return MessageListResponse(
        items=[_msg_out(m) for m in rows], watermark=watermark
    )


@router.delete(
    "/sessions/{session_id}/messages/{message_id}", summary="从该消息处截断（用于重发）"
)
async def truncate_messages(
    session_id: str, message_id: str, db: AsyncSession = Depends(get_db)
) -> dict[str, int]:
    """
    截断删除：删掉该消息及其之后的全部消息。

    ## 为什么要拦正在运行的 run

    流式生成过程中删历史，会让那个 run 继续往【已被删掉的会话历史】里
    写消息 —— 结果是删了一半又冒出来几条，seq 还可能和保留的消息撞上。

    表现很怪：用户点了"从这里重发"，界面上旧消息先消失、然后又蹦回来
    几条，而重发的新内容夹在中间。

    POST /api/chat 已经有这个检查（chat_service.py:135），截断也必须有 ——
    否则用户只要在流没结束时点重发就能撞上。
    """
    active = run_registry.active_run_of(session_id)
    if active is not None:
        raise ConflictError(
            "该会话正在生成回复，无法截断历史",
            code="run_in_progress",
            hint="先停止当前生成，再从这条消息重发",
        )
    n = await repo.truncate_from(db, session_id, message_id)
    return {"deleted_count": n}


# ─────────────────────────── chat ───────────────────────────


@router.post("/chat", summary="对话（SSE 流式）")
async def chat(
    body: ChatRequest, service: ChatService = Depends(deps.get_chat_service)
) -> StreamingResponse:
    """
    必须用 POST —— 请求体里有消息内容、引用列表、附件 ID。
    前端因此【不能用 EventSource】（它只能发 GET），必须 fetch + getReader。

    ## 两阶段的原因

    prepare() 必须在构造 StreamingResponse 【之前】await ——
    校验写在生成器里的话，函数体要等 FastAPI 开始迭代才执行，
    而那时响应头已发出，raise 无法变成 400/404/409，
    客户端只会看到 "peer closed connection without sending complete
    message body"，完全不指向真实原因。
    """
    prep = await service.prepare(
        session_id=body.session_id,
        content=body.content,
        refs=body.refs,
        images=body.images,
        agent_id=body.agent_id,
    )
    return StreamingResponse(
        service.stream(prep),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 关掉 nginx 等反代的缓冲，否则事件会被攒起来批量发出，
            # 流式效果完全消失
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/sessions/{session_id}/active-run", summary="这个会话有没有正在跑的 run")
async def active_run(session_id: str) -> dict[str, object] | None:
    """
    返回 {"run_id": ...} 或 null。

    ## 为什么需要这个接口

    用户在 auto 模式下切走会话时，服务端的 run 会继续跑完（那是有意的 ——
    他要的就是"让它自己跑"）。但切回来时前端只会 listMessages，
    看到的是切走那一刻的历史，之后再没有新内容。

    而后台其实一直在写库。用户以为卡死了，一发消息还会撞 409
    （"该会话已有正在进行的对话"，且被前端误报成"连接中断"）。

    有了这个接口，前端切回来能知道"还在跑"，于是锁上输入框、
    显示提示、轮询拉增量。

    ## 为什么不返回更多信息（进度、当前工具等）

    那些都在事件流里，而事件流已经随着上一个连接的 detach 消失了。
    这里能诚实给出的只有"在跑 / 不在跑"。多返回一个猜测的进度
    比不返回更糟。
    """
    rid = run_registry.active_run_of(session_id)
    return {"run_id": rid} if rid else None


@router.post("/runs/{run_id}/cancel", response_model=CancelResponse, summary="取消生成")
async def cancel_run(run_id: str) -> CancelResponse:
    """
    幂等：已结束的 run 重复调用返回 200 而非报错 —— 用户可能连点两次。
    """
    handle = run_registry.get(run_id)
    if handle is None:
        raise NotFoundError("run 不存在", code="run_not_found")
    run_registry.cancel(run_id)
    return CancelResponse(run_id=run_id, status="cancelled")


@router.post("/runs/{run_id}/approve", summary="审批工具调用")
async def approve(run_id: str, body: ApproveRequest) -> dict[str, bool]:
    handle = run_registry.get(run_id)
    if handle is None:
        raise NotFoundError("run 不存在", code="run_not_found")
    ok = runtime_state.resolve_approval(handle.session_id, body.call_id, body.approved)
    if not ok:
        # 不幂等：重复审批同一个 call_id 返回 409。
        # 超时后再来审批也是 409 —— 超时已被视为拒绝，工具已经执行完了。
        raise ConflictError("该审批已完成或已超时", code="run_already_finished")
    return {"ok": True}


@router.post("/runs/{run_id}/answer", summary="回答交互提问")
async def answer(run_id: str, body: AnswerRequest) -> dict[str, bool]:
    handle = run_registry.get(run_id)
    if handle is None:
        raise NotFoundError("run 不存在", code="run_not_found")
    value: Any = body.selected if body.selected is not None else body.answer
    ok = runtime_state.resolve_interact(handle.session_id, body.call_id, value)
    if not ok:
        raise ConflictError("该提问已回答或已超时", code="run_already_finished")
    return {"ok": True}
