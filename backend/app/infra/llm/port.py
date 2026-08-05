"""
LLM 抽象。

只有一个真实实现（openai_compat），但 port 仍然存在，理由是【测试】：
FakeLLM 直接实现这个 Protocol，返回预设的 chunk 序列，
agent loop 的测试就不需要 mock HTTP 或真实调用 API。

只支持 OpenAI 兼容协议。绝大多数供应商（DeepSeek/Kimi/智谱/通义/OpenRouter/
SiliconFlow）和几乎所有中转站都提供兼容端点，Ollama/vLLM/LM Studio 本地部署
也都提供。一条协议路径 = 一份代码 = 一处 bug。
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

ChunkKind = Literal["content", "reasoning", "tool_call", "usage", "done"]


@dataclass
class ToolCallDelta:
    """
    流式工具调用的增量。

    index 是必需的：一轮里可能有多个 tool_call 并行流式返回，
    上游用 index 标识这个增量属于第几个 call。只靠 id 不行 ——
    id 只在第一个 chunk 里出现，后续 chunk 只有 index 和 arguments 片段。
    """

    index: int
    call_id: str | None = None
    name: str | None = None
    arguments_delta: str = ""


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # 推理 token。实测一次演示请求 22921 个 completion token 里 20994 是推理
    # token（91%），这个数字对理解"为什么等了 220 秒"很关键。
    reasoning_tokens: int = 0


@dataclass
class LLMChunk:
    kind: ChunkKind
    # kind=content / reasoning 时的文本增量
    text: str = ""
    # kind=tool_call 时
    tool_call: ToolCallDelta | None = None
    # kind=usage 时
    usage: TokenUsage | None = None
    # kind=done 时上游给的结束原因（stop / tool_calls / length / ...）
    finish_reason: str | None = None


@dataclass
class ResolvedModel:
    """一次 LLM 调用需要的全部信息。由 provider 模块解析绑定后产出。"""

    model_id: str
    base_url: str
    api_key: str
    context_window: int = 32768
    supports_vision: bool = False
    # 供日志和事件用，不参与请求
    provider_name: str = ""
    purpose: str = "chat"
    # 每百万 token 单价（USD）。NULL 表示未配价，不是免费 ——
    # span 行里存这个快照值，好让历史成本可复算（价格会变）。
    price_in_per_1m: float | None = None
    price_out_per_1m: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMPort(Protocol):
    def stream_chat(
        self,
        model: ResolvedModel,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[LLMChunk]:
        """
        始终用流式请求 —— 非流式在长推理时更容易被中间层掐断。
        非流式的调用方只是不订阅事件，代码路径完全相同。
        """
        ...

    async def list_models(self, base_url: str, api_key: str) -> list[str]: ...

    async def probe_chat(
        self,
        *,
        base_url: str,
        api_key: str,
        model_id: str,
        messages: list[dict[str, Any]],
    ) -> str:
        """
        一次性探测请求，返回完整文本。

        ## 为什么这里可以非流式

        上面 stream_chat 的注释说"始终用流式" —— 那是针对**真实对话**的，
        因为长推理时非流式容易被中间层掐断。

        但探测不一样：它是交互式操作（用户在设置页等着），发的是最小请求，
        期望几秒内返回。非流式在这里更简单，且不需要处理 SSE 分片。

        ## 为什么不复用 ResolvedModel

        探测发生在【模型入库之前】—— 用户刚填完供应商信息，还没有
        model 行，也就构造不出 ResolvedModel。所以直接收三个原始参数。
        """
        ...
