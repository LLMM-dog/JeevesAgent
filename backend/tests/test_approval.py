"""
审批门控的测试。

## 这件事容易出的问题

- ****：写好了 Future 基础设施，但 `block_for_permission` 的函数体
  就是 `pass`，命令工具也不调用它。
- ****：唯一真正实现的。但**无超时** —— tool_python.py:77
  是裸 `await future`，用户不在电脑前就永久挂起。
- **一种常见实现**：明确不做（README:499 "No permission popups"），只给同步策略钩子。

本项目的关键分歧是**超时视为拒绝**：用户离开电脑时不应该有命令自己执行，
但也不能让 run 永久挂死。
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from app.core import runtime_state
from app.core.config import settings
from app.core.events import Ev, EventBus, reset_bus, set_bus
from app.modules.agent.tools.base import ToolContext, ToolRegistry, ToolResult


class SpyTool:
    """记录自己是否被执行过。"""

    name = "danger"
    description = "危险操作"
    requires_approval = True

    def __init__(self) -> None:
        self.ran = False
        self.got_args: dict[str, Any] = {}

    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"cmd": {"type": "string"}}}

    async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
        self.ran = True
        self.got_args = kw
        return ToolResult(content="已执行")


class SafeTool:
    name = "safe"
    description = "安全操作"
    requires_approval = False

    def __init__(self) -> None:
        self.ran = False

    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
        self.ran = True
        return ToolResult(content="ok")


def mk_ctx(session_id: str, tmp_path: Path, call_id: str = "c1") -> ToolContext:
    return ToolContext(
        session_id=session_id,
        run_id="run_1",
        workspace=tmp_path,
        db=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        current_call_id=call_id,
    )


@pytest.fixture
def manual_mode() -> Any:
    """
    这些测试要的是 manual 模式。

    conftest 的 autouse fixture 把默认改成了 auto（否则任何调用 write_file
    的测试都会挂 300 秒等审批），审批测试要显式改回来。
    """
    previous = runtime_state.set_default_approval_mode("manual")
    runtime_state._sessions.clear()
    yield
    runtime_state.set_default_approval_mode(previous)
    runtime_state._sessions.clear()


class TestApprovalGate:
    async def test_auto_mode_runs_without_asking(self, tmp_path: Path) -> None:
        """auto 模式下不拦截（conftest 默认就是 auto）。"""
        tool = SpyTool()
        reg = ToolRegistry()
        reg.register(tool)
        ctx = mk_ctx("s_auto", tmp_path)

        result = await reg.execute(ctx, "danger", {"cmd": "rm -rf /"})
        assert tool.ran is True
        assert result.is_error is False

    async def test_safe_tool_not_gated(self, tmp_path: Path, manual_mode: Any) -> None:
        """requires_approval=False 的工具即使在 manual 模式也直接跑。"""
        tool = SafeTool()
        reg = ToolRegistry()
        reg.register(tool)
        ctx = mk_ctx("s_safe", tmp_path)

        result = await reg.execute(ctx, "safe", {})
        assert tool.ran is True
        assert result.is_error is False

    async def test_manual_mode_blocks_until_approved(
        self, tmp_path: Path, manual_mode: Any
    ) -> None:
        tool = SpyTool()
        reg = ToolRegistry()
        reg.register(tool)
        ctx = mk_ctx("s_wait", tmp_path)

        task = asyncio.create_task(reg.execute(ctx, "danger", {"cmd": "ls"}))
        # 等审批被注册
        for _ in range(100):
            await asyncio.sleep(0.01)
            if "c1" in runtime_state.get_runtime("s_wait").approvals:
                break
        assert tool.ran is False, "还没批准就执行了"

        runtime_state.resolve_approval("s_wait", "c1", approved=True)
        result = await task
        assert tool.ran is True
        assert result.is_error is False

    async def test_denied_returns_error_and_does_not_run(
        self, tmp_path: Path, manual_mode: Any
    ) -> None:
        tool = SpyTool()
        reg = ToolRegistry()
        reg.register(tool)
        ctx = mk_ctx("s_deny", tmp_path)

        task = asyncio.create_task(reg.execute(ctx, "danger", {"cmd": "ls"}))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if "c1" in runtime_state.get_runtime("s_deny").approvals:
                break

        runtime_state.resolve_approval("s_deny", "c1", approved=False)
        result = await task

        assert tool.ran is False
        assert result.is_error is True
        # 拒绝理由要回给模型，否则它会原样重试。
        # tool_python.py:92 是同样的做法。
        assert "拒绝" in result.content
        assert "不要重复" in result.content

    async def test_timeout_denies_not_approves(
        self, tmp_path: Path, manual_mode: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        超时视为【拒绝】。

        这是与 关键分歧 —— 它无限等待（tool_python.py:77 是
        裸 await），用户去开会回来发现界面卡了几十分钟。

        而超时视为批准更糟：用户离开电脑时不应该有命令自己执行起来。
        """
        monkeypatch.setattr(settings.agent, "approval_timeout", 1)
        tool = SpyTool()
        reg = ToolRegistry()
        reg.register(tool)
        ctx = mk_ctx("s_timeout", tmp_path)

        result = await reg.execute(ctx, "danger", {"cmd": "ls"})

        assert tool.ran is False, "超时后仍然执行了 —— 超时被当成批准了"
        assert result.is_error is True
        assert "超时" in result.content
        # 要让模型知道这不是用户主动拒绝
        assert "不在电脑前" in result.content

    async def test_missing_call_id_does_not_hang(
        self, tmp_path: Path, manual_mode: Any
    ) -> None:
        """
        拿不到 call_id 时放行而不是卡住。

        卡住会让整个 run 挂死；放行至少还有工具自身的路径白名单兜着。
        """
        tool = SpyTool()
        reg = ToolRegistry()
        reg.register(tool)
        ctx = mk_ctx("s_nocid", tmp_path, call_id="")

        result = await asyncio.wait_for(
            reg.execute(ctx, "danger", {"cmd": "ls"}), timeout=5
        )
        assert tool.ran is True
        assert result.is_error is False


class TestPathDeniedIncludesHint:
    async def test_hint_reaches_the_model(self, tmp_path: Path) -> None:
        """
        路径被拒时 hint 必须一起给模型。

        hint 里有【当前白名单清单】。只给 message 的话模型不知道哪些路径
        是允许的，只能靠猜 —— 真实模型实测：一次任务 11 个工具调用里有 8 个
        是在猜路径（换 glob、换 run_shell 的 cwd、换相对路径前缀），全部被拒。

        这个坑很典型：异常对象带了有用信息，转成给模型的文本时丢了。
        """
        from app.core.exceptions import PathDeniedError

        class DenyTool:
            name = "denier"
            description = "总是拒绝"
            requires_approval = False

            def parameters(self) -> dict[str, Any]:
                return {"type": "object", "properties": {}}

            async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
                raise PathDeniedError(
                    "路径不在白名单内：C:\\outside",
                    hint="当前白名单：D:\\ws, D:\\uploads",
                )

        reg = ToolRegistry()
        reg.register(DenyTool())
        result = await reg.execute(mk_ctx("s_hint", tmp_path), "denier", {})

        assert result.is_error is True
        assert "不在白名单内" in result.content
        assert "当前白名单" in result.content, "hint 被丢掉了，模型只能靠猜路径"
        assert "D:\\ws" in result.content


class TestApprovalEvents:
    async def test_emits_required_with_full_args(
        self, tmp_path: Path, manual_mode: Any
    ) -> None:
        """
        审批事件必须带完整参数。

        只发工具名等于让用户盲签 —— 审批一条命令时必须能看到命令原文。
        """
        tool = SpyTool()
        reg = ToolRegistry()
        reg.register(tool)
        ctx = mk_ctx("s_ev", tmp_path)

        bus = EventBus()
        token = set_bus(bus)
        try:
            task = asyncio.create_task(
                reg.execute(ctx, "danger", {"cmd": "rm -rf /tmp/x"})
            )
            for _ in range(100):
                await asyncio.sleep(0.01)
                if "c1" in runtime_state.get_runtime("s_ev").approvals:
                    break
            runtime_state.resolve_approval("s_ev", "c1", approved=True)
            await task
        finally:
            await bus.close()
            reset_bus(token)

        events = []
        while True:
            item = await bus.get()
            if item is None:
                break
            events.append(item)

        req = next(e for e in events if e["event"] == str(Ev.APPROVAL_REQUIRED))
        assert req["data"]["tool_name"] == "danger"
        assert req["data"]["args"]["cmd"] == "rm -rf /tmp/x", "没带命令原文，用户在盲签"
        # 发的是【截止时刻】而不是剩余秒数：事件到达前端有网络延迟，
        # 标签页在后台还会被节流，给剩余秒数前端算不准
        import time as _time

        assert req["data"]["timeout_at"] > _time.time() * 1000

    async def test_emits_resolved_so_frontend_can_close_dialog(
        self, tmp_path: Path, manual_mode: Any
    ) -> None:
        """
        有结果时也要发事件，否则弹窗永远留在界面上。

        超时、取消、在别处批准这几种情况都需要它。
        """
        tool = SpyTool()
        reg = ToolRegistry()
        reg.register(tool)
        ctx = mk_ctx("s_ev2", tmp_path)

        bus = EventBus()
        token = set_bus(bus)
        try:
            task = asyncio.create_task(reg.execute(ctx, "danger", {"cmd": "ls"}))
            for _ in range(100):
                await asyncio.sleep(0.01)
                if "c1" in runtime_state.get_runtime("s_ev2").approvals:
                    break
            runtime_state.resolve_approval("s_ev2", "c1", approved=False)
            await task
        finally:
            await bus.close()
            reset_bus(token)

        events = []
        while True:
            item = await bus.get()
            if item is None:
                break
            events.append(item)

        done = next(e for e in events if e["event"] == str(Ev.APPROVAL_RESOLVED))
        assert done["data"]["approved"] is False

    async def test_timeout_also_emits_resolved(
        self, tmp_path: Path, manual_mode: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings.agent, "approval_timeout", 1)
        reg = ToolRegistry()
        reg.register(SpyTool())
        ctx = mk_ctx("s_ev3", tmp_path)

        bus = EventBus()
        token = set_bus(bus)
        try:
            await reg.execute(ctx, "danger", {"cmd": "ls"})
        finally:
            await bus.close()
            reset_bus(token)

        events = []
        while True:
            item = await bus.get()
            if item is None:
                break
            events.append(item)

        done = next(e for e in events if e["event"] == str(Ev.APPROVAL_RESOLVED))
        assert done["data"]["approved"] is False
        assert done["data"]["reason"] == "timeout"


class TestModeSwitchTakesEffectImmediately:
    async def test_switching_to_auto_mid_run_is_visible(
        self, tmp_path: Path, manual_mode: Any
    ) -> None:
        """
        运行中切成 auto 要立刻生效。

        这是 runtime_state 用模块级 dict 而非 ContextVar 的全部理由 ——
        在 interaction.py:22-25 留了注释说明同一个坑：
        asyncio.create_task 会复制 context，task 启动后外部的 set() 对它不可见。

        用户会遇到的症状是"我都切成自动了它还在弹框"。
        """
        reg = ToolRegistry()
        tool = SpyTool()
        reg.register(tool)
        ctx = mk_ctx("s_switch", tmp_path)

        # 先在 manual 下起一个等待中的调用
        task = asyncio.create_task(reg.execute(ctx, "danger", {"cmd": "a"}))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if "c1" in runtime_state.get_runtime("s_switch").approvals:
                break
        runtime_state.resolve_approval("s_switch", "c1", approved=True)
        await task

        # 切成 auto，第二次调用不该再等
        runtime_state.set_approval_mode("s_switch", "auto")
        ctx.current_call_id = "c2"
        tool.ran = False
        result = await asyncio.wait_for(
            reg.execute(ctx, "danger", {"cmd": "b"}), timeout=5
        )
        assert tool.ran is True
        assert result.is_error is False
        assert "c2" not in runtime_state.get_runtime("s_switch").approvals

    async def test_cancel_releases_pending_approval(
        self, tmp_path: Path, manual_mode: Any
    ) -> None:
        """
        取消时要释放挂起的审批，否则 run 挂死。

        用 set_result(False) 而非 cancel（照 做法）：
        工具在 await 这个 future，set_result 让它能正常返回"被拒绝"，
        而 cancel 会在工具内部抛 CancelledError，破坏工具自己的清理逻辑。
        """
        reg = ToolRegistry()
        tool = SpyTool()
        reg.register(tool)
        ctx = mk_ctx("s_cancel", tmp_path)

        task = asyncio.create_task(reg.execute(ctx, "danger", {"cmd": "a"}))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if "c1" in runtime_state.get_runtime("s_cancel").approvals:
                break

        runtime_state.cancel_all_pending("s_cancel")
        result = await asyncio.wait_for(task, timeout=5)

        assert tool.ran is False
        assert result.is_error is True


class TestExecToolsRequireApproval:
    def test_run_shell_and_run_python_are_gated(self) -> None:
        """
        执行类工具必须标 requires_approval。

        一条命令能做的事没有上界：curl | sh、删库、把 SSH key 传出去。
        路径白名单在这里帮不上忙，命令字符串的正则匹配也不是安全边界
        （同类实现-mode 自己就把 curl 放在白名单里）。
        """
        from app.modules.agent.tools.exec import RunPythonTool, RunShellTool

        assert RunShellTool.requires_approval is True
        assert RunPythonTool.requires_approval is True

    def test_write_and_edit_are_gated(self) -> None:
        """写文件同样要审批 —— 覆盖用户的文件是不可逆的。"""
        from app.modules.agent.tools.file import EditFileTool, WriteFileTool

        assert WriteFileTool.requires_approval is True
        assert EditFileTool.requires_approval is True

    def test_read_only_tools_are_not_gated(self) -> None:
        """只读工具不该弹框 —— 那会让审批变成噪音，用户很快就全点通过。"""
        from app.modules.agent.tools.file import (
            GlobTool,
            GrepTool,
            ListDirTool,
            ReadFileTool,
        )

        for cls in (ReadFileTool, ListDirTool, GlobTool, GrepTool):
            assert cls.requires_approval is False, f"{cls.__name__} 不该需要审批"
