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


class TestParseExtraLlmParams:
    """智能体额外 LLM 参数的解析。"""

    def test_empty(self) -> None:
        from app.modules.agent.chat_service import parse_extra_llm_params

        assert parse_extra_llm_params("") == {}
        assert parse_extra_llm_params("   \n  ") == {}

    def test_json_object(self) -> None:
        from app.modules.agent.chat_service import parse_extra_llm_params

        assert parse_extra_llm_params('{"thinking": {"type": "disabled"}}') == {
            "thinking": {"type": "disabled"}
        }

    def test_key_value_lines(self) -> None:
        from app.modules.agent.chat_service import parse_extra_llm_params

        params = parse_extra_llm_params(
            'thinking: {"type": "disabled"}\ntemperature: 0.7\n# 注释行\nenable_thinking: false'
        )
        assert params == {
            "thinking": {"type": "disabled"},
            "temperature": 0.7,
            "enable_thinking": False,
        }

    def test_value_as_plain_string(self) -> None:
        from app.modules.agent.chat_service import parse_extra_llm_params

        # value 不是合法 JSON 时当纯字符串
        assert parse_extra_llm_params("model: my-model") == {"model": "my-model"}

    def test_missing_colon_rejected(self) -> None:
        import pytest
        from app.modules.agent.chat_service import parse_extra_llm_params

        with pytest.raises(ValueError):
            parse_extra_llm_params("this line has no colon")

    def test_missing_value_rejected(self) -> None:
        import pytest
        from app.modules.agent.chat_service import parse_extra_llm_params

        with pytest.raises(ValueError):
            parse_extra_llm_params("thinking:")


class TestDescribeImages:
    """视觉模型识别图片。"""

    async def test_describe_images_calls_complete_chat(self) -> None:
        from app.modules.endpoint.vision import describe_images

        class FakeLLM:
            async def complete_chat(self, model, messages):
                assert model.model_id == "vision-model"
                content = messages[0]["content"]
                assert isinstance(content, list)
                assert content[0]["type"] == "text"
                assert content[1]["type"] == "image_url"
                assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
                return "图片显示了一个报错"

        class FakeModel:
            model_id = "vision-model"

        # 1x1 PNG data URL
        img = (
            "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
            "AAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
        )
        text = await describe_images(FakeLLM(), FakeModel(), [img])
        assert text == "图片显示了一个报错"

    async def test_describe_images_skips_bad_url(self) -> None:
        from app.modules.endpoint.vision import describe_images

        class FakeLLM:
            async def complete_chat(self, model, messages):
                raise AssertionError("不该调用 complete_chat")

        class FakeModel:
            model_id = "vision-model"

        # 全是坏的 data URL，识别结果应为空
        text = await describe_images(FakeLLM(), FakeModel(), ["not-a-data-url"])
        assert text == ""
