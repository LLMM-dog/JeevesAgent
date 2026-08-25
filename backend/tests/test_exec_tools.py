"""
run_shell 工具的测试。

进程层的行为（超时杀树、stdin、截断）在 test_sandbox_local.py 里测过了，
这里测的是工具层：参数处理、cwd 白名单、错误转成给模型的文本。
"""

import sys
from pathlib import Path
from typing import Any

import pytest
from app.core.config import settings
from app.modules.agent.pathguard import AllowedPath, set_allowed
from app.modules.agent.tools.base import ToolContext
from app.modules.agent.tools.exec import RunShellTool, _clamp_timeout

WIN = sys.platform == "win32"


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


class TestClampTimeout:
    """
    模型会给出各种值：字符串、0、负数、999999。

    不夹的话 wait_for(timeout=0) 立刻超时，而 999999 等于没有超时。
    """

    @pytest.mark.parametrize(
        "given,expected",
        [
            (None, settings.sandbox.timeout_default),
            ("", settings.sandbox.timeout_default),
            (0, settings.sandbox.timeout_default),
            (-5, settings.sandbox.timeout_default),
            ("abc", settings.sandbox.timeout_default),
            (30, 30),
            ("45", 45),
            (999_999, settings.sandbox.timeout_max),
        ],
    )
    def test_clamps(self, given: Any, expected: int) -> None:
        assert _clamp_timeout(given) == expected


class TestRunShell:
    async def test_runs_and_returns_output(self, ws: Path) -> None:
        cmd = "Write-Output 'shell works'" if WIN else "echo 'shell works'"
        r = await RunShellTool().run(mk_ctx(ws), command=cmd)
        assert r.is_error is False
        assert "shell works" in r.content
        assert r.display is not None
        assert r.display["exit_code"] == 0

    async def test_empty_command_rejected(self, ws: Path) -> None:
        r = await RunShellTool().run(mk_ctx(ws), command="   ")
        assert r.is_error is True
        assert "不能为空" in r.content

    async def test_nonzero_exit_is_error_but_keeps_output(self, ws: Path) -> None:
        """
        失败时输出和状态一起给，不是只给一句错误。

        命令工具完全忽略退出码（cmd.py 不读 returncode），
        模型只能从 stderr 文本猜。
        """
        cmd = (
            "Write-Output 'did some work'; exit 7"
            if WIN
            else "echo 'did some work'; exit 7"
        )
        r = await RunShellTool().run(mk_ctx(ws), command=cmd)
        assert r.is_error is True
        assert "did some work" in r.content, "丢掉了已捕获的输出"
        assert "7" in r.content

    async def test_timeout_marked(self, ws: Path) -> None:
        cmd = "Start-Sleep -Seconds 30" if WIN else "sleep 30"
        r = await RunShellTool().run(mk_ctx(ws), command=cmd, timeout=2)
        assert r.is_error is True
        assert "超时" in r.content
        assert r.display is not None
        assert r.display["timed_out"] is True

    async def test_cwd_relative_to_workspace(self, ws: Path) -> None:
        """
        相对路径按工作区解析 —— 模型不用手抄绝对路径。

        这是先前踩过的坑：提示词只给了绝对路径，模型手抄时漏掉一段目录名，
        被白名单拒绝，白费一轮。
        """
        sub = ws / "subdir"
        sub.mkdir()
        (sub / "here.txt").write_text("x", encoding="utf-8")

        cmd = "Get-ChildItem -Name" if WIN else "ls"
        r = await RunShellTool().run(mk_ctx(ws), command=cmd, cwd="subdir")
        assert r.is_error is False
        assert "here.txt" in r.content

    async def test_cwd_outside_whitelist_denied(self, ws: Path, tmp_path: Path) -> None:
        """
        执行目录也要过白名单。

        不能阻止刻意的破坏（命令本身能干任何事），但能挡住绝大多数
        "路径写错了"的意外 —— cwd 决定了 `rm -rf *` 删的是哪里。
        """
        outside = tmp_path.parent / "outside_ws"
        outside.mkdir(exist_ok=True)
        cmd = "Write-Output x" if WIN else "echo x"
        r = await RunShellTool().run(
            mk_ctx(ws), command=cmd, cwd=str(outside)
        )
        assert r.is_error is True

    async def test_cwd_not_a_directory(self, ws: Path) -> None:
        f = ws / "afile.txt"
        f.write_text("x", encoding="utf-8")
        cmd = "Write-Output x" if WIN else "echo x"
        r = await RunShellTool().run(mk_ctx(ws), command=cmd, cwd="afile.txt")
        assert r.is_error is True
        assert "不是目录" in r.content

    async def test_description_warns_about_stdin(self) -> None:
        """
        工具描述必须告诉模型"不能读标准输入"。

        否则它会用 `pip install`（等 y/n）这类交互式命令，然后拿到
        一个莫名的 EOF 错误。说清楚了它会自己加 -y。
        """
        desc = RunShellTool.description
        assert "标准输入" in desc or "stdin" in desc.lower()
        assert "-y" in desc or "非交互" in desc

    async def test_description_mentions_cwd(self) -> None:
        """
        必须提到 cwd 参数 —— 模型需要知道可以指定工作目录。
        """
        desc = RunShellTool.description
        assert "cwd" in desc.lower()
