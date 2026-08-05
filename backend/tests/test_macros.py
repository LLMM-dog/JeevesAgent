"""
宏的测试。

关键验证：宏【不进系统提示词】。在文档里区分了 Skill 和 Macro，
但运行时两者毫无差异 —— 它的 _scan_macros 和 _scan_anthropic_skills 是复制
粘贴，宏照样占常驻上下文位，`type: macro` 字段解析器根本不读。
"""

from pathlib import Path
from typing import Any

import pytest
from app.modules.agent import prompts
from app.modules.skill import macros


def write_macro(root: Path, name: str, *, desc: str = "测试宏", body: str = "# 流程\n1. 做事") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    f = d / "MACRO.md"
    f.write_text(
        f"---\nname: {name}\ntype: macro\ndescription: {desc}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return f


@pytest.fixture(autouse=True)
def clean() -> Any:
    macros.reset()
    yield
    macros.reset()


class TestLoading:
    def test_loads(self, tmp_path: Path) -> None:
        write_macro(tmp_path, "daily", desc="整理日报")
        idx = macros.load_macros(tmp_path)
        assert idx.names() == ["daily"]
        assert idx.macros["daily"].description == "整理日报"

    def test_missing_dir(self, tmp_path: Path) -> None:
        idx = macros.load_macros(tmp_path / "nope")
        assert idx.macros == {}

    def test_missing_description_skipped(self, tmp_path: Path) -> None:
        d = tmp_path / "nodesc"
        d.mkdir()
        (d / "MACRO.md").write_text("---\nname: x\n---\n\n正文", encoding="utf-8")
        idx = macros.load_macros(tmp_path)
        assert idx.macros == {}
        assert idx.diagnostics

    def test_one_bad_does_not_break_others(self, tmp_path: Path) -> None:
        write_macro(tmp_path, "good")
        bad = tmp_path / "bad"
        bad.mkdir()
        (bad / "MACRO.md").write_text("---\nname: [oops\n---\n", encoding="utf-8")
        idx = macros.load_macros(tmp_path)
        assert "good" in idx.macros

    def test_description_single_lined(self, tmp_path: Path) -> None:
        """和技能一样，description 会进前端列表，换行要压掉。"""
        d = tmp_path / "multi"
        d.mkdir()
        (d / "MACRO.md").write_text(
            '---\nname: multi\ndescription: "一行\\n\\n## 假标题"\n---\n', encoding="utf-8"
        )
        idx = macros.load_macros(tmp_path)
        assert "\n" not in idx.macros["multi"].description

    def test_no_recursion_into_subdirs(self, tmp_path: Path) -> None:
        """宏是单文件的，只看一层子目录。"""
        deep = tmp_path / "outer" / "inner"
        deep.mkdir(parents=True)
        (deep / "MACRO.md").write_text(
            "---\nname: deep\ndescription: 深层\n---\n", encoding="utf-8"
        )
        idx = macros.load_macros(tmp_path)
        assert "deep" not in idx.macros

    def test_reload(self, tmp_path: Path) -> None:
        write_macro(tmp_path, "one")
        idx1 = macros.load_macros(tmp_path)
        assert idx1.names() == ["one"]
        write_macro(tmp_path, "two")
        idx2 = macros.load_macros(tmp_path)
        assert idx2.names() == ["one", "two"]


class TestBody:
    def test_reads_body_without_frontmatter(self, tmp_path: Path) -> None:
        write_macro(tmp_path, "m", body="# 标题\n步骤一")
        idx = macros.load_macros(tmp_path)
        body = macros.read_macro_body(idx.macros["m"])
        assert "步骤一" in body
        assert "description:" not in body

    def test_macro_dir_substituted(self, tmp_path: Path) -> None:
        write_macro(tmp_path, "m", body="见 ${MACRO_DIR}/notes.md")
        idx = macros.load_macros(tmp_path)
        body = macros.read_macro_body(idx.macros["m"])
        assert "${MACRO_DIR}" not in body


class TestMacrosNotInSystemPrompt:
    def test_prompt_has_no_macro_section(self, tmp_path: Path) -> None:
        """
        宏不占常驻上下文位。

        它是用户按 `!` 主动触发的，模型不需要"知道它存在"。而常驻位很贵：
        个人工作流会攒到几十个宏，全塞进系统提示词就是纯浪费。

        宏照样进系统提示词 —— 它的文档区分了两者，
        运行时却没有。
        """
        write_macro(tmp_path, "secret-flow", desc="这句话不该出现在系统提示词里")
        macros._index = macros.load_macros(tmp_path)  # type: ignore[attr-defined]

        text = prompts.build_system_prompt(workspace="/ws", tool_names=["read_file"])
        assert "secret-flow" not in text
        assert "这句话不该出现" not in text

    def test_skills_do_appear(self, tmp_path: Path) -> None:
        """对照：技能是要进系统提示词的（L1 常驻）。"""
        text = prompts.build_system_prompt(
            workspace="/ws",
            tool_names=["read_file"],
            skills=[("some-skill", "某个技能的描述")],
        )
        assert "some-skill" in text
        assert "某个技能的描述" in text


class TestBuiltinMacroCreator:
    def test_macro_creator_loads(self) -> None:
        """内置的 macro-creator 必须能被加载 —— 它是宏的自我扩展入口。"""
        from app.core.config import settings

        idx = macros.load_macros(settings.macros_dir)
        assert "macro-creator" in idx.macros, f"实得：{idx.names()}"
        body = macros.read_macro_body(idx.macros["macro-creator"])
        # 它必须教会模型判断"该不该做成宏"，而不是无脑创建
        assert "技能" in body
        assert "description" in body
