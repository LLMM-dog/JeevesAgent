"""
钩子系统测试（扩展版）—— 覆盖 Loop 级钩子。
"""

from __future__ import annotations

from typing import Any

import pytest
from app.modules.agent.hooks import (
    AfterToolContext,
    BeforeToolContext,
    HookPoint,
    HookRegistry,
)
from app.modules.agent.tools.base import ToolContext, ToolRegistry, ToolResult


def _registry() -> ToolRegistry:
    return ToolRegistry()


def _ctx() -> ToolContext:
    from pathlib import Path

    return ToolContext(
        session_id="s1",
        run_id="r1",
        workspace=Path("/tmp"),
        db=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
    )


class _NoopTool:
    name = "noop"
    description = "无操作工具"
    requires_approval = False

    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
        return ToolResult(content="ok")


# ──────────────────────────── Tool 级（已有） ────────────────────────────


class TestToolHooks:
    async def test_before_tool_blocks(self) -> None:
        reg = _registry()
        reg.register(_NoopTool())
        reg.hooks.on(HookPoint.BEFORE_TOOL, lambda c: "阻止")

        result = await reg.execute(_ctx(), "noop", {})
        assert result.is_error
        assert "阻止" in result.content

    async def test_after_tool_sees_result(self) -> None:
        reg = _registry()
        reg.register(_NoopTool())
        captured: list[AfterToolContext] = []
        reg.hooks.on(HookPoint.AFTER_TOOL, lambda c: captured.append(c))

        await reg.execute(_ctx(), "noop", {"k": "v"})
        assert len(captured) == 1
        assert captured[0].tool_name == "noop"
        assert captured[0].args == {"k": "v"}
        assert captured[0].result.content == "ok"

    async def test_forked_independent_hooks(self) -> None:
        parent = _registry()
        parent.hooks.on(HookPoint.BEFORE_TOOL, lambda c: "父级阻止")
        child = parent.forked()
        child.register(_NoopTool())
        result = await child.execute(_ctx(), "noop", {})
        assert not result.is_error


# ──────────────────────────── Loop 级（新增） ────────────────────────────


class TestLoopHooks:
    """Loop 级钩子：AFTER_LLM / SHOULD_STOP / ON_COMPACT / ON_MESSAGE。"""

    def test_after_llm_can_reject(self) -> None:
        reg = HookRegistry()
        reg.on(HookPoint.AFTER_LLM, lambda c: "模型输出不安全，需要重新生成" if c.turn > 5 else None)

        from app.modules.agent.hooks import AfterLlmContext
        from app.modules.agent.loop import _Accum

        accum = _Accum()
        accum.content = ["hello"]

        # turn <= 5: 放行
        assert reg.fire_after_llm(AfterLlmContext(accum=accum, turn=3, session_id="s", agent_name="a")) is None
        # turn > 5: 阻止
        assert reg.fire_after_llm(AfterLlmContext(accum=accum, turn=10, session_id="s", agent_name="a")) is not None

    def test_should_stop_can_override(self) -> None:
        reg = HookRegistry()
        should_continue: list[bool] = []

        def guard(ctx) -> str | None:
            should_continue.append(True)
            # 如果文本里没有"验证通过"，就让 agent 继续
            if "验证通过" not in ctx.final_text:
                return "请先跑测试验证你的修改，确认通过后再结束。"
            return None

        reg.on(HookPoint.SHOULD_STOP, guard)

        from app.modules.agent.hooks import ShouldStopContext

        # 没通过验证 → 阻止停止
        rejection = reg.fire_should_stop(ShouldStopContext(session_id="s", agent_name="a", turn=5, final_text="完成了"))
        assert rejection is not None
        assert "测试验证" in rejection

        # 通过了 → 放行
        rejection = reg.fire_should_stop(ShouldStopContext(session_id="s", agent_name="a", turn=5, final_text="验证通过，任务完成"))
        assert rejection is None

    def test_on_compact_observes(self) -> None:
        reg = HookRegistry()
        captured: list[dict] = []

        reg.on(HookPoint.ON_COMPACT, lambda c: captured.append({"before": c.before_tokens, "after": c.after_tokens}))

        from app.modules.agent.hooks import OnCompactContext

        reg.fire_on_compact(OnCompactContext(session_id="s", agent_name="a", before_tokens=5000, after_tokens=2000))
        assert captured == [{"before": 5000, "after": 2000}]

    def test_on_message_fires(self) -> None:
        reg = HookRegistry()
        captured: list[str] = []

        reg.on(HookPoint.ON_MESSAGE, lambda c: captured.append(c.msg.role))

        from app.modules.agent.hooks import OnMessageContext
        from app.modules.agent.messages import Msg

        reg.fire_on_message(OnMessageContext(msg=Msg(role="user", content="hi"), session_id="s", agent_name="a", turn=1))
        reg.fire_on_message(OnMessageContext(msg=Msg(role="assistant", content="hello"), session_id="s", agent_name="a", turn=1))
        reg.fire_on_message(OnMessageContext(msg=Msg(role="tool", content="result", tool_call_id="c1"), session_id="s", agent_name="a", turn=1))

        assert captured == ["user", "assistant", "tool"]

    def test_each_hook_isolated(self) -> None:
        """不同类型的钩子互不干扰。"""
        reg = HookRegistry()

        tool_calls: list[str] = []
        compact_calls: list[str] = []

        reg.on(HookPoint.BEFORE_TOOL, lambda c: (tool_calls.append("before"), None)[1])
        reg.on(HookPoint.ON_COMPACT, lambda c: compact_calls.append("compact"))

        reg.fire_before_tool(BeforeToolContext("x", {}, _ctx(), "s", "a"))
        from app.modules.agent.hooks import OnCompactContext
        reg.fire_on_compact(OnCompactContext(session_id="s", agent_name="a", before_tokens=0, after_tokens=0))

        assert tool_calls == ["before"]
        assert compact_calls == ["compact"]
