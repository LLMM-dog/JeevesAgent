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
from app.modules.memory import service as memory_service
from app.modules.provider import service as provider_service
from app.modules.session import repo
from app.modules.skill import registry as skill_registry
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
    user_message_id: str
    title_empty: bool
    # 本轮的图片 data URL。只在这一轮进 LLM 请求，不进历史 ——
    # 一张图每轮重发的话，20 轮会话里第 1 轮的图会被发 20 次。
    images: list[str] = field(default_factory=list)
    # 本轮引用清单。展开在 _expand_refs 里做 ——
    # 必须真的展开，不能像 那样把 JSON 原样丢给模型
    refs: list[dict[str, Any]] = field(default_factory=list)


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

            # 预检 chat 位能否解析出模型，结果丢弃。
            # 目的是让"未配置模型"返回 400 而不是流中途的 error 事件。
            await provider_service.resolve(db, purpose="chat")

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
            work_root = session.work_dir or workspace.root_path

            # 图片校验在这里做，不在流里做。
            #
            # 放流里的话响应头已发出，报错无法变成 400 —— 用户看到的是
            # "peer closed connection"，完全不指向"图片太大"或"格式不对"。
            checked = self._check_images(images or [], session)

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
            user_message_id=user_mid,
            title_empty=not session.title,
            images=checked,
            refs=refs or [],
        )

    @staticmethod
    def _check_images(images: list[str], session: Any) -> list[str]:
        """
        校验图片并返回可用的那些。

        ## 为什么没开视觉模式时直接丢弃而不报错

        用户可能在关掉开关后才发出已经贴好的图。报错会让他丢掉整条消息
        （文字也发不出去），而丢弃图片只损失图片 —— 文字仍然送达。

        日志里记下丢了几张，排查"我的图怎么没了"时能查到。
        """
        if not images:
            return []
        if not session.vision_mode:
            log.info(
                "images_dropped_vision_off",
                session_id=session.id,
                count=len(images),
            )
            return []

        from app.modules.provider import vision

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
                    item = await asyncio.wait_for(
                        bus.get(), timeout=settings.agent.heartbeat_interval
                    )
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
    ) -> None:
        model = await provider_service.resolve(db, purpose="chat")
        registry = self._base_registry.forked()

        # 技能清单每轮都从 registry 现取，不缓存在 service 上 ——
        # 用户上传技能后应该立即可用，不需要重启也不需要新建会话。
        system_prompt = prompts.build_system_prompt(
            workspace=workspace_path,
            tool_names=registry.names(),
            skills=skill_registry.get_index().l1(),
        )

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

        with record_span(
            "agent", "main", session_id=session_id, agent_name="main", run_id=run_id
        ):
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
            )
            await loop.load_context()

            # 把本轮图片挂到最后一条 user 消息上。
            #
            # 必须在 load_context 之后做 —— row_to_msg 故意不还原 images
            #（避免历史里的图每轮重发），所以刚落库的那条读回来也是没图的。
            # 这里补上，让它只在这一轮生效。
            if images:
                for m in reversed(loop.messages):
                    if m.role == "user":
                        m.images = images
                        break

            await self._expand_refs(loop, refs, workspace_path)
            await self._inject_memories(loop, db, session_id)
            result = await loop.run()

            await emit(
                Ev.AGENT_END,
                agent_name="main",
                stop_reason=result.stop_reason,
                turns=result.turns,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
            )

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
            await self._generate_title(db, session_id)

    async def _expand_refs(
        self, loop: AgentLoop, refs: list[dict[str, Any]] | None, workspace_path: str
    ) -> None:
        """
        展开本轮引用，附加到最后一条 user 消息。

        ## 为什么必须真的展开

        这里有个坑。把 refs 序列化成
        `__refs__[JSON]__/refs__` 拼在消息尾部，后端零解析 —— 模型看到的是
        一段它没被教过的私有 JSON。它的 `macro` 引用因此完全失效
        （宏的全部价值就在正文，而正文根本没进上下文）。

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

    async def _inject_memories(
        self, loop: AgentLoop, db: AsyncSession, session_id: str
    ) -> None:
        """
        召回相关记忆，注入本轮上下文。

        ## 三个关键决定

        **1. 走 user 位，不走 system 位**

        记忆是【观察到的事实】，不是指令。放 system 位会让"用户上次用了
        Python"升格成"必须用 Python"。而且记忆是自动写入的，可信度天然低于
        人写的系统提示词 —— 把低可信内容放最高权威位置是错的。

        这和技能正文不进 system 位是同一条规则。也是这么做的
        （相关实现 用 HumanMessage 注入）。

        **2. 不落库**（persist=False）

        召回结果是每轮现算的。落库的话下一轮 load_context 会把它读回来，
        与新召回的内容并存 —— 同一条记忆在上下文里出现两次三次。

        **3. 查询只用最后一条真实用户输入**

        不用全部历史：历史里包含上一轮注入的记忆文本，拿它去检索会
        召回同一批记忆并逐轮强化。见下面的 INJECTION_MARKER 过滤。
        """
        session = await repo.get_session(db, session_id)
        # amnesia_mode：这轮不读记忆（失忆模式）。
        #
        # 与 private_mode（这轮不写）分开 —— 合成一个开关会丢掉
        # "让它记但这轮别提"这类需求。
        if session.amnesia_mode:
            log.info("memory_recall_skipped", session_id=session_id)
            return

        # 取最后一条 user 消息作为查询。
        #
        # 【必须过滤掉自己注入的那条】。不过滤会形成自反馈：注入的记忆
        # 被当成用户输入去检索，召回同一批记忆，措辞逐轮漂移。
        # 也做了这个过滤。
        query = ""
        for m in reversed(loop.messages):
            if m.role != "user":
                continue
            if m.content.startswith(memory_service.INJECTION_MARKER):
                continue
            query = m.content
            break
        if not query:
            return

        try:
            hits = await memory_service.recall(db, query)
        except Exception as e:  # noqa: BLE001
            # 召回失败不该让对话失败。记忆是增强，不是必需品。
            log.warning("memory_recall_failed", err=str(e))
            return
        if not hits:
            return

        text = memory_service.format_for_injection(hits)
        if not text:
            return

        loop.messages.append(
            Msg(role="user", content=text, agent_name=loop.agent_name)
        )
        await emit(
            Ev.MEMORY_RECALLED,
            count=len(hits),
            items=[
                {
                    "memory_id": h.memory.id,
                    "theme": h.memory.theme,
                    "content": h.memory.content,
                    "score": h.score,
                }
                for h in hits
            ],
        )
        # 命中计数。失败只记日志 —— 它是统计，不是功能。
        try:
            await memory_service.touch_hits(db, [h.memory.id for h in hits])
            await db.commit()
        except Exception as e:  # noqa: BLE001
            log.warning("memory_touch_failed", err=str(e))

    async def _generate_title(self, db: AsyncSession, session_id: str) -> None:
        """
        首轮后生成标题。失败不影响对话 —— 标题只是个便利功能。
        """
        try:
            model = await provider_service.resolve(db, purpose="title")
            rows = await repo.load_messages(db, session_id)
            convo = "\n".join(
                f"{r.role}: {r.content[:500]}" for r in rows if r.role in ("user", "assistant")
            )[:4000]
            template = prompts.load_builtin("title")
            text = prompts.render(template, conversation=convo)

            chunks: list[str] = []
            async for c in get_llm().stream_chat(
                model, [{"role": "user", "content": text}]
            ):
                if c.kind == "content":
                    chunks.append(c.text)

            # 只取第一行并去掉模型爱加的引号书名号。
            # 注意 "".splitlines() 返回 [] 而不是 [""]，
            # 所以不能直接 [0] —— 空响应时会 IndexError。
            lines = "".join(chunks).strip().strip("\"'《》").splitlines()
            title = lines[0].strip()[:40] if lines else ""
            if not title:
                return
            await repo.update_session_title(db, session_id, title)
            await emit(Ev.TITLE, session_id=session_id, title=title)
        except (AppError, ProviderError) as e:
            # 标题只是便利功能，失败不影响对话，也不该弹给用户
            log.warning("title_generation_failed", err=str(e))
