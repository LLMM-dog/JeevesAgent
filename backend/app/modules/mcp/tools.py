"""
把 MCP 工具包装成本项目的 Tool。

## 为什么不用 langchain_mcp_adapters

一些实现（/ ）都用它。但本项目的工具层是自己的
`Tool` 协议，不是 LangChain 的 `BaseTool` —— 引入 adapter 等于多一层
无用转换，而且会把 LangChain 的工具抽象带进来。

官方 SDK 直接给 `ClientSession.list_tools()` / `call_tool()`，正好对上。
"""

from __future__ import annotations

from typing import Any

import structlog

from app.modules.agent.tools.base import ToolContext, ToolResult
from app.modules.mcp.config import RemoteTool, ServerConfig, is_destructive

log = structlog.get_logger(__name__)


class McpTool:
    """
    一个 MCP 远程工具。

    ## requires_approval 恒为 True

    规范：`clients MUST consider tool annotations to be untrusted`。

    注解里的 `readOnlyHint: true` 是服务器自己声明的 —— 据此跳过审批
    等于让被审查对象填审查结论。所以注解只用来【加严】（destructive 时
    额外警告），不用来放宽，基线是全部需要确认。

    这也是与 关键分歧：它把 MCP 工具和内置工具扁平合并
    ，而全仓只有 相关实现 一处需要确认 ——
    **第三方代码一个都不过审批**。
    """

    def __init__(self, cfg: ServerConfig, remote: RemoteTool) -> None:
        self._cfg = cfg
        self._remote = remote
        self.name = remote.name
        self.description = remote.description
        self.requires_approval = True
        # 服务器自述有破坏性时，在审批界面额外警告。
        # 单向采纳：说没有我们不信，说有我们信。
        self.destructive = is_destructive(remote)

    def parameters(self) -> dict[str, Any]:
        """
        入参 schema 直接用服务器给的。

        方法名必须是 `parameters` —— `Tool` 协议要求的就是这个名字，
        `ToolRegistry.to_specs()` 调的是 `t.parameters()`。

        最初写成了 `schema()`，而 Protocol 不做运行时检查、注册也不报错 ——
        直到真正发请求时 `to_specs()` 才会 AttributeError。而单测只直接
        调了 `schema()`，所以全都通过。见 开发笔记 里"鸭子类型的接口不匹配
        要靠测试覆盖真实调用路径"。

        不做深度校验 —— JSON Schema 的完整校验很重，而错误的 schema
        最终表现是模型传错参数、服务器报错，那个错误信息比我们自己
        编的更有用。

        但要保证顶层结构合法，否则某些模型会因 tools 字段格式不对
        直接 400（而错误信息不会指向具体哪个工具）。
        """
        s = self._remote.input_schema or {}
        if not isinstance(s, dict) or s.get("type") != "object":
            return {"type": "object", "properties": {}}
        return {
            "type": "object",
            "properties": s.get("properties") or {},
            **({"required": s["required"]} if isinstance(s.get("required"), list) else {}),
        }

    def preview(self, **kwargs: Any) -> str:
        """
        审批界面显示什么。

        规范客户端义务：`Show tool inputs to the user before calling the
        server, to avoid malicious or accidental data exfiltration`。

        参数必须显示 —— 用户要能看到"它想把什么发出去"。
        """
        import json

        try:
            args = json.dumps(kwargs, ensure_ascii=False)[:600]
        except (TypeError, ValueError):
            args = str(kwargs)[:600]
        head = f"MCP 服务器 {self._cfg.server_id} / 工具 {self._remote.raw_name}"
        if self.destructive:
            head += "\n⚠ 该服务器自述此工具有破坏性"
        return f"{head}\n参数：{args}"

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        from app.modules.mcp.manager import get_manager

        text, is_err = await get_manager().call(
            self._cfg.server_id, self._remote.raw_name, kwargs
        )
        log.info(
            "mcp_tool_called",
            run_id=ctx.run_id,
            server=self._cfg.server_id,
            tool=self._remote.raw_name,
            error=is_err,
            chars=len(text),
        )
        return ToolResult(
            content=text,
            is_error=is_err,
            display={"server_id": self._cfg.server_id, "raw_name": self._remote.raw_name},
        )


def build_tools() -> list[McpTool]:
    """把当前已连接的所有 MCP 工具包装成 Tool 列表。"""
    from app.modules.mcp.manager import get_manager

    return [McpTool(cfg, t) for cfg, t in get_manager().all_tools()]
