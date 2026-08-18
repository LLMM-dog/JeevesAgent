"""
Agent loop。

## 为什么 M0 不用 LangGraph

文档里的设计是 StateGraph（reason ⇄ act）。实际写下来发现：这个图只有两个
节点、一条条件边，而 LangGraph 带来的额外约束反而是负担 ——

1. journal 必须绕开图状态（节点被取消时该批状态追加全部作废），
   所以状态管理本来就是自己做的
2. 事件全部走自己的 EventBus 显式 emit，不消费 astream_events，
   于是 LangGraph 的回调/trace 机制完全用不上
   （为此要给节点加 trace=False 避免污染事件流）
3. 压缩需要【重写】整个 messages（删中段插摘要），reducer 语义做不到,
   所以 messages 本来就不能加 reducer

剩下的价值只有"图的可视化"，而代价是多一层间接。M0 先用直白的 while 循环,
等真需要多节点编排（如 plan/reflect）时再引入。

这个决定推翻了 docs/01-architecture/agent-loop.md 的"图结构"一节 ——
按项目约定，文档随后更新。langgraph 依赖暂时保留（M6 的 SubAgent 编排可能用到）。

## journal 与 messages 的分离

messages 是发给 LLM 的工作副本（会被压缩重写）；
journal 是 append-only 的完整流水，落库读它。
压缩只动 messages，绝不动 journal —— 所以库里存的永远是完整原始对话，
用户在前端能看到全部历史，即使模型自己已经"忘了"中段。
"""

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.events import Ev, emit
from app.core.exceptions import AppError, ProviderError
from app.core.runtime_state import cancel_all_pending
from app.infra.llm.openai_compat import classify_error
from app.infra.llm.port import LLMChunk, LLMPort, ResolvedModel, TokenUsage, ToolCallDelta
from app.modules.agent import compaction
from app.modules.agent.hooks import (
    AfterLlmContext,
    HookRegistry,
    OnCompactContext,
    OnMessageContext,
    ShouldStopContext,
)
from app.modules.agent.messages import (
    Msg,
    ToolCall,
    find_missing_tool_calls,
    mark_stale_file_reads,
    repair_tool_pairing,
)
from app.modules.agent.tokens import count_text, count_tools, estimate_tokens
from app.modules.agent.tools.base import (
    ToolContext,
    ToolRegistry,
    ToolResult,
)
from app.modules.endpoint import service as provider_service
from app.modules.session import repo
from app.modules.trace.recorder import record_span

log = structlog.get_logger(__name__)


@dataclass
class LoopResult:
    stop_reason: str  # final | max_turns | cancelled | error
    turns: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str | None = None
    final_text: str = ""


@dataclass
class _Accum:
    """流式增量的累积器。"""

    content: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    # index → (id, name, arguments 片段)
    calls: dict[int, dict[str, str]] = field(default_factory=dict)
    usage: TokenUsage | None = None
    finish_reason: str | None = None

    def feed(self, chunk: LLMChunk) -> None:
        if chunk.kind == "content":
            self.content.append(chunk.text)
        elif chunk.kind == "reasoning":
            self.reasoning.append(chunk.text)
        elif chunk.kind == "tool_call" and chunk.tool_call is not None:
            self._feed_call(chunk.tool_call)
        elif chunk.kind == "usage":
            self.usage = chunk.usage
        elif chunk.kind == "done":
            self.finish_reason = chunk.finish_reason

    def _feed_call(self, d: ToolCallDelta) -> None:
        # 按 index 聚合：id 和 name 只在第一个 chunk 出现，
        # 后续 chunk 只有 index + arguments 片段。
        slot = self.calls.setdefault(d.index, {"id": "", "name": "", "arguments": ""})
        if d.call_id:
            slot["id"] = d.call_id
        if d.name:
            slot["name"] = d.name
        if d.arguments_delta:
            slot["arguments"] += d.arguments_delta

    @property
    def truncated(self) -> bool:
        """
        上游因长度上限截断了输出。

        这时 tool_call 的 arguments 可能是半截 JSON，绝不能拿去执行 ——
        见 to_msg 与 _act 里的处理。
        """
        return self.finish_reason == "length"

    @property
    def is_unusable(self) -> bool:
        """
        这一轮没有产出任何可用的东西 —— 必须重试，不能当成"模型说完了"。

        判据是【没有正文，也没有工具调用】。注意思维链不算产出：

        ## 为什么思维链不算

        推理模型有两种形状：

          content 空 + reasoning 有 + tool_calls 有  → 正常（真实 deepseek 就这样）
          content 空 + reasoning 有 + tool_calls 空  → 不可用

        第二种发生在 max_tokens 在【思考阶段】就用尽时。模型想了半天，
        还没开始写答案就被截断了。

        早先的判据是"三者全空"，于是第二种情况因为 reasoning 非空而被
        判为有效，走到 `if not ai_msg.tool_calls` 分支直接返回 final ——
        而 `final_text` 保持上一轮的值（`if ai_msg.content` 保护了它不被
        空串覆盖）。结果是用户拿到一个和当前问题无关的旧答案，全程无报错。

        改成看"有没有产出"之后，两种形状都判对：有 tool_calls 就是正常
        推进，没有就是这一轮白跑了。
        """
        return not self.content and not self.calls

    def to_msg(self, agent_name: str) -> Msg:
        tool_calls: list[ToolCall] = []
        for idx in sorted(self.calls):
            slot = self.calls[idx]
            if not slot["name"]:
                # 没有名字的 call 无法执行。上游偶尔会在流被截断时留下半个 call。
                log.warning("incomplete_tool_call_dropped", index=idx, slot=slot)
                continue
            tool_calls.append(
                ToolCall(
                    # id 缺失时兜底造一个 —— 少数中转站不返回 id，
                    # 而 tool 消息必须带 tool_call_id 才能配对。
                    id=slot["id"] or f"call_{idx}",
                    name=slot["name"],
                    arguments=slot["arguments"] or "{}",
                )
            )
        return Msg(
            role="assistant",
            content="".join(self.content),
            reasoning="".join(self.reasoning) or None,
            tool_calls=tool_calls,
            agent_name=agent_name,
        )


class EmptyResponseError(Exception):
    """
    模型这一轮没有产出可用内容（没有正文、也没有工具调用）。可重试。

    truncated_thinking 区分成因：True 表示思维链有内容但被截断在思考阶段，
    这时重试要附带"少想多说"的提示，否则大概率再次耗尽预算。
    """

    def __init__(self, message: str, *, truncated_thinking: bool = False) -> None:
        super().__init__(message)
        self.truncated_thinking = truncated_thinking


class AgentLoop:
    def __init__(
        self,
        *,
        db: AsyncSession,
        llm: LLMPort,
        model: ResolvedModel,
        registry: ToolRegistry,
        session_id: str,
        run_id: str,
        workspace: Path | None = None,
        agent_name: str = "",
        system_prompt: str = "",
        depth: int = 0,
        journal_sink: list[Msg] | None = None,
        stream_enabled: bool = True,
        extra_params: dict[str, Any] | None = None,
    ) -> None:
        self.db = db
        self.llm = llm
        self.model = model
        self.registry = registry
        self.session_id = session_id
        self.run_id = run_id
        # 会话级流式开关
        self.stream_enabled = stream_enabled
        # 智能体级额外 LLM 参数（已解析）。里若含 stream 则覆盖会话开关。
        self.extra_params = dict(extra_params or {})
        # 工作区必须【显式传入】，取自该会话的 workspace 行。
        # 早先这里直接读 settings.workspace_dir，导致所有会话都用同一个目录 ——
        # 多工作区功能静默失效，而表现是"路径不在白名单内"，
        # 看起来像白名单配错了。
        self.workspace = workspace or settings.workspace_dir
        self.agent_name = agent_name
        self.system_prompt = system_prompt
        self.depth = depth
        # hook 注册中心 —— 和 ToolRegistry.hooks 是分开的实例
        self.hooks = HookRegistry()
        # 执行提醒 — 会话长了以后注入到 user 消息末尾，防止注意力衰减。
        # 不持久化、不进 journal，避免记忆污染。
        self._system_reminder = (
            "[执行规则 — 本轮强制]"
            " 一次只做一个步骤，完成并验证后再继续。"
            " 如果这一步产生了可验证的结果，立即验证。"
            " 出错时停下来分析，不要跳到下一步。"
        )
        self._reminder_delay = 3  # 前 3 轮不注入（system prompt 还紧挨着）
        # 外部持有的普通 list。节点每产生一条消息就 append 进去。
        # 普通 list 的 append 立即生效，不受任何状态提交时机影响。
        # 落库读这个，不读内部工作副本。
        self.journal: list[Msg] = journal_sink if journal_sink is not None else []
        self.messages: list[Msg] = []
        # 上一轮的真实 prompt_tokens。压缩触发要用它 ——
        # 真实 usage 只有调完 LLM 才有，所以必须跨轮存下来。
        self._last_prompt_tokens = 0
        # 打转检测状态
        self._last_call_sig: tuple[tuple[str, str], ...] | None = None
        self._repeat_count = 0
        # 压缩尝试次数。每次强制压缩后递增，用来逐步放宽 keep_tail ——
        # tail 本身太大时（粘了一大段代码）不放宽会陷入"压了但还是超"的循环。
        self._compact_attempts = 0

    # ─────────────────────────── 上下文 ───────────────────────────

    async def load_context(self) -> None:
        """
        从 DB 组装上下文，并【无条件】做一次 tool 配对修复。

        不做"是否需要修复"的判断 —— 取消、进程崩溃、断电、手动改库都会
        产生不一致，而后三者走不到取消处理代码。只修取消路径的话，
        任何非正常退出都会留下一个每次打开都 400 的会话。

        ## 归档水位线

        记忆提取会把已提取的消息归档（seq <= watermark），这些消息不再
        发回 LLM，改用归档的 .overview.md 摘要替代。这里读最新归档，
        只加载 watermark 之后的消息，并在最前面注入摘要。
        """
        # 读归档水位线（文件系统是 source of truth，不是 memory_extraction 表）
        watermark = -1
        overview = ""
        try:
            from app.modules.memory.commit import get_latest_archive_summary

            latest = await get_latest_archive_summary(self.session_id)
            if latest is not None:
                watermark = latest.last_seq
                overview = latest.overview
        except Exception as e:  # noqa: BLE001
            # 归档读失败不能拖垮对话 —— 最多是没跳过已归档消息（多占 token）
            log.warning("archive_read_failed", session_id=self.session_id, error=str(e))

        rows = await repo.load_messages(
            self.db,
            self.session_id,
            agent_name=self.agent_name,
            after_seq=watermark if watermark >= 0 else None,
        )
        msgs = [repo.row_to_msg(r) for r in rows]
        repaired, fixes = repair_tool_pairing(msgs)
        if fixes:
            log.warning("context_repaired", session_id=self.session_id, fixes=fixes)
            await self._persist_repairs(msgs, repaired)
        self.messages = repaired

        # 折叠"读取后又被修改"的 read_file 结果，防止旧文件快照污染上下文。
        stale = mark_stale_file_reads(self.messages)
        if stale:
            log.info("stale_file_reads_marked", session_id=self.session_id, count=stale)

        # 注入归档摘要，替代已归档的原始消息。summary 角色在 to_api 里
        # 映射成 user，作为"之前聊过什么"的历史放在最前面。
        if overview.strip():
            self.messages.insert(0, Msg(role="summary", content=overview))
            log.info(
                "archive_overview_injected",
                session_id=self.session_id,
                watermark=watermark,
                chars=len(overview),
            )

        # 恢复上一次的真实 prompt_tokens。
        #
        # 每个 HTTP 请求都新建一个 loop，`_last_prompt_tokens` 从 0 开始。
        # 不恢复的话【每次请求的第一轮都走估算分支】—— 而单轮对话（用户问
        # 一句、模型答一句、不调工具）恰好只有第一轮。于是"用真实 usage
        # 触发压缩"这条铁律在最常见的场景里根本没生效。
        #
        # 库里存着每条 assistant 消息的 prompt_tokens，取最后一个即可。
        for r in reversed(rows):
            if r.prompt_tokens:
                self._last_prompt_tokens = r.prompt_tokens
                break

    async def _persist_repairs(self, before: list[Msg], after: list[Msg]) -> None:
        """
        把补出来的占位 tool 消息落库。

        不落库的话每次组装都要重新补一遍，且前端看不到"这个工具调用没完成"，
        用户只会觉得那个工具卡片永远停在执行中。
        """
        existing_ids = {m.message_id for m in before if m.message_id}
        for m in after:
            if m.message_id in existing_ids:
                continue
            if m.role != "tool":
                continue
            await repo.append_message(self.db, self.session_id, m, run_id=self.run_id)

    def build_api_messages(self) -> list[dict[str, Any]]:
        """
        组装顺序固定：system → 历史（压缩后）。
        """
        out: list[dict[str, Any]] = []
        if self.system_prompt:
            out.append({"role": "system", "content": self.system_prompt})

        for m in self.messages:
            if m.role == "system":
                continue
            out.append(m.to_api())
        return out

    # ─────────────────────────── 主循环 ───────────────────────────

    async def run(self, *, max_turns: int | None = None) -> LoopResult:
        """
        max_turns 可覆盖全局值。

        子智能体需要这个：调研型任务要多轮读文件，审查型几轮就够 ——
        用同一个上限对两者都不合适。不传则用 settings.agent.max_turns。
        """
        limit = max_turns if max_turns is not None else settings.agent.max_turns
        total_prompt = 0
        total_completion = 0
        turn = 0
        warn_at = max(1, int(limit * settings.agent.warn_turn_ratio))
        warned = False
        final_text = ""

        try:
            while True:
                if turn >= limit:
                    # 达到上限不是异常，是保护。写明 stop_reason，
                    # 前端显示"已达最大轮次"。
                    log.warning("max_turns_reached", run_id=self.run_id, turns=turn)
                    return LoopResult(
                        stop_reason="max_turns",
                        turns=turn,
                        prompt_tokens=total_prompt,
                        completion_tokens=total_completion,
                        final_text=final_text,
                    )

                # 到 80% 时注入一次催促而非硬停。照抄 做法
                # （用 == 判定所以只注入一次，不会反复刷）：模型收到催促通常
                # 会收敛给结论，而硬停会留下一个半成品。
                if turn == warn_at and not warned:
                    warned = True
                    self._append(
                        Msg(
                            role="user",
                            content=(
                                "[系统提示] 已进行较多轮次。请尽快收敛：如果任务已基本完成，"
                                "直接给出结论；如果还需要多步，先说明当前进展和剩余计划。"
                            ),
                            agent_name=self.agent_name,
                        ),
                        persist=False,
                    )

                # 按阈值主动压缩，在发请求【之前】。
                # 放在之后的话，超限的那次请求已经发出并失败了。
                await self._maybe_compact()

                accum = await self._reason_with_retry()

                # ── AFTER_LLM 钩子 ──
                if self.hooks.has_hooks:
                    rejection = self.hooks.fire_after_llm(
                        AfterLlmContext(
                            accum=accum,
                            turn=turn,
                            session_id=self.session_id,
                            agent_name=self.agent_name,
                        )
                    )
                    if rejection is not None:
                        # 钩子返回 str → 注入为 user 消息继续循环
                        self._append(
                            Msg(role="user", content=rejection, agent_name=self.agent_name),
                            persist=True,
                        )
                        continue

                if accum.usage:
                    total_prompt = accum.usage.prompt_tokens or total_prompt
                    total_completion += accum.usage.completion_tokens
                    # 压缩的触发依据。必须是【上一轮的真实 prompt_tokens】——
                    # 真实 usage 只有调完才有，所以要存下来给下一轮的
                    # 压缩判断用。M1 做压缩时读这个字段。
                    self._last_prompt_tokens = accum.usage.prompt_tokens

                ai_msg = accum.to_msg(self.agent_name)
                await self._persist(
                    ai_msg,
                    prompt_tokens=accum.usage.prompt_tokens if accum.usage else None,
                    completion_tokens=accum.usage.completion_tokens if accum.usage else None,
                )
                if ai_msg.content:
                    final_text = ai_msg.content

                if not ai_msg.tool_calls:
                    # ── SHOULD_STOP 钩子 ──
                    if self.hooks.has_hooks:
                        rejection = self.hooks.fire_should_stop(
                            ShouldStopContext(
                                session_id=self.session_id,
                                agent_name=self.agent_name,
                                turn=turn,
                                final_text=final_text,
                            )
                        )
                        if rejection is not None:
                            self._append(
                                Msg(role="user", content=rejection, agent_name=self.agent_name),
                                persist=True,
                            )
                            continue

                    # 【收尾时再发一次占用】。
                    #
                    # ## 为什么不能停在最后一次 LLM 调用的 prompt_tokens
                    #
                    # 那个数字是"这一轮发出去了多少"，不含模型刚写的回复。
                    # 而用户看这个条是想知道"下一轮会发多少"——两者差一整条
                    # 回复（长回复能差几千 token）。
                    #
                    # 停在旧值的话现象是：模型写了一大段，条却几乎没动，
                    # 用户以为回复不占上下文。
                    #
                    # ## 为什么这不是估算
                    #
                    # prompt_tokens 已含系统提示词、工具定义和全部历史；
                    # completion_tokens 是这条回复的真实长度。两者都是模型
                    # 返回的值，相加正好是下一轮的 prompt —— 不需要本地估。
                    if accum.usage and accum.usage.prompt_tokens:
                        await self._emit_context_usage(
                            accum.usage.prompt_tokens + accum.usage.completion_tokens,
                            is_estimate=False,
                            specs=self.registry.to_specs() or None,
                        )
                    return LoopResult(
                        stop_reason="final",
                        turns=turn + 1,
                        prompt_tokens=total_prompt,
                        completion_tokens=total_completion,
                        final_text=final_text,
                    )

                self._check_repeating(ai_msg)
                await self._act(ai_msg, truncated=accum.truncated)
                turn += 1

        except asyncio.CancelledError:
            # 必须重新抛，但抛之前要补齐孤立的 tool_calls。
            # 这是修复的第一道；load_context 里的 repair 是兜底第二道。
            await self._fill_missing_tool_results("用户取消了该工具调用")
            log.info("run_cancelled", run_id=self.run_id)
            raise

        except ProviderError as e:
            log.error("loop_provider_error", run_id=self.run_id, code=e.code, msg=e.message)
            await self._fill_missing_tool_results("工具调用因上游错误中断")
            return LoopResult(
                stop_reason="error",
                turns=turn,
                prompt_tokens=total_prompt,
                completion_tokens=total_completion,
                error=e.message,
                final_text=final_text,
            )

        except Exception:
            # 【任何非取消的意外退出都要补齐】。
            #
            # 工具自身的异常不会到这里（registry.execute 一律转成错误文本，
            # 那是它的铁律）。能到这里的是落库失败、事件总线异常这类 ——
            # 它们发生在"assistant 已落库、部分 tool 结果已落库"的中间态,
            # 剩下的 tool_call 就成了孤立的。
            #
            # load_context 的 repair 能兜住下一次打开，但在那之前用户会看到
            # 一个永远转圈的工具卡片，而且不知道为什么。
            log.exception("loop_unexpected_error", run_id=self.run_id)
            await self._fill_missing_tool_results("工具调用因内部错误中断")
            raise

    async def _reason_with_retry(self) -> _Accum:
        """
        带重试的 LLM 调用。三类错误三种应对：

          token_exceed  → 先压缩再重试（直接重试会再超一次）
          rate_limit    → 退避后重试
          空响应        → 直接重试

        adapter 层已经处理了连接类瞬时错误的重试，这一层只管
        它处理不了的（需要改 messages 才能解决的）。
        """
        attempt = 0
        while True:
            try:
                accum = await self._reason()
                if accum.is_unusable:
                    # 区分两种成因，因为提示词不同：
                    #   截断  → 让模型少想多说
                    #   全空  → 单纯重试
                    if accum.reasoning:
                        raise EmptyResponseError(
                            "模型只输出了思维链，没有正文和工具调用"
                            f"（finish_reason={accum.finish_reason}）",
                            truncated_thinking=True,
                        )
                    raise EmptyResponseError("模型返回了空响应")
                return accum

            except (ProviderError, EmptyResponseError) as e:
                attempt += 1
                if attempt > settings.agent.max_llm_retries:
                    raise

                if isinstance(e, EmptyResponseError):
                    log.warning(
                        "unusable_response_retry",
                        run_id=self.run_id,
                        attempt=attempt,
                        reason=str(e),
                    )
                    if e.truncated_thinking:
                        # 原样重发大概率再次在思考阶段耗尽预算。
                        # 注入一次提示让它压缩思考长度 —— 与 80% 催促同一思路，
                        # 只注入一次（靠 == 判定）避免反复刷。
                        if attempt == 1:
                            self._append(
                                Msg(
                                    role="user",
                                    content=(
                                        "[系统提示] 上一次回复在思考阶段就用尽了长度上限，"
                                        "没有产出正文。请缩短思考，直接给出结论或调用工具。"
                                    ),
                                    agent_name=self.agent_name,
                                ),
                                persist=False,
                            )
                    continue

                kind = classify_error(f"{e.message} {e.hint or ''}")
                if e.code == "context_overflow" or kind == "token_exceed":
                    if not await self._force_compact():
                        # 压不动了就别再转 —— 否则会无限循环
                        raise
                    log.info("compacted_after_overflow", run_id=self.run_id, attempt=attempt)
                    continue

                # 其余错误 adapter 已经重试过了，到这里说明是永久错误
                raise

    async def _compact_on_request(self, reason: str) -> dict[str, object]:
        """
        compact_context 工具的执行入口。

        ## 为什么不复用 _maybe_compact

        那个先看阈值（涨到 75% 才动手），而主动压缩的整个意义就是
        "不等阈值"。模型判断"调研阶段结束了"时上下文可能只用了 40%，
        走 _maybe_compact 会直接返回、什么都不做，而工具会报告"已压缩"——
        一个静默的谎。

        ## 为什么 urgent=True

        urgent 在这里的含义是"别因为候选集小就拒绝"。模型明确要求压缩时
        应该照做 —— 它比阈值更清楚哪些内容已经没用。真的压不动会返回
        compacted=False，工具那边如实告诉模型。
        """
        specs = self.registry.to_specs() or None
        before = estimate_tokens([m.to_api() for m in self.messages], specs)

        model = await self._resolve_compact_model()
        out = await compaction.compact(
            messages=self.messages,
            llm=self.llm,
            model=model,
            agent_name=self.agent_name,
            tool_specs=specs,
            tail_token_budget=self._tail_budget(),
            urgent=True,
        )
        if out is None:
            log.info("compact_on_request_skipped", reason=reason)
            return {
                "compacted": False,
                "reason": "没有足够的历史可压缩（最近几轮和当前成果不参与压缩）",
            }

        new_messages, summary = out
        victim_count = max(1, len(self.messages) - len(new_messages) + 1)
        self.messages = new_messages
        await repo.append_message(
            self.db, self.session_id, summary, run_id=self.run_id
        )
        self.journal.append(summary)
        # 压缩后上一轮的 usage 不再代表当前上下文大小，清掉。
        # 不清的话下一轮会立刻再触发一次被动压缩。
        self._last_prompt_tokens = 0

        after = estimate_tokens([m.to_api() for m in self.messages], specs)

        # 【必须重新发一次 context_usage】。
        #
        # 不发的话界面上的占用条还停在压缩前的数字，而用户刚刚看到
        # "已压缩"的提示 —— 两者矛盾，他会以为压缩没生效。
        #
        # 这是估算值（本地 tiktoken），下一轮真实 usage 回来会覆盖它。
        await self._emit_context_usage(after, is_estimate=True, specs=specs)

        log.info(
            "compact_on_request",
            reason=reason,
            before=before,
            after=after,
            victims=victim_count,
        )
        return {
            "compacted": True,
            "victim_count": victim_count,
            "before_tokens": before,
            "after_tokens": after,
        }

    async def _force_compact(self) -> bool:
        """
        强制压缩一次，返回是否真的压缩了。

        用在"上游已经报了上下文超限"的错误处理路径上。此时不看阈值 ——
        阈值是预防，这里是已经撞墙了。

        返回 False 时上层会把原始的 overflow 错误如实抛出。那个错误比
        "压缩失败"对用户更有信息量。

        ## 压缩时逐步放宽 keep_tail

        第一次用配置的 keep_tail_turns。如果压完还是超限（上层会再调一次），
        就减少保留的轮次。tail 本身太大时（比如粘了一大段代码进来）
        不放宽就会陷入"压了但还是超"的循环。
        """
        keep = max(0, settings.agent.keep_tail_turns - self._compact_attempts)
        self._compact_attempts += 1

        model = await self._resolve_compact_model()
        out = await compaction.compact(
            messages=self.messages,
            llm=self.llm,
            model=model,
            agent_name=self.agent_name,
            keep_tail_turns=keep,
            tool_specs=self.registry.to_specs() or None,
            tail_token_budget=self._tail_budget(),
            # 上游已经报了超限，此时"压两条也比 400 好"
            urgent=True,
        )
        if out is None:
            return False

        new_messages, summary = out
        old_messages = self.messages  # 保存压缩前的消息，用于钩子的 token 估算
        self.messages = new_messages
        # summary 要落库 —— 否则下次打开会话时重新组装上下文，
        # 压缩成果丢失，又会立刻撞一次超限。
        # 注意只落 summary，不删原始消息：journal 保持完整，
        # 用户在前端仍能看到全部历史。
        await repo.append_message(
            self.db, self.session_id, summary, run_id=self.run_id
        )
        self.journal.append(summary)

        # ── ON_COMPACT 钩子 ──
        if self.hooks.has_hooks:
            specs = self.registry.to_specs() or None
            before_est = estimate_tokens(old_messages, specs)
            after_est = estimate_tokens(new_messages, specs)
            self.hooks.fire_on_compact(
                OnCompactContext(
                    session_id=self.session_id,
                    agent_name=self.agent_name,
                    before_tokens=before_est,
                    after_tokens=after_est,
                )
            )

        return True

    def _tail_budget(self) -> int:
        """
        tail 最多占多少 token。

        ## 为什么必须有这个上限

        `keep_tail_turns` 是按【轮次】数的，但每轮大小差异巨大。实测撞到过：
        keep=4、每轮 3000 token 的长文 → tail 自己 12000 token，
        而窗口只有 4200。结果压缩每轮触发、每轮因"候选集太少"放弃，
        上下文一路涨到窗口的 221%。

        取窗口的 40%：剩下的要留给 system 提示词、工具定义（实测 1400+）、
        摘要本身，以及这一轮的生成。全给 tail 的话压缩完还是超。
        """
        return int(self.model.context_window * settings.agent.tail_budget_ratio)

    async def _resolve_compact_model(self) -> ResolvedModel:
        """
        取 compact 功能位的模型，没绑定则回落到当前对话模型。

        ## 为什么回落到 chat 而不是随便找个便宜的

        直觉上压缩是"简单任务"，配便宜模型省钱。但 compact 错一次
        影响【整个会话往后的全部推理】—— 摘要丢了关键约定，模型后面
        所有决策都基于错误前提，而且不会报错。

        enrich 类任务错一次只影响那一块内容，compact 不是。
        所以没绑定时回落到 chat 位，而不是找个最便宜的。
        """
        try:
            return await provider_service.resolve(self.db, purpose="compact")
        except AppError:
            log.info("compact_model_fallback_to_chat", run_id=self.run_id)
            return self.model

    async def _maybe_compact(self) -> None:
        """
        按阈值主动压缩。在每次 LLM 调用【之前】检查。

        ## 为什么用上一轮的真实 usage

        本地估算与真实值在有工具定义、长 system、图片时可差 20% 以上。
        估高了白压缩（丢信息），估低了直接 400。

        首轮没有真实 usage 时用估算，但阈值打八折做保守判断
        （estimate_safety_ratio）。

        ## 为什么在调用前而不是调用后

        调用后检查的话，超限的那一次请求已经发出去并且失败了。
        调用前检查能避免那次白跑。
        """
        window = self.model.context_window
        if window <= 0:
            return

        if self._last_prompt_tokens > 0:
            used = self._last_prompt_tokens
            threshold = window * settings.agent.compact_trigger_ratio
        else:
            # 没有真实 usage，用估算并收紧阈值
            used = estimate_tokens(
                self.build_api_messages(), self.registry.to_specs() or None
            )
            threshold = (
                window
                * settings.agent.compact_trigger_ratio
                * settings.agent.estimate_safety_ratio
            )

        if used < threshold:
            return

        log.info(
            "compaction_triggered",
            run_id=self.run_id,
            used=used,
            window=window,
            threshold=int(threshold),
            is_estimate=self._last_prompt_tokens <= 0,
        )
        model = await self._resolve_compact_model()
        out = await compaction.compact(
            messages=self.messages,
            llm=self.llm,
            model=model,
            agent_name=self.agent_name,
            tool_specs=self.registry.to_specs() or None,
            tail_token_budget=self._tail_budget(),
            # 已经超过窗口时不能再挑三拣四 —— 实测遇到过候选集 3 条、
            # min_victims 4 于是拒绝压缩，上下文涨到窗口 221%
            urgent=used >= window,
        )
        if out is None:
            return

        new_messages, summary = out
        self.messages = new_messages
        await repo.append_message(
            self.db, self.session_id, summary, run_id=self.run_id
        )
        self.journal.append(summary)
        # 压缩后上一轮的 usage 不再代表当前上下文大小，清掉。
        # 不清的话下一轮会立刻再触发一次压缩。
        self._last_prompt_tokens = 0

    def _check_repeating(self, ai_msg: Msg) -> None:
        """
        跨轮打转检测。

        很多实现只有单轮内的重复工具检测，没有跨轮的。
        但 max_turns=40 下打转会烧掉 40 次调用才停，而检测只需几行。

        到达阈值时注入一次提示让模型换路，不硬停 —— 与 80% 催促同一思路。
        用 == 判定所以只注入一次，不会反复刷。
        """
        sig = tuple(sorted((tc.name, tc.arguments) for tc in ai_msg.tool_calls))
        if sig == self._last_call_sig:
            self._repeat_count += 1
        else:
            self._last_call_sig = sig
            self._repeat_count = 0

        if self._repeat_count == settings.agent.max_repeat_calls:
            log.warning("repeating_tool_calls", run_id=self.run_id, sig=str(sig)[:200])
            self._append(
                Msg(
                    role="user",
                    content=(
                        "[系统提示] 你已连续多次以完全相同的参数调用同一工具，"
                        "结果不会改变。请改变参数、换用其他工具，"
                        "或直接说明当前遇到的障碍。"
                    ),
                    agent_name=self.agent_name,
                ),
                persist=False,
            )

    async def _reason(self) -> _Accum:
        """调 LLM，逐 chunk 发事件。"""
        accum = _Accum()
        with record_span(
            "llm",
            self.model.model_id,
            session_id=self.session_id,
            agent_name=self.agent_name,
            run_id=self.run_id,
        ) as sink:
            api_msgs = self.build_api_messages()
            # ── 执行提醒：会话长了以后，每次调 LLM 前追加独立消息 ──
            # 不放在 build_api_messages 里，因为内部 loop 以 tool 结尾，
            # 不存在 user 消息可追加。这里追加独立消息，无论结尾角色。
            # api_msgs 是局部变量，不持久化，不会污染记忆。
            if self._system_reminder and len(self.messages) > self._reminder_delay:
                api_msgs = list(api_msgs)
                api_msgs.append({
                    "role": "user",
                    "content": self._system_reminder,
                })
            specs = self.registry.to_specs()
            sink.model_id = self.model.model_id
            sink.provider_name = getattr(self.model, "provider_name", "") or ""
            sink.price_in_per_1m = getattr(self.model, "price_in_per_1m", None)
            sink.price_out_per_1m = getattr(self.model, "price_out_per_1m", None)
            # 只存最后一条用户/工具消息的摘要，不存整个 api_msgs ——
            # 那会把整段上下文复制进 span 表，一次对话十几条 span 就是
            # 十几份上下文副本，表大小失控。
            if api_msgs:
                last = api_msgs[-1]
                sink.input_text = str(last.get("content") or "")

            # stream 优先级：智能体 extra_params 里显式写的 stream > 会话开关。
            # 多智能体场景下不能因会话开关覆盖掉某个智能体自己的自定义。
            extra = dict(self.extra_params)
            stream_value = extra.pop("stream", None)
            if stream_value is None:
                stream_value = self.stream_enabled
            else:
                stream_value = bool(stream_value)

            stream = self.llm.stream_chat(
                self.model, api_msgs, tools=specs if specs else None,
                stream=stream_value,
                **extra,
            )
            async for chunk in stream:
                accum.feed(chunk)
                if chunk.kind == "content" and chunk.text:
                    await emit(Ev.MESSAGE, delta=chunk.text)
                elif chunk.kind == "reasoning" and chunk.text:
                    await emit(Ev.THINKING, delta=chunk.text)

            sink.output_text = "".join(accum.content)
            if accum.usage:
                sink.set_usage(
                    input_tokens=accum.usage.prompt_tokens or None,
                    output_tokens=accum.usage.completion_tokens or None,
                )

            # 有真实 usage 用真实值，没有则本地估算。
            # 不能在没有 usage 时直接跳过 —— 那样中转站不返 usage 时前端的
            # 上下文进度条会停在上一轮的值不动，用户以为卡住了。
            if accum.usage and accum.usage.prompt_tokens:
                used = accum.usage.prompt_tokens
                is_estimate = False
            else:
                # 必须带上 tools —— 工具定义占 1400+ tokens，
                # 漏算会让进度条少报一千多，用户以为还很空
                used = estimate_tokens(api_msgs, specs)
                is_estimate = True

            await self._emit_context_usage(
                used, is_estimate=is_estimate, api_msgs=api_msgs, specs=specs
            )
        return accum

    async def _emit_context_usage(
        self,
        used: int,
        *,
        is_estimate: bool,
        api_msgs: list[dict[str, Any]] | None = None,
        specs: list[dict[str, Any]] | None = None,
    ) -> None:
        """
        发上下文占用事件。

        ## 为什么抽成方法

        主动压缩之后也要发一次 —— 不发的话界面上的占用条还停在压缩前的
        数字，而用户刚看到"已压缩"的提示，两者矛盾，他会以为压缩没生效。

        ## 固定开销为什么要单独报

        用户发一句"你好"看到 4551 token，第一反应是"计数错了"——
        因为那句话只有 2 个 token。实际是 18 个工具的 JSON schema 占了
        4298，系统提示词再占 1311，而这两项每一轮都重发。

        不拆开的话用户唯一的解释就是"这个数字是错的"。拆开之后他能看到
        真正该动的地方（关掉用不到的 MCP、精简 AGENTS.md）。
        """
        if api_msgs is None:
            api_msgs = self.build_api_messages()
        if specs is None:
            specs = self.registry.to_specs() or None

        local_tools = count_tools(specs) if specs else 0
        local_system = count_text(
            next(
                (
                    str(m.get("content") or "")
                    for m in api_msgs
                    if m.get("role") == "system"
                ),
                "",
            )
        )
        # 【必须按比例分摊，不能直接相减】。
        #
        # 本地用 tiktoken(cl100k_base) 数，而模型用自己的分词器。
        # 实测本地数出 tools 4298 + system 1845 = 6143，
        # 而模型报的整个 prompt 才 4547 —— 本地高 35%。
        #
        # 直接拿真实总数减本地分项的话，"对话内容"会变成 -1596。
        # 用户看到一个负数只会更确信"这个计数是坏的"。
        local_total = local_tools + local_system
        if local_total > 0 and used > 0:
            scale = min(1.0, used / local_total)
            tools_tok = int(local_tools * scale)
            system_tok = int(local_system * scale)
            # 本地 tiktoken(cl100k) 对中文系统提示词的估算偏高约 35%。used 小于
            # local_total 时（归档后 held_back + overview 很少，或首轮只发一句话），
            # 按比例分摊会把对话内容全吞掉 —— 前端显示"对话 token 归零"，
            # 而 held_back + overview 真实存在，不该归零。
            # 固定开销最多占 used 的 90%，至少留 10% 给对话内容。
            if tools_tok + system_tok >= used:
                ratio = (used * 0.9) / (tools_tok + system_tok)
                tools_tok = int(tools_tok * ratio)
                system_tok = int(system_tok * ratio)
        else:
            tools_tok = 0
            system_tok = 0

        await emit(
            Ev.CONTEXT_USAGE,
            used_tokens=used,
            window_tokens=self.model.context_window,
            ratio=round(used / max(1, self.model.context_window), 4),
            compact_at=int(
                self.model.context_window * settings.agent.compact_trigger_ratio
            ),
            is_estimate=is_estimate,
            # 每轮都重发的部分。对话内容 = used - 这两项
            tools_tokens=tools_tok,
            system_tokens=system_tok,
            tool_count=len(specs) if specs else 0,
        )

    async def _act(self, ai_msg: Msg, *, truncated: bool = False) -> None:
        """
        顺序执行工具，不并行。

        ## 为什么顺序

        并行实现容易踩的坑：直接用 LangGraph 的
        ToolNode（原生并行）且【完全没有】写冲突防护；自己包了
        ToolNode 但只改了异常处理，并行调度没动，它对冲突的应对是
        在模型层面阻止 —— 用一个 conflict_tool_set 工具名黑名单
        + prompt 告知，检测到就重试。

        顺序执行的其它好处：审批流程不会同时弹三个框；前一个工具失败后
        续可以跳过。并行收益主要在纯读取场景，等真成为瓶颈再优化。

        ## truncated

        上游因长度上限截断时，tool_call 的 arguments 可能是半截 JSON。
        这时【绝不能执行】—— parsed_args() 对坏 JSON 返回空 dict，
        于是 write_file() 会以空参数被真的调用。
        """
        ctx = ToolContext(
            session_id=self.session_id,
            run_id=self.run_id,
            workspace=self.workspace,
            db=self.db,
            llm=self.llm,
            agent_name=self.agent_name,
            depth=self.depth,
            registry=self.registry,
            # compact_context 工具靠这个回调请求压缩。
            #
            # 放 extra 而不是给 ToolContext 加字段：那是所有工具共用的
            # dataclass，加一个只有一个工具用的字段会让其它 20 个工具
            # 都带上这个无关依赖。
            #
            # 【只在主 agent 注入】。子 agent 的上下文独立且短暂，
            # 压缩没有意义 —— 工具那边会如实说明不支持。
            extra=(
                {"compact_now": self._compact_on_request}
                if self.depth == 0
                else {}
            ),
        )

        for tc in ai_msg.tool_calls:
            with record_span(
                "tool",
                tc.name,
                session_id=self.session_id,
                agent_name=self.agent_name,
                run_id=self.run_id,
            ) as tool_sink:
                args = tc.parsed_args()
                # 参数原样存（会走脱敏）—— 排查"为什么这个工具报错"时，
                # 第一个要看的就是它收到了什么参数。
                tool_sink.input_text = tc.arguments
                # 审批要靠 call_id 把前端的回复配对回来。ctx 在循环外只建一次
                # （它装的是请求级依赖），所以这里每轮更新这一个字段。
                ctx.current_call_id = tc.id
                # agent_name 必须带上。
                #
                # 前端靠它把工具调用挂到正确的智能体下面。只靠 span depth
                # 不行 —— 实测子智能体内部的 tool_end 拿到的 depth 是 0，
                # 因为 emit 读的是【当前】span，而工具执行时那个 agent span
                # 已经不在栈顶了。
                #
                # 后果是子智能体读的 6 个文件全被算成父代理自己读的，
                # "委派省了上下文"在界面上完全看不出来。
                await emit(
                    Ev.TOOL_START,
                    call_id=tc.id,
                    tool_name=tc.name,
                    args=args,
                    agent_name=self.agent_name,
                )
                started = asyncio.get_running_loop().time()

                if truncated:
                    # 截断保护：整批作废，让模型用完整参数重发。
                    # 拿半截参数执行的后果可能是不可逆的（空参数写文件）。
                    result = ToolResult(
                        content=(
                            f"错误：上一次响应因长度上限被截断，工具 {tc.name} 的参数"
                            "可能不完整，未执行。请用完整参数重新调用。"
                        ),
                        is_error=True,
                    )
                else:
                    result = await self._execute_with_timeout(ctx, tc)

                elapsed_ms = int((asyncio.get_running_loop().time() - started) * 1000)
                tool_sink.output_text = result.content
                if result.is_error:
                    # 工具返回错误不是异常（模型要看到错误文本去自我纠正），
                    # 但 span 里要标出来 —— 否则查"哪一步失败了"查不到。
                    tool_sink.status = "error"
                    tool_sink.error = result.content[:2000]
                tool_msg = Msg(
                    role="tool",
                    content=result.content,
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    is_error=result.is_error,
                    agent_name=self.agent_name,
                )
            await self._persist(tool_msg, display=result.display)

            await emit(
                Ev.TOOL_END,
                    call_id=tc.id,
                    tool_name=tc.name,
                    agent_name=self.agent_name,
                    is_error=result.is_error,
                    duration_ms=elapsed_ms,
                    display=result.display,
                    # 完整内容已落库，前端需要时拉 message 接口。
                    # 工具输出可能几万字符，走 SSE 会拖慢流。
                    content_preview=result.content[:500],
                )

    async def _execute_with_timeout(self, ctx: ToolContext, tc: ToolCall) -> ToolResult:
        """
        单个工具的执行上限。

        没有这层的话，一个忘了设 timeout 的工具能把整个 run 挂死 ——
        而 SSE 有心跳所以前端不会断开，用户就一直等着，看不出发生了什么。

        很多实现没有这层统一超时（timeout 分散在各个工具内部，
        run_python_code 就完全没有）。我们有 registry.execute
        这一个入口，加在这里能全局兜住。

        注意 wait_for 会把内部的 CancelledError 转成 TimeoutError，
        不会污染真正的取消路径。
        """
        try:
            return await asyncio.wait_for(
                self.registry.execute(ctx, tc.name, tc.parsed_args()),
                timeout=settings.agent.tool_timeout,
            )
        except TimeoutError:
            log.warning("tool_timeout", tool=tc.name, timeout=settings.agent.tool_timeout)
            # 超时是给模型的信息，不是系统故障 ——
            # 与 registry.execute 的"永不向上抛异常"铁律一致
            return ToolResult(
                content=(
                    f"工具 {tc.name} 执行超过 {settings.agent.tool_timeout} 秒未返回，已中止。"
                    "可尝试缩小处理范围后重新调用。"
                ),
                is_error=True,
            )

    async def _fill_missing_tool_results(self, reason: str) -> None:
        """
        为没有结果的 tool_calls 补占位消息。

        取消路径和错误路径【都要调】。只在取消路径补的话，任何非正常退出
        都会在库里留下孤立 tool_call，前端那个工具卡片会一直转圈。
        """
        cancel_all_pending(self.session_id)
        missing = find_missing_tool_calls(self.messages)
        if not missing:
            return
        for tc in missing:
            placeholder = Msg(
                role="tool",
                content=reason,
                tool_call_id=tc.id,
                tool_name=tc.name,
                is_error=True,
                agent_name=self.agent_name,
            )
            await self._persist(placeholder)
            # 通知前端把那个还在转圈的工具卡片标成错误态
            await emit(
                Ev.TOOL_END,
                call_id=tc.id,
                tool_name=tc.name,
                agent_name=self.agent_name,
                is_error=True,
                duration_ms=0,
                display=None,
                content_preview=reason,
            )
        log.info("filled_missing_tool_results", run_id=self.run_id, count=len(missing))

    # ─────────────────────────── 落库 ───────────────────────────

    def _append(self, msg: Msg, *, persist: bool = True) -> None:
        self.messages.append(msg)
        if persist:
            self.journal.append(msg)

    async def _persist(self, msg: Msg, **kw: Any) -> None:
        """
        立即落库，成功后才进工作副本和 journal。

        ## 顺序很重要

        必须【先落库再入内存】。反过来的话，落库失败时这条消息仍留在
        self.messages 里，于是 find_missing_tool_calls 会认为它已被应答、
        跳过补占位 —— 而库里其实没有它，孤立 tool_call 就留下来了。

        这个顺序错误在正常路径上完全看不出来，只在落库失败时暴露。
        """
        await repo.append_message(
            self.db, self.session_id, msg, run_id=self.run_id, **kw
        )
        self.messages.append(msg)
        self.journal.append(msg)

        # ── ON_MESSAGE 钩子 ──
        if self.hooks.has_hooks:
            self.hooks.fire_on_message(
                OnMessageContext(
                    msg=msg,
                    session_id=self.session_id,
                    agent_name=self.agent_name,
                    turn=getattr(self, '_current_turn', -1),
                )
            )


def json_preview(v: Any, limit: int = 200) -> str:
    try:
        s = json.dumps(v, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(v)
    return s[:limit]
