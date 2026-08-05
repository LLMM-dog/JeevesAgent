"""
测试替身。

FakeLLM 直接实现 LLMPort，返回预设的 chunk 序列 —— agent loop 的测试
不需要 mock HTTP 也不需要真实调用 API。这是 LLMPort 存在的主要理由
（生产只有一个实现）。
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from app.infra.llm.port import LLMChunk, ResolvedModel, TokenUsage, ToolCallDelta
from app.modules.agent.tools.base import ToolContext, ToolResult


def text_chunks(text: str, *, usage: TokenUsage | None = None) -> list[LLMChunk]:
    """把文本切成逐字 chunk，模拟真实流式。"""
    out = [LLMChunk(kind="content", text=ch) for ch in text]
    out.append(LLMChunk(kind="done", finish_reason="stop"))
    if usage:
        out.append(LLMChunk(kind="usage", usage=usage))
    return out


def tool_call_chunks(
    name: str, args: str, *, call_id: str = "call_1", index: int = 0
) -> list[LLMChunk]:
    """
    模拟分片到达的 tool_call：id/name 只在第一个 chunk，
    arguments 分多个 chunk。这是真实上游的行为。
    """
    out = [
        LLMChunk(
            kind="tool_call",
            tool_call=ToolCallDelta(index=index, call_id=call_id, name=name),
        )
    ]
    mid = len(args) // 2
    for piece in (args[:mid], args[mid:]):
        out.append(
            LLMChunk(kind="tool_call", tool_call=ToolCallDelta(index=index, arguments_delta=piece))
        )
    out.append(LLMChunk(kind="done", finish_reason="tool_calls"))
    return out


class FakeLLM:
    """
    scripts[0] 是第一轮的响应，scripts[1] 是第二轮…
    这样可以编排"第一轮调工具、第二轮给答案"这类多轮场景。
    """

    def __init__(self, scripts: list[list[LLMChunk]]) -> None:
        self.scripts = scripts
        self.calls = 0
        self.received: list[list[dict[str, Any]]] = []

    async def stream_chat(  # type: ignore[override]
        self,
        model: ResolvedModel,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMChunk]:
        self.received.append(messages)
        idx = min(self.calls, len(self.scripts) - 1)
        self.calls += 1
        for chunk in self.scripts[idx]:
            # 让出控制权，使取消能在流中间生效
            await asyncio.sleep(0)
            yield chunk

    async def list_models(self, base_url: str, api_key: str) -> list[str]:
        return ["fake-model"]


def fake_model(context_window: int = 32768) -> ResolvedModel:
    return ResolvedModel(
        model_id="fake-model",
        base_url="http://fake/v1",
        api_key="sk-fake",
        context_window=context_window,
        provider_name="fake",
    )


class EchoTool:
    name = "echo"
    description = "回显传入的文本，用于测试"
    requires_approval = False

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        return ToolResult(content=f"echo: {kwargs.get('text', '')}", display={"ok": True})


class HangTool:
    """永久挂住，用于测试取消。"""

    name = "hang"
    description = "永不返回"
    requires_approval = False

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        self.entered.set()
        await asyncio.sleep(3600)
        return ToolResult(content="never")


class BoomTool:
    name = "boom"
    description = "总是抛异常"
    requires_approval = False

    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        raise RuntimeError("炸了")
