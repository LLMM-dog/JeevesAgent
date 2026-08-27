"""
对话服务。

## 事件流的生产/消费分离

生产端（agent loop 所在的 task）往 EventBus 推事件；
消费端（SSE 响应生成器）从 bus 取事件往外发。

两者分离的关键理由：SSE 连接断开时，生产端还在跑。如果生产端直接往
response 写，连接断开会让生成中途崩掉、消息不落库。分离之后连接断开
只影响消费端，生成继续跑完并正常落库 —— 用户刷新页面能看到完整结果。
"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.events import Ev, EventBus, emit, reset_bus, set_bus
from app.core.exceptions import AppError, BadRequestError, ConflictError, ProviderError
from app.core.ids import run_id as new_run_id
from app.core.time import now_ms
from app.core.trace_context import run_scope
from app.infra.llm.openai_compat import get_llm
from app.modules.agent import prompts, run_registry
from app.modules.agent import refs as ref_expander
from app.modules.agent.loop import AgentLoop
from app.modules.agent.messages import Msg
from app.modules.agent.pathguard import load_session_allowed, scoped_guard
from app.modules.agent.tools.base import ToolRegistry
from app.modules.endpoint import service as provider_service
from app.modules.session import repo
from app.modules.skill import registry as skill_registry
from app.modules.skill import state as skill_state
from app.modules.trace import writer as trace_writer
from app.modules.trace.recorder import compute_cost, record_span
from app.modules.trace.writer import RunRecord

log = structlog.get_logger(__name__)


@dataclass
class PreparedChat:
    """prepare() 的产出，交给 stream() 使用。"""

    session_id: str
    run_id: str
    workspace_path: str
    # 会话选的模型。空串 = 跟随功能位绑定
    model_pk: str
    user_message_id: str
    title_empty: bool
    # 本轮的图片 data URL。只在这一轮进 LLM 请求，不进历史 ——
    # 一张图每轮重发的话，20 轮会话里第 1 轮的图会被发 20 次。
    images: list[str] = field(default_factory=list)
    # 本轮引用清单。展开在 _expand_refs 里做 ——
    # 必须真的展开，不能像 那样把 JSON 原样丢给模型
    refs: list[dict[str, Any]] = field(default_factory=list)
    agent_id: str = ""  # 选择的智能体，空串=默认
    stream_enabled: bool = True  # 会话级流式开关


# ── 权限过滤 ──

_TOOL_PERMISSION_MAP: dict[str, str] = {
    "read_file": "read",
    "grep": "read",
    "glob": "read",
    "list_dir": "read",
    "write_file": "write",
    "edit_file": "write",
    "run_shell": "shell",
    "web_search": "network",
    "web_fetch": "network",
    "delegate_task": "subagent",
}


def _filter_tools_by_permissions(registry: "ToolRegistry", permissions: dict[str, bool]) -> None:
    """根据智能体权限，从 registry 中移除未授权的工具。"""
    for tool_name in list(registry.names()):
        category = _TOOL_PERMISSION_MAP.get(tool_name)
        if category is None:
            continue  # 不在映射表中的工具（如 todo_write、skill_manage）不受限制
        if not permissions.get(category, True):
            registry.unregister(tool_name)


def parse_extra_llm_params(text: str) -> dict[str, Any]:
    """
    解析智能体的额外 LLM 参数字符串。

    支持两种格式：
    - 完整 JSON 对象：{"thinking": {"type": "disabled"}, "temperature": 0.7}
    - key: value 多行（value 尝试按 JSON 解析，失败当纯字符串）：
          thinking: {"type": "disabled"}
          temperature: 0.7

    解析失败整段抛 ValueError —— 静默忽略会让"看起来发了其实没生效"。
    各模型的思考/采样字段不统一，这里不做抽象映射，原样交给上游。
    """
    text = (text or "").strip()
    if not text:
        return {}

    # 先试整体 JSON 对象
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    out: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"无法解析的额外参数行（缺冒号）：{line!r}")
        key, _, raw_value = line.partition(":")
        key = key.strip()
        raw_value = raw_value.strip()
        if not key:
            raise ValueError(f"额外参数缺少 key：{line!r}")
        if not raw_value:
            raise ValueError(f"额外参数缺少 value：{line!r}")
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            value = raw_value
        out[key] = value
    return out


async def _try_resolve_vision(db: AsyncSession) -> "Any | None":
    """
    解析视觉功能位（purpose="vision"）的模型。没显式配置返回 None，
    不回落 chat。

    视觉识别要么用专门配的视觉模型（识别图片 → 文本描述），要么走 chat
    模型的多模态（图片直接进请求），两者语义不同，不能混在一个回落链里。
    """
    from sqlalchemy import select

    from app.modules.endpoint import service as endpoint_service
    from app.modules.endpoint.models import ModelBinding

    b = (
        await db.execute(select(ModelBinding).where(ModelBinding.agent_name == "", ModelBinding.purpose == "vision"))
    ).scalar_one_or_none()
    if b is None:
        return None
    return await endpoint_service.resolve(db, purpose="vision")


_COMMON_FIELDS = ("ts", "span_id", "parent_span_id", "depth")


def sse_pack(event: str, data: dict[str, Any]) -> str:
    """
    编码一个 SSE 事件。

    data 必须是【单行】JSON：裸换行会被 SSE 协议当作字段分隔符，
    导致前端解析到半个 JSON。ensure_ascii=False 保留中文原样
    （转义会让体积翻倍）。

    公共字段在这里补齐。meta / done / ping 不经过 emit()（它们由 SSE 层
    直接构造），如果不在这里统一补，前端就得对这几个事件做特殊处理 ——
    而"大部分事件有 span_id、少数没有"是最容易写出漏判的形状。
    """
    payload: dict[str, Any] = {
        "ts": now_ms(),
        "span_id": None,
        "parent_span_id": None,
        "depth": 0,
        **data,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {body}\n\n"


async def _fetch_url_text(href: str) -> str:
    """
    给引用展开器用的网页抓取。

    与 web_fetch 工具共用同一套抓取逻辑（SSRF 检查、重定向逐跳校验、
    大小上限、readability 正文提取）—— 写两遍必然分叉，而其中一份漏掉
    SSRF 检查就等于没有防护。
    """
    from app.modules.web.fetch import fetch_page

    res = await fetch_page(href)
    if res.truncated:
        return f"{res.text}\n\n（原文 {res.original_bytes} 字节，已截断）"
    return res.text


class ChatService:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        base_registry: ToolRegistry,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._base_registry = base_registry

    async def prepare(
        self,
        *,
        session_id: str,
        content: str,
        refs: list[dict[str, Any]] | None = None,
        images: list[str] | None = None,
        agent_id: str = "",
    ) -> PreparedChat:
        """
        校验 + 落用户消息。【必须是普通 async 方法，不能是生成器】。

        ## 为什么不能把校验写在 stream() 里

        调用一个 async 生成器函数【不执行任何函数体代码】——
        它只返回一个生成器对象，函数体要等第一次 __anext__() 才开始跑。
        而 FastAPI 是先构造 StreamingResponse、发出响应头，之后才去迭代。

        所以写在生成器里的 raise ConflictError 发生在响应头已发出之后，
        根本无法变成 400，客户端看到的是：
            httpx.RemoteProtocolError: peer closed connection without
            sending complete message body (incomplete chunked read)

        这个报错完全不指向真实原因（未配置模型），排查方向会完全错。

        所以：路由层先 await prepare()（此时可以正常抛 HTTP 错误），
        再把 stream() 交给 StreamingResponse。
        """
        async with self._sessionmaker() as db:
            session = await repo.get_session(db, session_id)

            # 审批模式从 DB 恢复到 runtime_state。get_session 路由也会做，
            # 但发消息不一定先走那个路由（比如前端缓存了会话详情），
            # 这里兜底一次，保证审批检查读到的是用户设过的模式而非默认 manual。
            from app.core import runtime_state

            runtime_state.restore_approval_mode(session_id, session.approval_mode)

            # 预检 chat 位能否解析出模型，结果丢弃。
            # 目的是让"未配置模型"返回 400 而不是流中途的 error 事件。
            await provider_service.resolve(db, purpose="chat", override_pk=session.model_pk or "")

            if run_registry.active_run_of(session_id) is not None:
                raise ConflictError(
                    "该会话已有正在进行的对话",
                    code="run_in_progress",
                    hint="等待当前回复结束或先取消",
                )

            # 工作目录：会话自己设的优先，没设就回落到默认工作区。
            #
            # 为什么回落而不是直接拒绝：用户可能只想聊天，不碰文件。这时候没有
            # 工作目录完全正常，强制他先选一个目录才能说话是荒谬的。
            #
            # 回落到默认工作区（项目内的 workspace/）意味着：不设工作目录时
            # agent 仍能读写文件，但只能在项目自己的沙箱目录里 ——
            # 这是最小权限的合理默认。要它操作你的代码，在对话页指定工作目录。
            workspace = await repo.get_workspace(db, session.workspace_id)
            work_root = workspace.root_path

            # 图片校验在这里做，不在流里做。
            #
            # 放流里的话响应头已发出，报错无法变成 400 —— 用户看到的是
            # "peer closed connection"，完全不指向"图片太大"或"格式不对"。
            checked = self._check_images(images or [], session)

            # 图片策略检查：有图但既没配视觉模型、chat 模型又不支持图片 → 报错。
            # 报错放在发消息前（这里能变成 400），不放流里 —— 流里只能走 error
            # 事件，且响应头已发出，报错会变成"连接中断"。
            if checked and await _try_resolve_vision(db) is None:
                chat_model = await provider_service.resolve(db, purpose="chat", override_pk=session.model_pk or "")
                if not chat_model.supports_vision:
                    raise BadRequestError(
                        "当前对话模型不支持图片，且未配置视觉模型",
                        code="vision_unavailable",
                        hint=(
                            f"模型 {chat_model.model_id} 的图片能力未核验或核验为不支持，"
                            "且未在设置页绑定视觉功能位。去「功能位绑定」配一个视觉模型，"
                            "或对当前模型点「核验视觉」"
                        ),
                    )

            # 用户消息立即落库。之后的任何失败（包括流开始后的）
            # 都不会让用户的输入丢失。
            user_msg = Msg(role="user", content=content, images=checked)
            user_mid = await repo.append_message(
                db,
                session_id,
                user_msg,
                refs=refs,
                # 图片存 attachments 供前端回显。
                # 它【不参与】后续轮次的 LLM 请求 —— 见 repo.row_to_msg 的说明。
                attachments=checked or None,
            )

        return PreparedChat(
            session_id=session_id,
            run_id=new_run_id(),
            workspace_path=work_root,
            model_pk=session.model_pk or "",
            agent_id=agent_id,
            user_message_id=user_mid,
            title_empty=not session.title,
            images=checked,
            refs=refs or [],
            stream_enabled=bool(session.stream_enabled),
        )

    @staticmethod
    def _check_images(images: list[str], session: Any) -> list[str]:
        """
        校验图片并返回可用的那些。

        发图即自动识别，不再有"视觉模式开关"这一层 ——
        用户把图贴进来就是意图，这里只做格式/大小/张数校验。
        """
        if not images:
            return []

        from app.modules.endpoint import vision

        out: list[str] = []
        for url in images[: vision.MAX_IMAGES_PER_TURN]:
            decoded = vision.decode_data_url(url)
            if decoded is None:
                raise BadRequestError(
                    "图片数据格式不对（不是合法的 data URL）",
                    code="image_invalid",
                )
            mime, raw = decoded
            ok, err = vision.validate_image(raw, mime)
            if not ok:
                raise BadRequestError(err, code="image_invalid")
            out.append(url)

        if len(images) > vision.MAX_IMAGES_PER_TURN:
            # 超出的部分丢弃并告知。不报错 ——
            # 前 N 张仍然可用，让用户重发不如直接处理掉。
            log.info(
                "images_truncated",
                given=len(images),
                kept=vision.MAX_IMAGES_PER_TURN,
            )
        return out

    async def stream(self, prep: PreparedChat) -> AsyncIterator[str]:
        """
        SSE 生成器。到这里所有校验已通过，此后的错误只能走 error 事件。
        """
        session_id = prep.session_id
        run_id = prep.run_id
        user_mid = prep.user_message_id
        bus = EventBus()

        async def produce() -> None:
            """
            生成端跑在独立 task 里。用自己的 DB session ——
            与消费端共用会话对象在并发访问时会出错。
            """
            token = set_bus(bus)
            try:
                with run_scope(run_id):
                    async with self._sessionmaker() as pdb:
                        # 会话级白名单。
                        #
                        # 必须在【这个 task 内部】设置：ContextVar 在 task
                        # 创建时快照，在外面设的话 produce() 读不到 ——
                        # 表现为白名单加了却不生效。
                        #
                        # 子代理在自己的 task 里跑，会继承这里的 context，
                        # 所以它拿到的权限不会超过派它的会话。
                        allowed = await load_session_allowed(pdb, session_id)
                        with scoped_guard(allowed):
                            await self._run_agent(
                                db=pdb,
                                session_id=session_id,
                                run_id=run_id,
                                workspace_path=prep.workspace_path,
                                title_empty=prep.title_empty,
                                images=prep.images,
                                refs=prep.refs,
                                model_pk=prep.model_pk,
                                agent_id=prep.agent_id,
                                stream_enabled=prep.stream_enabled,
                            )
            except asyncio.CancelledError:
                await emit(Ev.CANCELLED, run_id=run_id, partial_saved=True)
                raise
            except AppError as e:
                log.error("run_app_error", run_id=run_id, code=e.code)
                await emit(
                    Ev.ERROR,
                    code=e.code,
                    message=e.message,
                    hint=e.hint,
                    retryable=e.status_code >= 500,
                )
            except Exception as e:
                log.exception("run_failed", run_id=run_id)
                await emit(
                    Ev.ERROR,
                    code="internal_error",
                    message=f"{type(e).__name__}: {e}",
                    hint=None,
                    retryable=True,
                )
            finally:
                reset_bus(token)
                run_registry.unregister(run_id)
                await bus.close()

        task = asyncio.create_task(produce())
        run_registry.register(run_id, session_id, task)

        # meta 必须是第一个事件 —— 前端拿到 run_id 才能启用取消按钮
        yield sse_pack(
            str(Ev.META),
            {
                "run_id": run_id,
                "session_id": session_id,
                "user_message_id": user_mid,
                "assistant_message_id": None,
            },
        )

        status = "done"
        try:
            while True:
                try:
                    item = await asyncio.wait_for(bus.get(), timeout=settings.agent.heartbeat_interval)
                except TimeoutError:
                    # 心跳。推理阶段可能 200s+ 不吐任何字节，没有心跳
                    # 会被代理/浏览器判为超时断开。
                    yield sse_pack(str(Ev.PING), {})
                    continue

                if item is None:
                    break
                if item["event"] == str(Ev.CANCELLED):
                    status = "cancelled"
                elif item["event"] == str(Ev.ERROR):
                    status = "error"
                yield sse_pack(item["event"], item["data"])
        finally:
            # 【必须 detach】。
            #
            # 这个 finally 在两种情况下执行：正常跑完，以及客户端断开
            # （用户切走会话 → 前端 abort fetch → Starlette 取消这个
            # 生成器 → GeneratorExit 走到这里）。
            #
            # 第二种情况下 produce() 还在跑（没人取消它，这是有意的：
            # 用户切走的意图是"让它在后台跑完"）。但从此刻起没人再调
            # bus.get()，队列会填满，然后下一个结构类事件在
            # `await queue.put()` 上永久阻塞 —— produce 的 finally 永不
            # 执行，run_registry 永不释放，那个会话被永久锁死。
            #
            # detach 之后 push 变成 no-op，run 继续跑完并正常写库。
            bus.detach()
            # done 一定会发，无论成功、失败还是取消。
            # 前端的"恢复输入框"挂在这里，不能挂 agent_end（子智能体也会发）。
            yield sse_pack(str(Ev.DONE), {"run_id": run_id, "status": status})

        if bus.dropped:
            log.info("events_dropped", run_id=run_id, count=bus.dropped)

    async def _run_agent(
        self,
        *,
        db: AsyncSession,
        session_id: str,
        run_id: str,
        workspace_path: str,
        title_empty: bool,
        images: list[str] | None = None,
        refs: list[dict[str, Any]] | None = None,
        model_pk: str = "",
        agent_id: str = "",
        stream_enabled: bool = True,
    ) -> None:
        # 解析智能体定义
        agent_system_prompt = ""
        agent_permissions: dict[str, bool] = {}
        agent_model: str | None = None
        extra_params: dict[str, Any] = {}
        if agent_id:
            from app.modules.agent.agent_service import get as get_agent

            agent_def = await get_agent(db, agent_id)
            if agent_def is not None:
                agent_system_prompt = agent_def.system_prompt
                agent_model = agent_def.model_id
                agent_permissions = {
                    "read": bool(agent_def.permission_read),
                    "write": bool(agent_def.permission_write),
                    "shell": bool(agent_def.permission_shell),
                    "network": bool(agent_def.permission_network),
                    "subagent": bool(agent_def.permission_subagent),
                }
                # 智能体级额外 LLM 参数。解析失败不阻断对话，只记日志 ——
                # 不能让一个手滑的格式错误让整个对话发不出去。
                if agent_def.extra_llm_params:
                    try:
                        extra_params = parse_extra_llm_params(agent_def.extra_llm_params)
                    except ValueError as e:
                        log.warning("extra_llm_params_invalid", agent_id=agent_id, error=str(e))

        # 模型：智能体绑定 > 会话选择 > 默认
        model = await provider_service.resolve(db, purpose="chat", override_pk=agent_model or model_pk)
        registry = self._base_registry.forked()

        # 按智能体权限过滤工具
        if agent_permissions:
            _filter_tools_by_permissions(registry, agent_permissions)

        system_prompt = prompts.build_system_prompt(
            workspace=workspace_path,
            tool_names=registry.names(),
            skills=await skill_state.filter_l1(db, skill_registry.get_index().l1()),
        )
        # 智能体的 system_prompt 追加在系统提示词后面
        if agent_system_prompt:
            system_prompt = system_prompt + "\n\n" + agent_system_prompt

        # ── 记忆召回 ──
        # 在对话开始前召回相关记忆，注入到系统提示词
        if settings.memory.enabled and agent_id:
            try:
                from app.modules.memory.recall import recall_memories
                from app.modules.session import repo as session_repo

                # 获取最后一条用户消息作为查询
                recent_messages = await session_repo.load_messages(db, session_id, agent_name=None, limit=10)
                user_query = ""
                for msg in reversed(recent_messages):
                    if msg.role == "user":
                        user_query = msg.content
                        break

                if user_query:
                    # 获取嵌入模型
                    try:
                        from app.modules.llm import get_embedding_model

                        embed_model = await get_embedding_model(db)
                    except Exception:
                        embed_model = None

                    # 召回记忆
                    recall_result = await recall_memories(
                        db=db,
                        session_id=session_id,
                        query=user_query,
                        agent_id=agent_id,
                        embedding_model=embed_model,
                    )

                    # 将召回的记忆注入到系统提示词
                    if recall_result.rendered:
                        system_prompt = (
                            system_prompt
                            + "\n\n# 相关记忆\n\n"
                            + recall_result.rendered
                            + "\n\n---\n\n请基于以上记忆回答用户问题。"
                        )
            except Exception as e:
                # 召回失败不影响对话，记录日志
                log.warning("memory_recall_failed", session_id=session_id, error=str(e))

        run_started = now_ms()
        # run 行先写"running"，跑完再更新。
        #
        # 先写的理由：进程被 kill 时至少留下"这个 run 开始过但没结束"的痕迹。
        # 只在结束时写的话，崩溃的那次执行在库里完全不存在 ——
        # 而那正是最需要排查的一次。
        trace_writer.submit(
            RunRecord(
                id=run_id,
                session_id=session_id,
                started_at=run_started,
                agent_name="main",
                status="running",
            )
        )

        with record_span("agent", "main", session_id=session_id, agent_name="main", run_id=run_id):
            await emit(Ev.AGENT_START, agent_name="main", task=None)

            loop = AgentLoop(
                db=db,
                llm=get_llm(),
                model=model,
                registry=registry,
                session_id=session_id,
                run_id=run_id,
                # 取自该会话的 workspace 行，不是全局 settings ——
                # 否则多工作区静默失效
                workspace=Path(workspace_path),
                system_prompt=system_prompt,
                # 会话级流式开关 + 智能体级额外 LLM 参数
                stream_enabled=stream_enabled,
                extra_params=extra_params,
            )
            await loop.load_context()

            # 图片策略：配置了视觉功能位 → 用视觉模型识别，文本描述替代图片；
            # 否则用 chat 模型直接多模态（supports_vision 已在 prepare 检查过）。
            #
            # 必须在 load_context 之后做 —— row_to_msg 故意不还原 images
            # （避免历史里的图每轮重发），所以刚落库的那条读回来也是没图的。
            # 这里补上，让它只在这一轮生效。
            if images:
                vision_model = await _try_resolve_vision(db)
                if vision_model is not None:
                    # 视觉模型识别图片 → 文本描述 → 塞回 user 消息。
                    # chat 模型看不到原图，只看到这段文字（省 token 且不依赖 chat 的多模态）。
                    from app.modules.endpoint import vision as vision_mod

                    description = await vision_mod.describe_images(get_llm(), vision_model, images)
                    if description:
                        for m in reversed(loop.messages):
                            if m.role == "user":
                                # 用标签包裹识别结果，chat 模型能明确区分
                                # "用户说的话"和"系统注入的图片描述"。
                                suffix = f"\n\n<image_description>\n{description}\n</image_description>"
                                m.content = (m.content or "") + suffix
                                break
                else:
                    for m in reversed(loop.messages):
                        if m.role == "user":
                            m.images = images
                            break

            await self._expand_refs(loop, refs, workspace_path)
            result = await loop.run()

            await emit(
                Ev.AGENT_END,
                agent_name="main",
                stop_reason=result.stop_reason,
                turns=result.turns,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
            )

            # ── 检查是否需要自动提取记忆 ──
            # 对话完成后检查，不阻塞响应返回。
            #
            # 【必须用独立会话】。这里的 db 是 produce() 内部的 pdb，
            # 主 run 结束后 `async with self._sessionmaker() as pdb` 会
            # 关闭它。若后台任务继续用这个已关闭的会话做查询，会触发
            # IllegalStateChangeError（close() 与 _connection_for_bind()
            # 竞争）。后台任务必须开自己的会话。
            if settings.memory.enabled and agent_id:
                try:
                    from app.modules.memory import auto_commit

                    sessionmaker = self._sessionmaker

                    async def _auto_commit_later(sm: Any, sid: str) -> None:
                        try:
                            async with sm() as commit_db:
                                await auto_commit.check_and_trigger(commit_db, sid)
                        except Exception as e:
                            log.warning("auto_commit_later_failed", session_id=sid, error=str(e))

                    # 创建后台任务，不等待完成
                    asyncio.create_task(_auto_commit_later(sessionmaker, session_id))
                except Exception as e:
                    log.debug("auto_commit_check_failed", session_id=session_id, error=str(e))

        run_ended = now_ms()
        trace_writer.submit(
            RunRecord(
                id=run_id,
                session_id=session_id,
                started_at=run_started,
                ended_at=run_ended,
                duration_ms=run_ended - run_started,
                agent_name="main",
                status=("error" if result.stop_reason == "error" else "done"),
                stop_reason=result.stop_reason,
                turns=result.turns,
                input_tokens=result.prompt_tokens or None,
                output_tokens=result.completion_tokens or None,
                total_tokens=result.prompt_tokens + result.completion_tokens,
                cost_usd=compute_cost(
                    input_tokens=result.prompt_tokens,
                    output_tokens=result.completion_tokens,
                    price_in_per_1m=model.price_in_per_1m,
                    price_out_per_1m=model.price_out_per_1m,
                ),
                error=result.error or "",
            )
        )

        if result.stop_reason == "error" and result.error:
            await emit(
                Ev.ERROR,
                code="upstream_error",
                message=result.error,
                hint=None,
                retryable=True,
            )
            return

        if title_empty and result.final_text:
            await self._generate_title(db, session_id, chat_model=model)

    async def _expand_refs(self, loop: AgentLoop, refs: list[dict[str, Any]] | None, workspace_path: str) -> None:
        """
        展开本轮引用，附加到最后一条 user 消息。

        ## 为什么必须真的展开

        这里有个坑。把 refs 序列化成
        `__refs__[JSON]__/refs__` 拼在消息尾部，后端零解析 —— 模型看到的是
        一段它没被教过的私有 JSON。它的技能引用因此完全失效
        （技能的全部价值就在正文，而正文根本没进上下文）。

        ## 为什么附加到 user 消息而不是单独一条

        引用是用户这句话的一部分（"帮我看看这个文件" + 文件本身），
        拆成两条消息会让"这个"失去指代对象。

        ## 展开失败要发事件

        静默失败的话用户以为 AI 读了那个文件，而它什么都没看到 ——
        然后对回答质量产生错误归因。
        """
        if not refs:
            return
        try:
            res = await ref_expander.expand(
                refs,
                workspace=Path(workspace_path),
                registry=self._base_registry,
                # 【必须传】—— 不传的话 url 引用永远报"未配置网页抓取能力"，
                # 就退化成我批评过 那种死引用：
                # 前端能创建 chip，后端零处理，用户以为 AI 会去读那个网页。
                fetch_url=_fetch_url_text,
            )
        except (OSError, ValueError, KeyError) as e:
            # 展开整体失败不该让对话失败 —— 引用是增强，不是必需品。
            #
            # 但【只捕预期内的异常】。最初这里写的是宽泛的 except Exception，
            # 结果 `self._tools` 拼错属性名（应为 _base_registry）被吞成一条
            # warning —— 验证时表现为"引用静默不生效"，而真因是 AttributeError。
            # 宽泛捕获把编码错误伪装成了运行时降级。
            log.warning("refs_expand_failed", err=str(e))
            return

        if res.failures:
            log.info("refs_partial_failure", count=len(res.failures))

        # 事件【只发一次】，在最后统一发。
        #
        # 最初这里在有失败时先发一条 ok=0，末尾再发一条真实值 —— 部分成功时
        # 前端会先收到"全失败"再收到"1 成功 1 失败"，中间那一帧是错的
        # （实测在场景「一批里坏一个」时看到两条 refs_expanded）。
        if not res.text:
            await emit(Ev.REFS_EXPANDED, ok=0, failures=res.failures, bytes_used=0)
            return

        for m in reversed(loop.messages):
            if m.role == "user":
                # 引用【拼在用户文本之后】。
                #
                # 放前面的话模型先读到几十 KB 材料才看到问题，
                # 而"根据这些材料回答什么"是问题决定的。
                m.content = f"{m.content}\n\n{res.text}" if m.content else res.text
                break

        await emit(
            Ev.REFS_EXPANDED,
            ok=len(refs) - len(res.failures),
            failures=res.failures,
            bytes_used=res.used_bytes,
            skills=res.skills,
        )

    async def _generate_title(
        self,
        db: AsyncSession,
        session_id: str,
        *,
        fallback_text: str = "",
        chat_model: Any = None,
    ) -> None:
        """
        首轮后生成标题。

        ## 为什么要有兜底

        标题位没配模型、或者上游报错时，会话就永远停在"未命名会话"。
        侧栏里几十条都叫这个名字，等于没有标题功能 ——
        而用户根本不知道这是"标题模型没配"导致的。

        兜底用模型第一次回复的开头。它不如专门生成的标题精炼，
        但"修好 calc.py 的第 5 行"永远比"未命名会话"有用。
        """
        try:
            from sqlalchemy import select

            from app.modules.endpoint.models import ModelBinding

            # 标题位没绑定时，直接用【当前对话模型】生成标题。
            # 走 provider_service.resolve 会回落到全局 chat 绑定，但用户在
            # 这个会话里可能临时选了另一个模型 —— 那才是他看到的“当前对话模型”。
            title_binding = (
                await db.execute(
                    select(ModelBinding).where(
                        ModelBinding.agent_name == "", ModelBinding.purpose == "title"
                    )
                )
            ).scalar_one_or_none()
            if title_binding is None and chat_model is not None:
                model = chat_model
            else:
                model = await provider_service.resolve(db, purpose="title")

            rows = await repo.load_messages(db, session_id)
            convo = "\n".join(f"{r.role}: {r.content[:500]}" for r in rows if r.role in ("user", "assistant"))[:4000]
            template = prompts.load_builtin("title")
            text = prompts.render(template, conversation=convo)

            chunks: list[str] = []
            async for c in get_llm().stream_chat(model, [{"role": "user", "content": text}]):
                if c.kind == "content":
                    chunks.append(c.text)

            # 只取第一行并去掉模型爱加的引号书名号。
            # 注意 "".splitlines() 返回 [] 而不是 [""]，
            # 所以不能直接 [0] —— 空响应时会 IndexError。
            lines = "".join(chunks).strip().strip("\"'《》").splitlines()
            title = lines[0].strip()[:40] if lines else ""
            if title:
                await repo.update_session_title(db, session_id, title)
                await emit(Ev.TITLE, session_id=session_id, title=title)
                return
            # 模型返回空 —— 走兜底，而不是留着"未命名会话"
            log.info("title_empty_response_fallback")
        except (AppError, ProviderError) as e:
            # 标题只是便利功能，失败不影响对话，也不该弹给用户。
            # 但【要走兜底】：不走的话侧栏里全是"未命名会话"，
            # 而用户不知道那是标题模型没配导致的。
            log.warning("title_generation_failed", err=str(e))

        await self._fallback_title(db, session_id, fallback_text)

    async def _fallback_title(self, db: AsyncSession, session_id: str, text: str) -> None:
        """
        用模型回复的开头当标题。

        ## 取多少

        20 个字。侧栏宽度大约能显示这么多，再长会被截断成省略号 ——
        那时多存的部分只是白占地方。

        ## 为什么按标点截断

        直接切 20 个字会切在词中间（"修好 calc.py 的第 5 行代码里的错"
        切成"…代码里的"）。碰到句号问号这类停顿点就停，
        读起来是完整的一小句。

        ## 为什么要去掉换行和代码块标记

        回复常常以 ```python 或者一个列表开头。那些符号当标题毫无信息，
        而它们会把真正有用的文字挤到 20 字之外。
        """
        raw = (text or "").strip()
        if not raw:
            return

        # 去掉代码块围栏和 markdown 标记开头
        cleaned: list[str] = []
        for line in raw.splitlines():
            s = line.strip()
            if not s or s.startswith("```"):
                continue
            s = s.lstrip("#*->` ").strip()
            if s:
                cleaned.append(s)
        flat = " ".join(cleaned)
        if not flat:
            return

        limit = 20
        if len(flat) <= limit:
            title = flat
        else:
            head = flat[:limit]
            # 在标点处收尾，让它读起来是完整的一小句
            cut = max(head.rfind(c) for c in "。！？；，、.!?;,")
            title = head[: cut + 1] if cut >= limit // 2 else head
        title = title.strip().rstrip("，、,")
        if not title:
            return
        await repo.update_session_title(db, session_id, title)
        await emit(Ev.TITLE, session_id=session_id, title=title)
        log.info("title_from_reply", title=title)
