"""
FastAPI 依赖。

进程级单例（ToolRegistry、ChatService）挂在 app.state 上，
不用模块级全局 —— 测试时可以整体替换。
"""

from app.modules.agent.chat_service import ChatService
from app.modules.agent.tools.base import ToolRegistry
from fastapi import Request


def get_registry(request: Request) -> ToolRegistry:
    return request.app.state.registry  # type: ignore[no-any-return]


def get_chat_service(request: Request) -> ChatService:
    return request.app.state.chat_service  # type: ignore[no-any-return]
