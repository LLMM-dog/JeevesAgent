"""
钩子系统 —— 会话级和工具级的拦截点。

两层钩子：
  - Loop 级：LLM 调用前后、消息落库、压缩、停止决策 → 挂在 AgentLoop.hooks
  - Tool 级：工具执行前后 → 挂在 ToolRegistry.hooks

设计原则：
  - 钩子做策略层的事，不做机制层的事
  - 钩子失败不崩掉 loop —— 异常被捕获并记录
  - BEFORE 类钩子返回 str → 阻止执行，返回 None → 放行
  - AFTER 类钩子纯观察，无返回值
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

import structlog

if TYPE_CHECKING:
    from app.modules.agent.tools.base import ToolContext, ToolResult
    from app.modules.agent.loop import _Accum, LoopResult
    from app.modules.agent.messages import Msg

log = structlog.get_logger(__name__)


# ──────────────────────────── Hook 点枚举 ────────────────────────────


class HookPoint(enum.Enum):
    """钩子触发时机。"""

    # Tool 级（在 ToolRegistry.execute 内触发）
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"

    # Loop 级（在 AgentLoop.run 内触发）
    AFTER_LLM = "after_llm"            # LLM 响应后，to_msg() 之前
    SHOULD_STOP = "should_stop"        # Agent 即将返回 final 时
    ON_COMPACT = "on_compact"          # 压缩完成后
    ON_MESSAGE = "on_message"          # 每条消息落库后


# ──────────────────────────── Tool 级上下文（已有） ────────────────────────────


@dataclass
class BeforeToolContext:
    tool_name: str
    args: dict[str, Any]
    ctx: ToolContext
    session_id: str
    agent_name: str


@dataclass
class AfterToolContext:
    tool_name: str
    args: dict[str, Any]
    result: ToolResult
    ctx: ToolContext
    session_id: str
    agent_name: str
    elapsed_ms: int


# ──────────────────────────── Loop 级上下文（新增） ────────────────────────────


@dataclass
class AfterLlmContext:
    """LLM 响应后触发。可检查 accum 决定是否继续。"""

    accum: _Accum
    turn: int
    session_id: str
    agent_name: str


@dataclass
class ShouldStopContext:
    """Agent 即将返回 final 时触发。返回 str 阻止停止、继续循环。"""

    session_id: str
    agent_name: str
    turn: int
    final_text: str


@dataclass
class OnCompactContext:
    """压缩完成后触发。"""

    session_id: str
    agent_name: str
    before_tokens: int
    after_tokens: int


@dataclass
class OnMessageContext:
    """每条消息落库后触发。"""

    msg: Msg
    session_id: str
    agent_name: str
    turn: int


# ──────────────────────────── 钩子函数签名 ────────────────────────────

BeforeToolHook = Callable[[BeforeToolContext], str | None]
AfterToolHook = Callable[[AfterToolContext], None]

AfterLlmHook = Callable[[AfterLlmContext], str | None]
ShouldStopHook = Callable[[ShouldStopContext], str | None]
OnCompactHook = Callable[[OnCompactContext], None]
OnMessageHook = Callable[[OnMessageContext], None]

HookFn = BeforeToolHook | AfterToolHook | AfterLlmHook | ShouldStopHook | OnCompactHook | OnMessageHook


# ──────────────────────────── HookRegistry ────────────────────────────


class HookRegistry:
    """钩子注册中心。可以挂在不同对象上（ToolRegistry / AgentLoop）。"""

    def __init__(self) -> None:
        self._before_tool: list[BeforeToolHook] = []
        self._after_tool: list[AfterToolHook] = []
        self._after_llm: list[AfterLlmHook] = []
        self._should_stop: list[ShouldStopHook] = []
        self._on_compact: list[OnCompactHook] = []
        self._on_message: list[OnMessageHook] = []

    # ── 注册 / 移除 ──

    def on(self, point: HookPoint, fn: HookFn) -> None:
        """注册一个钩子。"""
        if point == HookPoint.BEFORE_TOOL:
            self._before_tool.append(fn)  # type: ignore[arg-type]
        elif point == HookPoint.AFTER_TOOL:
            self._after_tool.append(fn)  # type: ignore[arg-type]
        elif point == HookPoint.AFTER_LLM:
            self._after_llm.append(fn)  # type: ignore[arg-type]
        elif point == HookPoint.SHOULD_STOP:
            self._should_stop.append(fn)  # type: ignore[arg-type]
        elif point == HookPoint.ON_COMPACT:
            self._on_compact.append(fn)  # type: ignore[arg-type]
        elif point == HookPoint.ON_MESSAGE:
            self._on_message.append(fn)  # type: ignore[arg-type]

    def off(self, point: HookPoint, fn: HookFn) -> None:
        """移除一个钩子。"""
        target = self._list_for(point)
        if target is not None:
            target[:] = [h for h in target if h is not fn]

    def clear(self, point: HookPoint | None = None) -> None:
        """清空钩子。不传 point 清空全部。"""
        if point is None:
            for lst in self._all_lists():
                lst.clear()
        else:
            target = self._list_for(point)
            if target is not None:
                target.clear()

    # ── 触发方法 ──

    def fire_before_tool(self, context: BeforeToolContext) -> str | None:
        for hook in self._before_tool:
            try:
                rejection = hook(context)
                if rejection is not None:
                    log.info("hook_blocked_tool", tool=context.tool_name, reason=rejection[:200])
                    return rejection
            except Exception:
                log.exception("hook_before_tool_failed", tool=context.tool_name)
        return None

    def fire_after_tool(self, context: AfterToolContext) -> None:
        for hook in self._after_tool:
            try:
                hook(context)
            except Exception:
                log.exception("hook_after_tool_failed", tool=context.tool_name)

    def fire_after_llm(self, context: AfterLlmContext) -> str | None:
        for hook in self._after_llm:
            try:
                rejection = hook(context)
                if rejection is not None:
                    log.info("hook_blocked_after_llm", turn=context.turn, reason=rejection[:200])
                    return rejection
            except Exception:
                log.exception("hook_after_llm_failed", turn=context.turn)
        return None

    def fire_should_stop(self, context: ShouldStopContext) -> str | None:
        for hook in self._should_stop:
            try:
                rejection = hook(context)
                if rejection is not None:
                    log.info("hook_should_stop_overridden", turn=context.turn, reason=rejection[:200])
                    return rejection
            except Exception:
                log.exception("hook_should_stop_failed", turn=context.turn)
        return None

    def fire_on_compact(self, context: OnCompactContext) -> None:
        for hook in self._on_compact:
            try:
                hook(context)
            except Exception:
                log.exception("hook_on_compact_failed")

    def fire_on_message(self, context: OnMessageContext) -> None:
        for hook in self._on_message:
            try:
                hook(context)
            except Exception:
                log.exception("hook_on_message_failed", turn=context.turn)

    # ── 内部 ──

    @property
    def has_hooks(self) -> bool:
        return any(lst for lst in self._all_lists())

    def _list_for(self, point: HookPoint) -> list[Any] | None:
        return {
            HookPoint.BEFORE_TOOL: self._before_tool,
            HookPoint.AFTER_TOOL: self._after_tool,
            HookPoint.AFTER_LLM: self._after_llm,
            HookPoint.SHOULD_STOP: self._should_stop,
            HookPoint.ON_COMPACT: self._on_compact,
            HookPoint.ON_MESSAGE: self._on_message,
        }.get(point)

    def _all_lists(self) -> list[list[Any]]:
        return [
            self._before_tool, self._after_tool,
            self._after_llm, self._should_stop,
            self._on_compact, self._on_message,
        ]
