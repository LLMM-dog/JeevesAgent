"""
文件工具的测试。

重点是 edit_file 匹配失败时的提示质量 —— 这是实测暴露的真实缺陷：
模型连续三次用几乎相同的 old_string 重试，三次都"未找到"，白烧三轮。
"""

from pathlib import Path

import pytest
from app.modules.agent.pathguard import AllowedPath, set_allowed
from app.modules.agent.tools.base import ToolContext
from app.modules.agent.tools.file import EditFileTool, _no_match_hint


def mk_ctx(ws: Path) -> ToolContext:
    return ToolContext(
        session_id="s",
        run_id="r",
        workspace=ws,
        db=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
    )


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    resolved = tmp_path.resolve()
    set_allowed([AllowedPath(path=resolved, can_write=True)])
    return resolved


SAMPLE = "def total(items):\n    s = 0\n    for it in items:\n        s += it\n    return s\n"


class TestNoMatchHint:
    """
    匹配失败的提示必须包含【文件里最接近的真实内容】。

    只说"没找到，请确认缩进"是不够的：模型记忆里的缩进与文件真实内容
    差了一点，而错误信息没告诉它差在哪，它只能继续猜。实测就是这样
    连续失败三次的。
    """

    def test_shows_nearest_real_content(self) -> None:
        # 模型记错了缩进（用了 2 空格而文件里是 4）
        hint = _no_match_hint(SAMPLE, "  for it in items:\n    s += it")
        assert "最接近的一段" in hint
        # 真实内容必须出现，且带行号便于定位
        assert "for it in items:" in hint
        assert "3|" in hint or "    3|" in hint

    def test_tells_model_line_numbers_are_not_content(self) -> None:
        """
        贴行号是为了定位，但必须说清它不是文件内容 ——
        否则模型会把 `  12| ` 抄进 old_string 里，下一轮继续失败。
        """
        hint = _no_match_hint(SAMPLE, "  for it in items:\n    s += it")
        assert "不是文件内容" in hint

    def test_unrelated_string_gets_no_misleading_snippet(self) -> None:
        """
        完全不相关时不该贴片段 —— 那会误导模型去改错地方。

        实测过阈值 0.35 太松："class Foo: pass" 对一个函数体也能拿到
        39% 相似度。改成 0.5。
        """
        hint = _no_match_hint(SAMPLE, "class Foo:\n    pass")
        assert "最接近的一段" not in hint
        assert "没有相近的内容" in hint
        # 至少告诉模型文件多大，让它决定怎么读
        assert "5 行" in hint

    def test_empty_file(self) -> None:
        hint = _no_match_hint("", "anything")
        assert "未找到" in hint

    def test_empty_old_string(self) -> None:
        hint = _no_match_hint(SAMPLE, "")
        assert "未找到" in hint

    def test_hint_is_actionable_for_whitespace_diff(self) -> None:
        """
        最常见的失败原因是空行差异。提示要能让模型看出来。
        """
        text = "class A:\n    def f(self):\n        pass\n\n\ndef g():\n    pass\n"
        # 模型以为只有一个空行
        hint = _no_match_hint(text, "        pass\n\ndef g():")
        assert "最接近的一段" in hint
        assert "def g():" in hint


class TestEditFileFailurePath:
    async def test_no_match_returns_hint_not_bare_error(self, ws: Path) -> None:
        f = ws / "m.py"
        f.write_text(SAMPLE, encoding="utf-8")

        r = await EditFileTool().run(
            mk_ctx(ws),
            path="m.py",
            old_string="  for it in items:\n    s += it",
            new_string="    for it in items:\n        s += it['price']",
        )
        assert r.is_error is True
        assert "最接近的一段" in r.content, "只给了'没找到'，模型无法定位差异"
        assert "for it in items:" in r.content

    async def test_successful_edit_still_works(self, ws: Path) -> None:
        f = ws / "ok.py"
        f.write_text(SAMPLE, encoding="utf-8")

        r = await EditFileTool().run(
            mk_ctx(ws),
            path="ok.py",
            old_string="        s += it",
            new_string="        s += it['price']",
        )
        assert r.is_error is False
        assert "it['price']" in f.read_text(encoding="utf-8")

    async def test_ambiguous_match_still_rejected(self, ws: Path) -> None:
        """
        多处匹配要拒绝，不能随便改第一处 ——
        改错地方比不改更糟，因为模型以为改对了。
        """
        f = ws / "dup.py"
        f.write_text("x = 1\ny = 2\nx = 1\n", encoding="utf-8")

        r = await EditFileTool().run(
            mk_ctx(ws), path="dup.py", old_string="x = 1", new_string="x = 9"
        )
        assert r.is_error is True
        # 原文不能被改动
        assert f.read_text(encoding="utf-8").count("x = 1") == 2


class TestEditRequiresApproval:
    def test_edit_and_write_gated(self) -> None:
        """
        改文件是不可逆的 —— 必须要人确认。
        """
        from app.modules.agent.tools.file import WriteFileTool

        assert EditFileTool.requires_approval is True
        assert WriteFileTool.requires_approval is True


class TestWriteFileInsert:
    async def test_insert_after_line(self, ws: Path) -> None:
        """write_file 的 insert_line 只写目标处，无需传整个文件。"""
        from app.modules.agent.tools.file import WriteFileTool

        (ws / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
        r = await WriteFileTool().run(mk_ctx(ws), path="a.py", content="z = 3", insert_line=1)
        assert r.is_error is False
        assert (ws / "a.py").read_text(encoding="utf-8") == "x = 1\nz = 3\ny = 2\n"

    async def test_insert_at_start(self, ws: Path) -> None:
        from app.modules.agent.tools.file import WriteFileTool

        (ws / "a.py").write_text("y = 2\n", encoding="utf-8")
        r = await WriteFileTool().run(mk_ctx(ws), path="a.py", content="x = 1", insert_line=0)
        assert r.is_error is False
        assert (ws / "a.py").read_text(encoding="utf-8") == "x = 1\ny = 2\n"

    async def test_insert_out_of_range(self, ws: Path) -> None:
        from app.modules.agent.tools.file import WriteFileTool

        (ws / "a.py").write_text("y = 2\n", encoding="utf-8")
        r = await WriteFileTool().run(mk_ctx(ws), path="a.py", content="z = 3", insert_line=99)
        assert r.is_error is True
        assert "超出范围" in r.content