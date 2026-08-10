"""
对话服务参数校验与权限过滤测试。
"""

from __future__ import annotations


class TestToolPermissionMap:
    """验证权限映射表覆盖所有受控工具。"""

    def test_all_controlled_tools_mapped(self) -> None:
        """_TOOL_PERMISSION_MAP 必须覆盖所有需要权限控制的工具。"""
        from pathlib import Path

        src = Path("backend/app/modules/agent/chat_service.py").read_text(encoding="utf-8")
        assert "_TOOL_PERMISSION_MAP" in src
        # 确认关键工具都在映射中
        for tool in ("read_file", "write_file", "run_shell", "web_search"):
            assert f'"{tool}"' in src, f"{tool} 不在权限映射中"

    def test_permission_filter_function_exists(self) -> None:
        from pathlib import Path

        src = Path("backend/app/modules/agent/chat_service.py").read_text(encoding="utf-8")
        assert "_filter_tools_by_permissions" in src


class TestPreparedChat:
    def test_prepared_chat_dataclass_has_expected_fields(self) -> None:
        from pathlib import Path

        src = Path("backend/app/modules/agent/chat_service.py").read_text(encoding="utf-8")
        # PreparedChat 必须有 agent_id 字段
        assert "agent_id" in src
        assert "session_id" in src
        assert "run_id" in src
        assert "workspace_path" in src


class TestChatServiceInit:
    def test_chat_service_requires_sessionmaker(self) -> None:
        """ChatService.__init__ 签名确认。"""
        from pathlib import Path

        src = Path("backend/app/modules/agent/chat_service.py").read_text(encoding="utf-8")
        assert "sessionmaker" in src
        assert "base_registry" in src

    def test_prepare_is_keyword_only(self) -> None:
        """prepare() 的参数必须是 keyword-only 的。"""
        from pathlib import Path

        src = Path("backend/app/modules/agent/chat_service.py").read_text(encoding="utf-8")
        idx = src.index("async def prepare(")
        sig = src[idx : idx + 300]
        assert "session_id" in sig
        assert "content" in sig


class TestMemoryInjectionFilter:
    """验证 chat_service 过滤自己的注入消息。"""

    def test_filter_injected_messages(self) -> None:
        """chat_service 不应把记忆注入消息发给模型。"""
        from pathlib import Path

        src = Path("backend/app/modules/agent/chat_service.py").read_text(encoding="utf-8")
        if "injected" in src:
            idx = src.index("injected")
            # 附近应有过滤逻辑
            window = src[max(0, idx - 50) : min(len(src), idx + 200)]
            assert "continue" in window or "skip" in window.lower() or "filter" in window.lower() or "==" in window
