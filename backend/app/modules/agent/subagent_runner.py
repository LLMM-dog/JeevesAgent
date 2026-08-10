"""
子智能体的执行入口。

## 状态用白名单构造，绝不用 {**parent_state}

做法是 `{**parent_state, "messages": [], "todos": [], ...}`
—— 21 个字段逐个覆盖。这是**黑名单式**：
语义是"默认全继承"，你必须记住去覆盖每一个不该继承的字段。

字段增长时这必然出错。已经能看到风险：`documents` 字段在
`MainAgentState` 里但 initial_state 没覆盖，虽然后续 `context_prepare` 会
重新赋值兜住，但那是巧合而非设计。

这里显式列出每一个要传的东西，没列的就没有。新增会话级状态时，
子代理默认拿不到 —— 这正是想要的方向。
"""

from __future__ import annotations

import structlog

from app.core.trace_context import current_span
from app.infra.llm.openai_compat import get_llm
from app.modules.agent import prompts
from app.modules.agent.loop import AgentLoop
from app.modules.agent.messages import Msg
from app.modules.agent.specs import AgentSpec
from app.modules.agent.tools.base import ToolContext, ToolRegistry, ToolResult
from app.modules.endpoint import service as provider_service

log = structlog.get_logger(__name__)


def build_subagent_prompt(spec: AgentSpec, *, workspace: str, tool_names: list[str]) -> str:
    """
    子代理的系统提示词。

    ## 不复用主智能体的提示词

    子代理**与主 Agent 用完全相同的 system prompt**
    （见 `prompt-caching.md:141` 的说明）。后果是子代理没有独立人格，
    它会像跟人聊天一样回答父代理 —— 带寒暄、带反问、带"还需要我做什么吗"。

    父代理拿到这种回复得再花一轮去解析。所以子代理的提示词要单独写，
    并且明确约束输出形态（见 specs.py 里的 _RESEARCHER_PROMPT）。

    ## 但环境部分要共用

    工作区路径、可用工具清单这些是客观事实，两边应该一致。
    重复写一份的话，改了主的忘了改子的，子代理就会尝试调用不存在的工具。
    """
    env = prompts.get_prompt_parts(workspace=workspace, tool_names=tool_names)
    env_text = "\n\n".join(p.content for p in env if p.key == "env")
    parts = [spec.prompt.strip()]
    if env_text:
        parts.append(env_text)
    # 技能清单【不给子代理】。
    #
    # 子代理的任务是单一的，它不需要知道全部技能 —— 常驻清单对它是纯浪费。
    # 需要某个技能时，父代理应该在 task 里直接点名，或者把结论写进 task。
    return "\n\n".join(parts)


async def run_subagent(ctx: ToolContext, spec: AgentSpec, task: str) -> ToolResult:
    """
    跑一个子智能体，返回它的最终结论。

    返回值的截断在调用方（SubAgentTool）做 —— 这里返回完整内容，
    好让事件流和 UI 拿到全量。
    """
    from app.modules.agent.tools.subagent import truncate_for_model

    # ── 工具集：白名单交上父代理实际有的 ──
    #
    # 交集而不是直接用 spec.tools：spec 可能声明了一个当前未注册的工具
    #（比如 web_search 还没实现），直接 bind 会让模型调用一个不存在的东西。
    parent_registry = ctx.registry
    available = parent_registry.names() if parent_registry else []
    allowed = spec.allowed_tools(available)

    sub_registry = ToolRegistry()
    if parent_registry is not None:
        for name in allowed:
            tool = parent_registry.get(name)
            if tool is not None:
                sub_registry.register(tool)

    if not allowed:
        # 一个工具都没有的子代理只能空想，直接报错比让它跑一轮说"我做不到"好
        return ToolResult(
            content=(
                f"子智能体 {spec.name} 没有可用工具"
                f"（声明了 {list(spec.tools)}，当前已注册 {available}）。"
                "请检查 agents/ 下的定义"
            ),
            is_error=True,
        )

    # ── 模型：按 agent_name 解析，自带降级 ──
    #
    # 侦察类任务用便宜模型读 20 个文件，成本是主模型的十分之一。
    # 和 都强制继承父模型，等于用最贵的模型去 grep。
    #
    # 走既有的 resolve(agent_name=...)：它的解析顺序已经是
    #   (agent_name, purpose) → ("", purpose) → ("", "chat")
    # 并且降级时会发 model_fallback 事件。不另造一套 —— 那会绕过降级可见性。
    model = await provider_service.resolve(
        ctx.db, purpose="chat", agent_name=spec.name
    )

    span = current_span()
    depth = span.depth if span else ctx.depth + 1

    # ── 白名单构造。每一项都是显式传的 ──
    loop = AgentLoop(
        db=ctx.db,
        llm=get_llm(),
        model=model,
        registry=sub_registry,
        session_id=ctx.session_id,
        run_id=ctx.run_id,
        workspace=ctx.workspace,
        # agent_name 让消息落进子代理自己的记忆线。
        # 主线只有用户输入和主代理的答复，子代理的中间过程不污染它。
        agent_name=spec.name,
        system_prompt=build_subagent_prompt(
            spec, workspace=str(ctx.workspace), tool_names=allowed
        ),
        depth=depth,
    )

    # 【不加载父会话历史】。
    #
    # 这是委派能省上下文的全部原因。继承历史的话子代理的上下文和父会话
    # 一样大，收益归零。
    #
    # 代价是 task 必须自包含 —— 工具描述里反复强调了这点。
    #
    # 必须是 Msg 实例，不能是 dict。实测踩过：塞 dict 进去后
    # `find_missing_tool_calls` 里 `msgs[idx].role` 抛
    # AttributeError: 'dict' object has no attribute 'role'，
    # 子代理【第一轮就崩】。而表现极具误导性 —— 父代理拿到错误后自己编了
    # 一段结论交差，看起来像委派成功了，token 也真的省了（因为子代理啥也没干）。
    loop.messages = [Msg(role="user", content=task, agent_name=spec.name)]

    # max_turns 按 spec 走，不用全局值。调研型任务需要多轮读文件，
    # 审查型任务通常几轮就够 —— 用同一个上限对两者都不合适。
    result = await loop.run(max_turns=spec.max_turns)

    final = (result.final_text or "").strip()
    if not final:
        # 没有最终文本说明子代理跑完了但没给结论（撞 max_turns 或只调工具
        # 不说话）。这种情况要明确告诉父代理，否则它会以为任务成功了。
        reason = {
            "max_turns": f"达到轮次上限（{spec.max_turns}）仍未给出结论",
            "cancelled": "被取消",
            "error": "执行出错",
        }.get(result.stop_reason, f"未给出结论（stop_reason={result.stop_reason}）")
        return ToolResult(
            content=(
                f"子智能体 {spec.name} {reason}。"
                "如果任务较大，考虑拆成更小的几步分别委派。"
            ),
            is_error=True,
        )

    trimmed, was_truncated = truncate_for_model(final)
    log.info(
        "subagent_done",
        agent=spec.name,
        turns=result.turns,
        chars=len(final),
        truncated=was_truncated,
        stop_reason=result.stop_reason,
    )
    return ToolResult(
        content=trimmed,
        display={
            "agent": spec.name,
            "turns": result.turns,
            "stop_reason": result.stop_reason,
            "model": model.model_id,
            "truncated": was_truncated,
            # token 归集到子智能体头上。委派的成本必须可见 ——
            # 少见实现做了这件事，另两个的子代理开销是黑洞，
            # "这次委派花了多少钱"无法回答。
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            # 完整结论进 display 给 UI ——
            # 模型看截断版，用户能展开看全量。这是 同类实现。
            "full_text": final,
            "chars": len(final),
        },
    )
