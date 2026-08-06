"""
主动压缩上下文的工具。

## 为什么需要它

原来只有被动压缩：上下文涨到窗口 75% 时自动触发。这有两个问题。

第一，时机不由模型决定。模型知道"这个任务的调研阶段结束了、几十条
工具输出已经没用了"，而阈值不知道 —— 它只看总量。于是常见情形是：
调研完还剩一半空间，模型继续往下做，等到 75% 触发压缩时，
正在进行的实现细节和早已无用的调研输出被一起压缩。

第二，撞到阈值时压缩是"打断式"的：正在推理的中途插入一次 LLM 调用。
主动压缩可以选在一个自然的段落边界上。

## 为什么不让模型直接改上下文

工具只能请求压缩，实际执行仍然走 compaction.compact —— 那里有
head/tail 的保留规则、artifact 钉住、摘要长度校验。让模型自己决定
"删哪些消息"会绕过全部这些保护。
"""

from __future__ import annotations

from typing import Any

import structlog

from app.modules.agent.tools.base import ToolContext, ToolResult

log = structlog.get_logger(__name__)


class CompactContextTool:
    """
    主动压缩。

    ## description 要说清什么时候用

    模型不会主动想起这个工具 —— 它感觉不到上下文压力（没有任何信号告诉
    它"你已经用了 60%"）。所以描述里必须给出具体的判断依据，
    而不是"需要时调用"。
    """

    name = "compact_context"
    description = (
        "把前面的对话历史压缩成一份交接摘要，腾出上下文空间。"
        "\n\n什么时候用："
        "\n- 一个阶段结束、要转入下一阶段时（比如调研完成、准备开始写代码）"
        "\n- 前面有大量已经用不到的工具输出（读过的文件内容、搜索结果）"
        "\n- 用户明确要求压缩或清理上下文"
        "\n\n不要用在："
        "\n- 刚开始对话时（没什么可压的，白花一次调用）"
        "\n- 正在进行的多步操作中间（摘要会丢掉刚才那几步的细节）"
        "\n\n压缩会保留最近几轮对话、当前工作成果、用户的约束和已做的决定。"
        "更早的内容会变成摘要 —— 这是有损的，所以不要频繁调用。"
    )
    # 不需要审批：它不动文件、不执行命令，最坏结果是白花一次 LLM 调用。
    # 要审批的话每次都弹窗，而模型会因为怕打扰用户而不敢用。
    requires_approval = False

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "为什么现在要压缩。会记进日志，也会展示给用户 —— "
                        "让他知道上下文为什么突然变短了。"
                    ),
                }
            },
            "required": ["reason"],
        }

    async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
        reason = str(kw.get("reason", "")).strip() or "未说明原因"

        # loop 通过 ctx.extra 注入回调。
        #
        # 为什么不让工具直接持有 loop：会形成循环引用（loop 建 registry、
        # registry 执行工具、工具反过来调 loop），而且 ToolContext 是所有
        # 工具共用的 dataclass，给它加一个只有一个工具用的字段
        # 会让其它 20 个工具都带上这个无关依赖。
        hook = ctx.extra.get("compact_now")
        if hook is None:
            # 子 agent 里没有这个回调 —— 它们的上下文是独立的、生命周期很短，
            # 压缩没有意义。如实说明而不是静默成功。
            return ToolResult(
                content=(
                    "当前环境不支持主动压缩（子 agent 的上下文独立且短暂，"
                    "不需要压缩）。"
                ),
                is_error=True,
            )

        result = await hook(reason)

        if not result.get("compacted"):
            why = result.get("reason", "没有足够的历史可压缩")
            # 【不算错误】。"现在没什么可压的"是正常答复，
            # 标成 is_error 会让模型以为工具坏了、开始重试。
            return ToolResult(
                content=f"没有执行压缩：{why}",
                display={"compacted": False, "why": why},
            )

        before = result.get("before_tokens", 0)
        after = result.get("after_tokens", 0)
        saved = max(0, before - after)
        return ToolResult(
            content=(
                f"已压缩 {result.get('victim_count', 0)} 条历史消息。"
                f"上下文从 {before} token 降到 {after} token"
                f"（省下 {saved}）。"
                "\n\n注意：更早的细节现在只存在于摘要里。"
                "如果需要某个具体文件的内容，重新读一次。"
            ),
            display={
                "compacted": True,
                "victim_count": result.get("victim_count", 0),
                "before_tokens": before,
                "after_tokens": after,
                "reason": reason,
            },
        )
