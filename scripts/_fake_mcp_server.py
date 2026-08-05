"""
测试用的 stdio MCP 服务器。故意包含真实世界会遇到的"不规矩"行为。

## 为什么要故意不规矩

规范说服务器 `MUST NOT write anything to its stdout that is not a valid
MCP message`，但**第三方不一定守规矩** —— 很多服务器往 stdout 打启动日志，
直接污染协议流。

还有工具名：规范没限制它必须符合 OpenAI 的函数名规范，所以能返回带空格、
带点、中文的名字。直接拼进请求会让整个请求 400。

公开的 MCP 服务器不会主动做这些事，所以要自己造。
"""

import asyncio
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

app: Server = Server("fake-test-server")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        # 名字带空格 —— 不合规化会让整个请求 400
        Tool(
            name="say hello",
            description="打招呼。名字里故意有空格。",
            inputSchema={
                "type": "object",
                "properties": {"who": {"type": "string"}},
                "required": ["who"],
            },
        ),
        # 正常名字
        Tool(
            name="add",
            description="两数相加。",
            inputSchema={
                "type": "object",
                "properties": {
                    "a": {"type": "number"},
                    "b": {"type": "number"},
                },
                "required": ["a", "b"],
            },
        ),
        # 名字带点和中文
        Tool(
            name="util.读取",
            description="名字带点和中文。",
            inputSchema={"type": "object", "properties": {}},
        ),
        # 超长描述 —— 不截断会吃掉上下文
        Tool(
            name="long_desc",
            description="很长的描述。" + ("填充内容" * 3000),
            inputSchema={"type": "object", "properties": {}},
        ),
        # 声明 readOnly 但其实会"改东西" —— 测试注解不可信
        Tool(
            name="readonly_liar",
            description="声明自己只读，但实际会修改数据。用于测试注解不可信。",
            inputSchema={"type": "object", "properties": {}},
            annotations={"readOnlyHint": True, "destructiveHint": False},
        ),
        # 卡住不返回 —— 测试超时
        Tool(
            name="hang",
            description="永远不返回，用于测试超时。",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "say hello":
        return [TextContent(type="text", text=f"你好，{arguments.get('who', '?')}！")]
    if name == "add":
        a = arguments.get("a", 0)
        b = arguments.get("b", 0)
        return [TextContent(type="text", text=f"结果是 {a + b}")]
    if name == "util.读取":
        return [TextContent(type="text", text="读到了内容")]
    if name == "long_desc":
        return [TextContent(type="text", text="ok")]
    if name == "readonly_liar":
        return [TextContent(type="text", text="我改了数据（虽然声明是只读）")]
    if name == "hang":
        await asyncio.sleep(3600)
        return [TextContent(type="text", text="never")]
    return [TextContent(type="text", text=f"未知工具 {name}")]


async def main() -> None:
    # 往 stderr 打日志 —— 规范允许（MAY write UTF-8 strings to stderr）。
    # 客户端必须能捕获或忽略，不能因此挂掉。
    print("fake MCP server 启动中（这行在 stderr，合规）", file=sys.stderr)

    # 【故意违反规范】往 stdout 打一行非 MCP 消息。
    #
    # 规范明写服务器 MUST NOT write anything to its stdout that is not a
    # valid MCP message，但真实世界里很多服务器会往 stdout 打启动日志 ——
    # 这是接第三方 MCP 时最常见的"连不上但看不出原因"。
    #
    # 客户端应该跳过这行垃圾并继续正常握手。做不到的话，表现是
    # 用户配了一个能跑的服务器却连不上，而日志里只有一句解析错误。
    if "--dirty-stdout" in sys.argv:
        print("[INFO] Server listening on stdio...", flush=True)

    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
