"""
系统提示词的组装测试。

重点是那些"写错了不报错、只是让模型表现变差"的部分 ——
它们没有测试就会静默退化。
"""

from app.modules.agent import prompts


class TestEnvPart:
    def test_tells_model_to_use_relative_paths(self) -> None:
        """
        必须明确要求用相对路径。

        真实模型验证时观测到的问题：提示词只给了工作区绝对路径而没说用相对路径，
        模型就手抄那个七八层深的绝对路径，把
        `D:\\studywork\\python\\PycharmProjects\\jeeves\\workspace`
        抄成了 `D:\\studywork\\PycharmProjects\\jeeves\\workspace`
        （漏掉 python 那一段），被白名单拒绝，白费一轮。

        加上这段说明后同一个任务的工具调用从 5 次降到 4 次。
        """
        text = prompts.build_system_prompt(workspace="D:\\a\\b\\ws", tool_names=["read_file"])
        assert "相对路径" in text
        assert "不要写绝对路径" in text

    def test_includes_workspace_and_tools(self) -> None:
        text = prompts.build_system_prompt(
            workspace="D:\\ws", tool_names=["read_file", "write_file"]
        )
        assert "D:\\ws" in text
        assert "read_file" in text
        assert "write_file" in text

    def test_no_tools_does_not_crash(self) -> None:
        text = prompts.build_system_prompt(workspace="/ws", tool_names=[])
        assert "（无）" in text

    def test_parts_have_stable_keys(self) -> None:
        """
        分块要有稳定的 key —— 前端的提示词预览页按 key 展示，
        改名会让它显示不出来。
        """
        parts = prompts.get_prompt_parts(workspace="/ws", tool_names=["glob"])
        keys = [p.key for p in parts]
        assert "env" in keys
        # 同一 key 不能重复，否则预览页会出现两个同名块
        assert len(keys) == len(set(keys))

    def test_skills_appended_when_present(self) -> None:
        text = prompts.build_system_prompt(
            workspace="/ws",
            tool_names=["glob"],
            skills=[("pdf-fill", "填写 PDF 表单")],
        )
        assert "pdf-fill" in text
        assert "填写 PDF 表单" in text
