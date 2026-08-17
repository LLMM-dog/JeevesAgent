"""
LLM 模块：模型解析与调用函数的便捷包装。

这个模块封装了 endpoint.service.resolve() 的调用，并提供了记忆提取
需要的 LLM 调用函数构造。记忆提取（commit_session）需要的是一个
可调用的 `async (messages, tools) -> (text, tool_calls)` 函数，
而不是 ResolvedModel 对象本身 —— 这里把解析和调用封装在一起。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.infra.llm.port import ResolvedModel

# llm_call 的签名：接收消息列表和工具定义，返回 (文本, 工具调用列表)。
MemoryLLMCall = Callable[[list[dict[str, Any]], list[dict[str, Any]] | None], Any]


def _make_llm_call(model: ResolvedModel) -> MemoryLLMCall:
    """把 ResolvedModel 包装成记忆提取可用的 llm_call 函数。"""

    async def llm_call(
        messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> tuple[str, list[Any], str]:
        from app.infra.llm.openai_compat import get_llm
        from app.modules.memory.extract_tools import ToolCall

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}

        async for chunk in get_llm().stream_chat(
            model, messages, tools=tools if tools else None
        ):
            if chunk.kind == "content":
                content_parts.append(chunk.text)
            elif chunk.kind == "reasoning":
                # thinking mode 模型（如 DeepSeek-R1/推理模型）在调工具前会
                # 先思考。这段 reasoning 必须在下轮请求里作为
                # reasoning_content 传回，否则上游 400。
                reasoning_parts.append(chunk.text)
            elif chunk.kind == "tool_call" and chunk.tool_call is not None:
                d = chunk.tool_call
                slot = calls.setdefault(d.index, {"id": "", "name": "", "arguments": ""})
                if d.call_id:
                    slot["id"] = d.call_id
                if d.name:
                    slot["name"] = d.name
                if d.arguments_delta:
                    slot["arguments"] += d.arguments_delta

        text = "".join(content_parts)
        reasoning = "".join(reasoning_parts)
        tool_calls = [
            ToolCall(
                call_id=slot["id"] or f"call_{idx}",
                name=slot["name"],
                arguments=slot["arguments"] or "{}",
            )
            for idx, slot in sorted(calls.items())
            if slot["name"]
        ]
        return text, tool_calls, reasoning

    return llm_call


async def get_memory_extraction_llm_call(
    db: AsyncSession,
) -> MemoryLLMCall | None:
    """
    获取记忆提取用的 LLM 调用函数（purpose="memory"）。

    返回 `async (messages, tools) -> (text, tool_calls)` 可调用对象。
    没配置时返回 None，调用方应该降级处理（跳过提取）。

    Args:
        db: 数据库会话

    Returns:
        llm_call 函数或 None（未配置）
    """
    from app.modules.endpoint import service as endpoint_service

    try:
        model = await endpoint_service.resolve(db, purpose="memory")
    except Exception:  # noqa: BLE001
        return None

    import structlog

    log = structlog.get_logger(__name__)
    log.info(
        "memory_extraction_model_resolved",
        model_id=model.model_id,
        purpose=model.purpose,
        base_url=model.base_url,
    )

    return _make_llm_call(model)


async def get_embedding_model(db: AsyncSession) -> ResolvedModel | None:
    """
    获取嵌入模型（purpose="embedding"）。

    这是 memory.service.resolve_embedding_model() 的便捷封装。
    没配置时返回 None，调用方应该跳过向量化。

    Args:
        db: 数据库会话

    Returns:
        ResolvedModel 或 None（未配置）
    """
    from app.modules.memory import service as memory_service

    return await memory_service.resolve_embedding_model(db)


__all__ = [
    "get_memory_extraction_llm_call",
    "get_embedding_model",
]
