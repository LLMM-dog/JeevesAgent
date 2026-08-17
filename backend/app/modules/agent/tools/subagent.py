"""
subagent 工具：把任务委派给一个独立上下文的子智能体。

## 委派的唯一理由是省上下文

如果子代理的结果原样回灌父上下文，那只是把污染从"过程"搬到了"结果"，
收益归零。所以返回值**必须截断**，且区分"模型可见"与"UI 可见"。

在这点上最反面：`outputs: Annotated[str, operator.add]`
—— 子代理跑一小时的全部文本输出累加后一次性进父
上下文。

有的实现是唯一做对的：50KB 硬上限、UTF-8 安全截断
、完整 messages 只进 details。
而且它是**两端都踩过**才定在 50KB —— 最早只回 100 字符，模型看不懂发生
了什么（CHANGELOG #4710）。

## 三件必须一起做的事

并发上限、超时、取消级联。这三件很容易只做一到两件：

| | | | 同类实现 | 本项目 |
| --- | --- | --- | --- | --- |
| 并发上限 | **无** | **无** | 8 任务 / 4 并发 | 有 |
| 超时 | **无** | **无** | **无** | 有 |
| 取消级联 | **不级联** | 会级联 | 会级联 | 会级联 |

超时是**最容易漏掉的一件**。后果分别是：
永久占 worker slot、永久阻塞父代理（裸 `await
sub.pending_future` 没有 timeout）、同类实现 永久占并发槽位。

## 递归防护是双保险

第一道：白名单里天然不含 `subagent`（见 specs.NEVER_FOR_SUBAGENT）。
第二道：ContextVar 深度计数。

为什么需要两道 —— 白名单是**配置**，配置会被写错。有实现只靠白名单，它的
`worker.md` 没写 `tools:` 字段就拿到全集，成了潜在的无限递归口子。

为什么用 ContextVar 而不是全局计数器（注释说得最清楚，
相关实现）：

> 深度追踪 — 使用 ContextVar 实现每个 asyncio Task 独立的计数。
> 并发调用（同一层级的多个子 Agent）互不干扰；
> 链式递归（子 Agent 再调子 Agent）才会递增深度。

全局计数器会把 5 个并行子代理误判成深度 5。
"""

from __future__ import annotations

import asyncio
import contextvars
from typing import Any

import structlog

from app.core.events import Ev, emit
from app.modules.agent import specs as agent_specs
from app.modules.agent.tools.base import ToolContext, ToolResult
from app.modules.trace.recorder import record_span

log = structlog.get_logger(__name__)

# 单个子代理返回给【模型】的上限。完整内容进事件流和 UI。
#
# 50KB 是 同类实现 试出来的折中值。100 字符太少（模型看不懂发生了什么），
# 无上限等于没做委派。
OUTPUT_CAP_BYTES = 50 * 1024

# 一次 tool_calls 里最多接受几个委派。LLM 会一次发 30 个。
MAX_PARALLEL = 6
# 实际同时跑几个
MAX_CONCURRENCY = 3
# 单个子代理的墙钟上限
TIMEOUT_S = 600.0

_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "subagent_depth", default=0
)
_sem: asyncio.Semaphore | None = None


def _semaphore() -> asyncio.Semaphore:
    # 懒创建：Semaphore 会绑定创建时的事件循环，模块级创建在测试里
    # （每个测试一个新循环）会报 "attached to a different loop"
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(MAX_CONCURRENCY)
    return _sem


def current_depth() -> int:
    return _depth.get()


def truncate_for_model(text: str) -> tuple[str, bool]:
    """
    按 UTF-8 字节截断，返回 (截断后文本, 是否截断过)。

    按字节而不是字符：上限是为了控制 token 和传输量，而中文一个字符占
    3 字节。按字符算的话中文内容实际能塞进三倍的量。

    截断后要**告诉模型完整内容在哪**，否则它会以为信息丢了，
    可能重新派一次子代理去拿。
    """
    raw = text.encode("utf-8")
    if len(raw) <= OUTPUT_CAP_BYTES:
        return text, False
    kept = raw[:OUTPUT_CAP_BYTES].decode("utf-8", errors="ignore")
    omitted = len(raw) - len(kept.encode("utf-8"))
    return (
        f"{kept}\n\n[输出已截断，省略 {omitted} 字节。"
        f"完整结果在工具详情里（前端可展开），不需要重新委派。]",
        True,
    )


class SubAgentTool:
    name = "subagent"
    description = (
        "委派子任务给独立上下文的子智能体，返回最终结论。"
        "用于需要读大量材料只要结论、或需受限权限执行的场景。"
        "task 必须自包含——子智能体看不到当前对话。"
        "简单任务不要用，委派本身有开销。"
    )
    requires_approval = False

    def parameters(self) -> dict[str, Any]:
        reg = agent_specs.get_registry()
        return {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "enum": reg.names(),
                    "description": "目标子智能体。"
                    + "；".join(f"{n}：{d}" for n, d in reg.catalog()),
                },
                "task": {
                    "type": "string",
                    "description": (
                        "完整、自包含的任务描述。子智能体看不到当前对话，"
                        "不要引用上文"
                    ),
                },
            },
            "required": ["agent", "task"],
        }

    async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
        agent_name = str(kw.get("agent") or "").strip()
        task = str(kw.get("task") or "").strip()

        if not agent_name or not task:
            return ToolResult(content="agent 和 task 都不能为空", is_error=True)

        reg = agent_specs.get_registry()
        spec = reg.get(agent_name)
        if spec is None:
            return ToolResult(
                content=(
                    f"子智能体 {agent_name} 不存在。可用：{', '.join(reg.names())}"
                ),
                is_error=True,
            )

        depth = _depth.get()
        if depth >= agent_specs.MAX_DEPTH:
            # 兜底防护。正常情况下子代理拿不到 subagent 工具（白名单已剔除），
            # 走到这里说明配置被写错了 —— 所以要报清楚原因，不是静默失败。
            return ToolResult(
                content=(
                    f"已达子智能体嵌套深度上限（{agent_specs.MAX_DEPTH}），拒绝继续委派。"
                    "请把任务拆成同一层的几个独立子任务，或者自己完成这一步。"
                ),
                is_error=True,
            )

        from app.modules.agent.subagent_runner import run_subagent

        token = _depth.set(depth + 1)
        try:
            async with _semaphore():
                # 用 record_span 而不是 new_span —— 这个 agent span 必须落库。
                #
                # 实测踩过：用 new_span 时它只传上下文不写表，于是子代理内部
                # 那几条 span 的 parent_span_id 指向一个【库里不存在的 id】。
                # 后果是执行树里子代理的 span 变成了和 agent:main 平级的孤儿，
                # 看起来像"委派和主流程是两件独立的事"，完全看不出嵌套关系。
                #
                # 孤儿 span 不会报错、不会丢数据，只是树画错了 ——
                # 这类问题只能靠盯着树的形状发现。
                with record_span(
                    "agent",
                    spec.name,
                    session_id=ctx.session_id,
                    agent_name=spec.name,
                    run_id=ctx.run_id,
                ) as agent_sink:
                    agent_sink.input_text = task
                    # span_id / parent_span_id / depth 由 emit 从当前 span
                    # 自动注入，这里不用手动传 ——
                    # 传了也会被同样的值覆盖，反而让人以为有两个来源。
                    await emit(
                        Ev.AGENT_START, agent_name=spec.name, task=task[:200]
                    )
                    try:
                        result = await asyncio.wait_for(
                            run_subagent(ctx, spec, task), timeout=TIMEOUT_S
                        )
                    except TimeoutError:
                        # 超时转成给模型的错误字符串，不向上抛 ——
                        # 父代理应该能决定"换个更小的任务重试"还是"自己做"。
                        await emit(
                            Ev.AGENT_END,
                            agent_name=spec.name,
                            stop_reason="timeout",
                            turns=0,
                            prompt_tokens=0,
                            completion_tokens=0,
                        )
                        log.warning("subagent_timeout", agent=spec.name)
                        return ToolResult(
                            content=(
                                f"子智能体 {spec.name} 超时（{int(TIMEOUT_S)} 秒）未完成，"
                                "已终止。任务可能过大，考虑拆小后重试。"
                            ),
                            is_error=True,
                        )
                    except asyncio.CancelledError:
                        # 取消级联。父代理被取消时子代理必须一起停 ——
                        # 做不到这点（子代理跑在全局 worker 的独立 Task 里，
                        # 与父代理无 Task 树关系），用户 Ctrl+C 后子代理继续烧钱。
                        #
                        # 这里用 asyncio.wait_for + 直接 await，子任务天然挂在
                        # 父 Task 树上，取消自动传播。重新抛出让传播继续。
                        await emit(
                            Ev.AGENT_END,
                            agent_name=spec.name,
                            stop_reason="cancelled",
                            turns=0,
                            prompt_tokens=0,
                            completion_tokens=0,
                        )
                        log.info("subagent_cancelled", agent=spec.name)
                        raise

                    # 字段与主代理的 AGENT_END 保持一致，前端一套逻辑处理两者。
                    # display 里带了 turns/tokens，从那里取。
                    d = result.display or {}
                    # agent span 记完整结论（走脱敏和截断），不是给模型的
                    # 截断版 —— span 是给人排查用的，要能看到子代理到底说了什么。
                    agent_sink.output_text = str(d.get("full_text") or result.content)
                    agent_sink.model_id = str(d.get("model") or "")
                    if result.is_error:
                        agent_sink.status = "error"
                        agent_sink.error = result.content[:2000]
                    agent_sink.set_usage(
                        input_tokens=int(d.get("prompt_tokens") or 0) or None,
                        output_tokens=int(d.get("completion_tokens") or 0) or None,
                    )
                    await emit(
                        Ev.AGENT_END,
                        agent_name=spec.name,
                        stop_reason=("final" if not result.is_error else "error"),
                        turns=int(d.get("turns") or 0),
                        prompt_tokens=int(d.get("prompt_tokens") or 0),
                        completion_tokens=int(d.get("completion_tokens") or 0),
                    )
        finally:
            # 恢复而非递减 —— 异常场景下递减会算错。
            # 注释：「恢复而非递减，避免异常场景下计数错乱」
            _depth.reset(token)

        return result
