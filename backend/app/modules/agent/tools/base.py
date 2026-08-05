"""
工具协议、上下文与注册表。

用 Protocol 而非 ABC，因为工具来源不止一处：内置工具是自己写的类，
MCP 工具是运行时动态构造的，技能里的脚本工具也是动态的。
强制继承会让动态构造变别扭。
"""

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from app.core.config import settings
from app.core.events import Ev, emit
from app.core.exceptions import PathDeniedError
from app.core.runtime_state import ApprovalMode, register_approval, resolve_approval

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.infra.llm.port import LLMPort

log = structlog.get_logger(__name__)


@dataclass
class ToolResult:
    """
    content 与 display 分离是关键设计。

    模型需要紧凑文本（省 token），前端需要结构化数据（好渲染）。
    例：read_file 给模型的 content 是带行号的文件内容，给前端的 display 是
    {"path":..., "lines":120, "language":"python"} —— 前端只需显示一张
    "读取了 xxx.py（120 行）"的卡片，不需要把文件内容再渲染一遍。

    照抄 双消费者思路：同一份返回值，模型看到错误文本能自我纠正，
    前端看到 is_error 显示红色错误态。
    """

    content: str
    display: dict[str, Any] | None = None
    is_error: bool = False
    # 工具产出的"工作成果"。非 None 时 loop 会写一条 role=artifact 的消息。
    #
    # ## 由工具决定，不由模型决定
    #
    # write_file 写出的完整文件算 artifact，edit_file 的零散改动不算。
    # 让模型决定的话它会把一切都标成产物（它倾向于认为自己做的都重要），
    # artifact 就失去了"只留最新一版"的意义。
    artifact: "ArtifactPayload | None" = None


@dataclass
class ArtifactPayload:
    """
    一份工作成果。

    ## 为什么要专门的角色而不是普通 assistant 消息

    artifact 有三条特殊待遇，普通消息给不了：
      1. 排除在压缩之外 —— 用户说"把刚才那份代码改一下"时它必须还在
      2. 每个 (session, agent) 只留最新一版 —— 否则改 5 次会累积 5 份
      3. 钉在上下文末尾 —— 按时序插入会埋在中间，模型注意不到
    """

    kind: str
    content: str
    path: str | None = None


@dataclass
class ToolContext:
    """
    所有请求级依赖装在一个 dataclass 里，不层层传参。
    新增工具需要新依赖时只改这一处 —— 反面做法是给 run() 加参数，
    那要改所有工具的签名。
    """

    session_id: str
    run_id: str
    workspace: Path
    db: "AsyncSession"
    llm: "LLMPort"
    agent_name: str = ""
    depth: int = 0
    registry: "ToolRegistry | None" = None
    # 当前正在执行的 tool_call id。审批要靠它把前端的回复配对回来。
    #
    # 放在 ctx 上而不是当 execute 的参数：审批发生在 registry.execute 内部，
    # 而工具自身也可能需要它（比如 ask_user 那类要等回复的工具）。
    # 每次 execute 前由 loop 写入。
    current_call_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def approval_mode(self) -> ApprovalMode:
        """
        每次读都从 runtime_state 取，不缓存在这个 dataclass 里 ——
        用户可能在流式进行中切换审批模式，缓存住就读不到新值了。
        见 core/runtime_state.py 的模块 docstring。
        """
        from app.core.runtime_state import get_approval_mode

        return get_approval_mode(self.session_id)


@runtime_checkable
class Tool(Protocol):
    name: str
    description: str
    requires_approval: bool

    def parameters(self) -> dict[str, Any]:
        """JSON Schema。"""
        ...

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult: ...


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            log.warning("tool_overwritten", name=tool.name)
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def to_specs(self) -> list[dict[str, Any]]:
        """转成 LLM 的 tools 参数。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters(),
                },
            }
            for t in (self._tools[n] for n in self.names())
        ]

    def forked(self) -> "ToolRegistry":
        """
        浅拷贝。

        进程级的 ToolRegistry 单例被所有请求共享，往里加请求级工具
        （MCP 工具、某个会话专属工具）会污染全局 ——
        【一个会话配的 MCP 工具会出现在所有会话的工具列表里】。
        所以请求开始时 forked() 拿到拷贝，请求级工具只往拷贝里加。
        """
        clone = ToolRegistry()
        clone._tools = dict(self._tools)
        return clone

    async def _await_approval(
        self, ctx: ToolContext, tool: "Tool", args: dict[str, Any]
    ) -> ToolResult | None:
        """
        执行前等人确认。返回 None 表示放行，返回 ToolResult 表示被拒。

        ## 这件事容易出的问题

        - ****：写好了 Future 基础设施，但 `block_for_permission` 的函数体
          就是一个 `pass`，命令工具也不调用它。
        - ****：唯一真正实现的，asyncio.Future + WebSocket 往返。
          但**无超时**（tool_python.py:77 是裸 `await future`），用户不在电脑前
          就永久挂起。
        - **一种常见实现**：明确不做，只给同步策略钩子。README:499 写着
          "No permission popups. Run in a container..."

        ## 超时视为拒绝

        这是本项目与 关键分歧。无限等待的后果很实际：
        用户点了"开始"就去开会，回来发现界面卡在审批框上，中间几十分钟
        什么都没发生。而超时视为**批准**更糟 —— 用户离开电脑时不应该有
        命令自己执行起来。

        所以超时视为拒绝，并把"超时"这件事本身告诉模型，让它知道不是
        用户主动拒绝的。
        """
        mode = ctx.approval_mode
        if mode == "auto":
            return None

        call_id = ctx.current_call_id or ""
        if not call_id:
            # 拿不到 call_id 就没法配对回填。这种情况下放行而不是卡住 ——
            # 卡住会让整个 run 挂死，而放行至少还有工具自身的路径白名单兜着。
            log.warning("approval_skipped_no_call_id", tool=tool.name)
            return None

        fut = register_approval(ctx.session_id, call_id)
        await emit(
            Ev.APPROVAL_REQUIRED,
            call_id=call_id,
            tool_name=tool.name,
            # 给用户看的是完整参数 —— 审批一条命令时必须能看到命令原文，
            # 只显示工具名等于让人盲签。
            args=args,
            # 发【截止时刻】而不是剩余秒数。事件到达前端有网络延迟，
            # 标签页在后台还会被节流；给剩余秒数的话前端得自己记事件到达
            # 时间，而那个时间本身就已经偏了。
            timeout_at=int((time.time() + settings.agent.approval_timeout) * 1000),
        )
        log.info("approval_requested", tool=tool.name, call_id=call_id)

        try:
            approved = await asyncio.wait_for(
                fut, timeout=settings.agent.approval_timeout
            )
        except TimeoutError:
            resolve_approval(ctx.session_id, call_id, approved=False)
            await emit(Ev.APPROVAL_RESOLVED, call_id=call_id, approved=False, reason="timeout")
            log.info("approval_timeout", tool=tool.name, call_id=call_id)
            return ToolResult(
                content=(
                    f"操作未执行：等待用户确认超时"
                    f"（{settings.agent.approval_timeout} 秒）。"
                    "用户可能不在电脑前。如果这一步是必要的，"
                    "可以先说明你要做什么并等用户回来确认。"
                ),
                is_error=True,
            )

        await emit(Ev.APPROVAL_RESOLVED, call_id=call_id, approved=approved)
        if approved:
            log.info("approval_granted", tool=tool.name, call_id=call_id)
            return None

        log.info("approval_denied", tool=tool.name, call_id=call_id)
        # 拒绝理由要回给模型，否则它会原样重试。
        # tool_python.py:92 是同样的做法。
        return ToolResult(
            content=(
                f"用户拒绝了这次 {tool.name} 调用。"
                "不要重复同样的调用 —— 先问清用户的顾虑，或者换一种方式。"
            ),
            is_error=True,
        )

    async def execute(self, ctx: ToolContext, name: str, args: dict[str, Any]) -> ToolResult:
        """
        一条铁律：本方法【永不向上抛异常】。工具失败是 agent 的正常工作状态，
        不是系统故障。唯一例外是 CancelledError —— 它必须往上传，
        否则取消功能失效。
        """
        tool = self.get(name)
        if tool is None:
            # 未知工具不抛异常。模型偶尔会幻觉出不存在的工具名，或在 MCP
            # 服务器掉线后继续调它的工具。返回错误文本让模型自我纠正，
            # 比让整轮对话崩掉好得多。
            log.warning("unknown_tool_called", name=name)
            return ToolResult(
                content=f"工具 {name} 不存在。可用工具：{', '.join(self.names())}",
                is_error=True,
            )

        if getattr(tool, "requires_approval", False):
            verdict = await self._await_approval(ctx, tool, args)
            if verdict is not None:
                return verdict

        try:
            return await tool.run(ctx, **args)
        except PathDeniedError as e:
            # 路径被拒也是给模型的信息，不是程序错误。
            #
            # 【hint 必须一起给】。只给 message 的话模型不知道哪些路径是
            # 允许的，只能靠猜 —— 真实模型实测：一次任务里 11 个工具调用有
            # 8 个是在猜路径（换 glob、换 run_shell 的 cwd、换相对路径前缀），
            # 全部被拒。hint 里有白名单清单，它看一眼就知道该去哪。
            #
            # 这个坑很典型：异常对象带了有用信息，而转成给模型的文本时丢了。
            parts = [f"路径访问被拒绝：{e.message}"]
            hint = getattr(e, "hint", "")
            if hint:
                parts.append(str(hint))
            return ToolResult(content="\n".join(parts), is_error=True)
        except TypeError as e:
            # 参数名不匹配（模型给了工具不认识的参数）。单独处理是因为
            # 这个错误的修复方式很明确，可以直接告诉模型正确的参数名。
            log.warning("tool_bad_args", tool=name, args=args, err=str(e))
            return ToolResult(
                content=f"调用 {name} 的参数不正确：{e}。正确的参数定义：{tool.parameters()}",
                is_error=True,
            )
        except Exception as e:
            log.exception("tool_failed", tool=name)
            return ToolResult(content=f"工具执行失败：{type(e).__name__}: {e}", is_error=True)
