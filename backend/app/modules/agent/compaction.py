"""
上下文压缩。

## 为什么切点逻辑单独抽出来

切点选错的后果是上游直接 400（`tool_calls` 与 `tool` 被拆开），而这只在
长会话里才出现 —— 开发时撞不到，上线后必然撞到。所以切点计算写成
**纯函数**，不碰数据库、不调 LLM，能穷举边界情况单独测。

## 三铁律

1. 用真实 usage 触发，不用本地估算（估高白压缩、估低直接 400）
2. 绝不拆开 `tool_calls` 与其对应的 `tool` 结果
3. 必须保留 tail，否则模型会忘记"当前在做什么"

详见 docs/01-architecture/context.md。
"""

from dataclasses import dataclass
from typing import Any

import structlog

from app.core.config import settings
from app.core.events import Ev, emit
from app.core.exceptions import ProviderError
from app.infra.llm.port import LLMPort, ResolvedModel
from app.modules.agent import prompts
from app.modules.agent.messages import Msg
from app.modules.agent.tokens import count_text, estimate_tokens

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class CompactPlan:
    """
    压缩计划。分成"算"和"做"两步，算的部分可以单独测。

    head_end:   [0, head_end) 是不参与压缩的头部（system 等）
    cut:        [head_end, cut) 是要被摘要掉的候选集
    tail_start: [tail_start, len) 是保留的尾部

    正常情况 cut == tail_start。两者可以不等 —— 当切点因为 tool 组边界
    被向前调整时，中间那段会一并保留。
    """

    head_end: int
    cut: int
    tail_start: int
    victims: tuple[Msg, ...]
    pinned: tuple[Msg, ...]

    def is_worth_doing(self, *, urgent: bool = False) -> bool:
        """
        候选集太小就别压了 —— 除非已经超窗口。

        平时：压缩要花一次 LLM 调用，把两条消息换成一条摘要毫无意义，
        还会把"压缩"这件事本身变成上下文里的噪音。

        urgent=True（已经超窗口或上游已报超限）时只要有东西可压就压。
        实测撞到过：候选集 3 条、`compact_min_victims=4`，于是拒绝压缩，
        上下文一路涨到窗口的 221%。此时"压两条也比 400 好"。
        """
        if urgent:
            return len(self.victims) >= 1
        return len(self.victims) >= settings.agent.compact_min_victims


def _is_tool_result(m: Msg) -> bool:
    return m.role == "tool"


def _declares_tools(m: Msg) -> bool:
    return m.role == "assistant" and bool(m.tool_calls)


def plan_compaction(
    messages: list[Msg],
    *,
    keep_tail_turns: int | None = None,
    tail_token_budget: int | None = None,
) -> CompactPlan | None:
    """
    算出该压缩哪一段。返回 None 表示不该压。

    这是纯函数：同样的输入永远给同样的输出，没有副作用。

    ## tail 要同时受【轮次】和【token】两个限制

    只按轮次数会出事，这是实测撞到的：

        keep_tail_turns=4，但每轮是 3000 token 的长文
        → tail 自己就 12000 token，而窗口只有 4200

    结果是压缩每轮都触发、每轮都因为"候选集太少"放弃，上下文一路涨到
    窗口的 221% 才被上游 400 打断。日志里看到的是

        compaction_triggered  used=7575 window=4200
        compaction_skipped    reason=too_few_victims victims=3

    轮次限制解决"保留几次交互"，token 限制解决"保留多少内容"。
    两者取更严的那个。
    """
    keep = keep_tail_turns if keep_tail_turns is not None else settings.agent.keep_tail_turns

    # 头部：连续的 system。它们是指令，永不压缩。
    head_end = 0
    while head_end < len(messages) and messages[head_end].role == "system":
        head_end += 1

    # artifact 单独钉住，不参与压缩也不占 tail 名额。
    # 它是"当前工作成果"，压缩掉之后用户说"把刚才那份代码改一下"就没法接。
    pinned = tuple(m for m in messages[head_end:] if m.role == "artifact")
    body = [m for m in messages[head_end:] if m.role != "artifact"]

    if not body:
        return None

    # tail：从后往前数 keep 轮。一"轮"以 user 消息为界 ——
    # 按消息条数数会把一个 assistant+tool+tool 的组切碎。
    tail_start_in_body = _find_tail_start(body, keep)

    # 再按 token 预算收紧。轮次够少但内容巨大时，上面那个起点会让
    # tail 自己就超窗口 —— 必须往后挪。
    if tail_token_budget is not None and tail_token_budget > 0:
        tail_start_in_body = _shrink_tail_to_budget(
            body, tail_start_in_body, tail_token_budget
        )

    if tail_start_in_body <= 0:
        # 整个 body 都在 tail 里，没东西可压
        return None

    # 关键一步：把切点从 tool 组内部挪出去
    cut_in_body = _adjust_to_tool_boundary(body, tail_start_in_body)
    if cut_in_body <= 0:
        return None

    victims = tuple(
        m for m in body[:cut_in_body] if m.role != "summary"
    )
    if not victims:
        # 全是已有的 summary，再压一次只会让信息越来越糊
        return None

    # 换算回原始索引
    cut_abs = head_end + _abs_index(messages, head_end, body, cut_in_body)
    tail_abs = head_end + _abs_index(messages, head_end, body, tail_start_in_body)

    return CompactPlan(
        head_end=head_end,
        cut=cut_abs,
        tail_start=tail_abs,
        victims=victims,
        pinned=pinned,
    )


def _abs_index(
    messages: list[Msg], head_end: int, body: list[Msg], body_index: int
) -> int:
    """
    把 body 里的下标换算成 messages 里的偏移。

    body 是 messages[head_end:] 去掉 artifact 后的结果，所以下标会错位。
    用对象身份匹配而不是下标算术 —— 后者在有 artifact 时一定算错。
    """
    if body_index >= len(body):
        return len(messages) - head_end
    target = body[body_index]
    for i, m in enumerate(messages[head_end:]):
        if m is target:
            return i
    return len(messages) - head_end


def _find_tail_start(body: list[Msg], keep_turns: int) -> int:
    """
    从后往前数 keep_turns 个 user 消息，返回 tail 的起点。

    以 user 为界而不是按条数：一轮可能是
    user → assistant(tool_calls) → tool → tool → assistant
    五条消息，按条数数会把它切碎。
    """
    seen = 0
    for i in range(len(body) - 1, -1, -1):
        if body[i].role == "user":
            seen += 1
            if seen > keep_turns:
                return i + 1
    return 0  # user 消息不够 keep_turns 个，全部保留


def _shrink_tail_to_budget(body: list[Msg], tail_start: int, budget: int) -> int:
    """
    把 tail 起点往后挪，直到 tail 的 token 数落进预算内。

    从最后一条往前累加，超预算就停 —— 保证留下的是【最近的】内容。

    ## 最后一个 user 消息是硬下限

    它是"用户当前问的问题"。压掉它之后模型收到的是
    「system + 摘要 + 一堆历史」，没有任何当前诉求 —— 模型只能猜，
    或者反问"你想做什么"。这比上下文超限更糟：超限有报错，这个没有。

    所以即使那条消息本身就超预算（用户粘了一大段代码进来），
    也必须保留。预算在这种情况下会被突破，但那是唯一正确的选择 ——
    真正超限时上游会报错，`_force_compact` 会继续放宽 keep_tail。
    """
    if tail_start >= len(body):
        return tail_start

    floor = _last_user_index(body)

    total = 0
    i = len(body)
    while i > tail_start:
        cost = estimate_tokens([body[i - 1].to_api()])
        if total + cost > budget and i - 1 <= floor:
            # 已经缩到硬下限还是超预算 —— 停在下限，不再往后挪
            break
        if total + cost > budget:
            break
        total += cost
        i -= 1
    # 不能越过最后一个 user 消息
    return min(i, floor) if floor >= 0 else i


def _last_user_index(body: list[Msg]) -> int:
    """最后一条 user 消息的下标。没有则返回 -1。"""
    for i in range(len(body) - 1, -1, -1):
        if body[i].role == "user":
            return i
    return -1


def _adjust_to_tool_boundary(body: list[Msg], cut: int) -> int:
    """
    把切点向前挪，直到它不在一个 tool 组内部。

    ## 为什么必须这样

    OpenAI 兼容 API 的硬性要求：
      - 带 tool_calls 的 assistant 后面必须紧跟对应的 tool 消息
      - tool 消息前面必须有声明它的 assistant

    切在中间的后果是 400，而且错误信息通常只说"messages 格式不对"，
    不指出是哪一对被拆开了。

    ## 向前而不是向后

    向后挪会把本该保留的 tail 内容也压掉（tail 是模型理解"当前在做什么"
    的依据）。向前挪只是少压一点，代价小得多。
    """
    if cut >= len(body):
        # 保留段为空。这本身是边界安全的（没有 tool 消息会变成孤立的），
        # 直接返回即可 —— 但调用方有"最后一个 user 消息不能被压"的下限，
        # 正常走不到这里。
        return len(body)

    i = cut
    # 切点落在 tool 消息上，说明它属于前面某个 assistant 的组，往前退
    while i > 0 and _is_tool_result(body[i]):
        i -= 1
    # 退到了声明 tool_calls 的 assistant 上，它也要一起归入保留段
    if i > 0 and _declares_tools(body[i]):
        return i
    # 检查 i-1 是不是带 tool_calls 的 assistant —— 那样 i 就在组内
    if i > 0 and _declares_tools(body[i - 1]):
        return i - 1
    return i


def build_summary_input(victims: tuple[Msg, ...]) -> str:
    """
    把待压缩的消息渲染成给 compact 模型看的文本。

    刻意保留角色标签和工具名：摘要模型需要知道"这是模型自己做的"还是
    "用户要求的"，也需要知道调过哪些工具（否则摘要里会丢掉"已经建过
    某个文件"这类事实）。

    思维链【不含】在内 —— 它是过程不是结论，占 token 且对重建上下文无用。
    """
    lines: list[str] = []
    for m in victims:
        if m.role == "user":
            lines.append(f"[用户] {m.content}")
        elif m.role == "assistant":
            if m.content:
                lines.append(f"[助手] {m.content}")
            for tc in m.tool_calls or []:
                args = tc.arguments[:300]
                lines.append(f"[助手调用工具] {tc.name}({args})")
        elif m.role == "tool":
            flag = "失败" if m.is_error else "成功"
            body = m.content[: settings.agent.compact_tool_excerpt]
            lines.append(f"[工具结果 {m.tool_name} {flag}] {body}")
        elif m.role == "summary":
            lines.append(f"[更早的摘要] {m.content}")
    return "\n".join(lines)


async def compact(
    *,
    messages: list[Msg],
    llm: LLMPort,
    model: ResolvedModel,
    agent_name: str = "",
    keep_tail_turns: int | None = None,
    tool_specs: list[dict[str, Any]] | None = None,
    tail_token_budget: int | None = None,
    urgent: bool = False,
) -> tuple[list[Msg], Msg] | None:
    """
    执行压缩。返回 (新消息列表, summary 消息)，不该压或压不动时返回 None。

    ## 失败时返回 None 而不是抛异常

    调用方是"上下文超限"的错误处理路径。压缩失败时上层要把原始的
    overflow 错误如实抛给用户 —— 那个错误比"压缩失败"更有信息量。
    """
    plan = plan_compaction(
        messages,
        keep_tail_turns=keep_tail_turns,
        tail_token_budget=tail_token_budget,
    )
    if plan is None or not plan.is_worth_doing(urgent=urgent):
        log.info(
            "compaction_skipped",
            reason="no_plan" if plan is None else "too_few_victims",
            victims=0 if plan is None else len(plan.victims),
            urgent=urgent,
        )
        return None

    # 带上 tool_specs：工具定义占 1400+ tokens，两侧都算才有可比性
    before_tokens = estimate_tokens([m.to_api() for m in messages], tool_specs)
    await emit(Ev.COMPACTING, victim_count=len(plan.victims))

    # 占位符名字必须与 compact.md 里的一致（是 history 不是 conversation）。
    #
    # 传错名字【不会报任何错】—— render 只做字符串替换，找不到就原样留着。
    # 于是模型收到的是字面的 "{{history}}"，回复"无对话历史内容，
    # 请提供需要压缩的对话历史"，而这条回复被当成摘要存了下来。
    #
    # 后果极其隐蔽：压缩事件正常发出、token 数正常下降（因为历史真的被
    # 换掉了）、日志一切正常，只有摘要内容是垃圾。会话越往后模型越糊涂，
    # 而没有任何报错。下面的空摘要和长度检查就是为了兜住这类问题。
    rendered_input = build_summary_input(plan.victims)

    # 摘要目标长度按【当前模型的窗口】算，不是写死一个字数。
    #
    # ## 为什么要动态
    #
    # 8K 窗口的模型和 128K 的模型能容纳的摘要差 16 倍。写死"压到 2000 字"
    # 的话：小窗口模型压完仍然超限（压了等于没压），大窗口模型则白丢信息
    # （明明还有 100K 空间，却把细节砍光了）。
    #
    # ## 为什么只算对话部分
    #
    # 系统提示词和工具定义是固定开销（本项目 18 个工具就 4300 token）。
    # 把它们算进额度的话，8K 窗口下摘要只剩几百 token 可用 ——
    # 而那点长度装不下"用户的约束 + 决定的理由 + 失败原因"。
    budget_tokens = max(200, int(model.context_window * settings.agent.compact_target_ratio))
    # 中文大约 1.4 token/字（tiktoken 对中文偏贵）。给模型一个字符数
    # 参照 —— 它数不准 token，但对字数有感觉。
    budget_chars = int(budget_tokens / 1.4)

    text = prompts.render(
        prompts.load_builtin("compact"),
        history=rendered_input,
        budget_tokens=str(budget_tokens),
        budget_chars=str(budget_chars),
        window_tokens=str(model.context_window),
        budget_percent=str(int(settings.agent.compact_target_ratio * 100)),
    )
    if "{{" in text:
        # 模板里还有没被替换的占位符 —— 说明变量名对不上
        log.error("compact_prompt_unrendered", prompt_head=text[:200])
        return None

    try:
        chunks: list[str] = []
        async for c in llm.stream_chat(model, [{"role": "user", "content": text}]):
            if c.kind == "content" and c.text:
                chunks.append(c.text)
        summary_text = "".join(chunks).strip()
    except ProviderError as e:
        # 压缩用的模型也可能挂。这时如实返回失败，让上层抛原始错误。
        log.warning("compaction_llm_failed", err=e.message)
        return None

    if not summary_text:
        log.warning("compaction_empty_summary")
        return None

    # 摘要短得不合理时拒绝使用。
    #
    # 实测遇到过 9662 token 的历史被"压缩"成 17 个字符
    # （模型收到未替换的占位符，回复"无对话历史内容"）。这种摘要通过了
    # 所有其它检查：压缩事件正常、token 数正常下降、日志无异常 ——
    # 只有内容是垃圾，而会话会从此越来越糊涂。
    #
    # 阈值取输入长度的 0.5%，且至少 80 字符。压缩比 200:1 已经远超
    # 任何合理摘要（提示词要求保留约定、决定、改动、失败原因等六类信息）。
    min_len = max(80, int(len(rendered_input) * 0.005))
    if len(summary_text) < min_len:
        log.warning(
            "compaction_summary_too_short",
            summary_chars=len(summary_text),
            input_chars=len(rendered_input),
            min_expected=min_len,
            summary_head=summary_text[:120],
        )
        return None

    # 超预算只记日志，不拒绝。
    #
    # ## 为什么不截断
    #
    # 摘要是结构化的（小标题分段），从中间切断会切掉最后一个小节 ——
    # 而提示词里"篇幅不够时先砍改动细节"的取舍顺序意味着重要内容在前，
    # 但"失败原因"那一节完全可能排在末尾。切掉它比超预算糟得多。
    #
    # ## 为什么不重试
    #
    # 重试要再花一次 LLM 调用，而模型第二次通常也压不到目标 ——
    # 它对 token 数没有精确控制力。超一点点不影响可用性
    # （阈值本身留了 75% 的余量），超很多则说明历史确实太长，
    # 下一轮会再压一次。
    #
    # 记日志是为了能发现"某个模型系统性压不动"这件事。
    actual_tokens = count_text(summary_text)
    if actual_tokens > budget_tokens * 1.5:
        log.warning(
            "compaction_over_budget",
            actual_tokens=actual_tokens,
            budget_tokens=budget_tokens,
            window=model.context_window,
            model=model.model_id,
        )

    summary = Msg(
        role="summary",
        content=summary_text,
        agent_name=agent_name,
    )

    # 新列表 = head + summary + 保留段（含 tail）+ 钉住的 artifact
    #
    # artifact 放【最末尾】而不是按时序插入：它是"当前工作成果"，
    # 放在最后模型最容易注意到。按时序插入会让它埋在中间。
    new_messages = (
        messages[: plan.head_end]
        + [summary]
        + [m for m in messages[plan.cut :] if m.role != "artifact"]
        + list(plan.pinned)
    )

    after_tokens = estimate_tokens([m.to_api() for m in new_messages], tool_specs)
    await emit(
        Ev.COMPACTED,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        victim_count=len(plan.victims),
        summary_chars=len(summary_text),
    )
    log.info(
        "compacted",
        victims=len(plan.victims),
        before=before_tokens,
        after=after_tokens,
        kept=len(new_messages),
    )
    return new_messages, summary
