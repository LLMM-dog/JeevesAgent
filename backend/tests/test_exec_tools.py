"""
run_shell / run_python 工具的测试。

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
from app.modules.agent.tools.exec import (
    RunPythonTool,
    RunShellTool,
    _clamp_timeout,
)

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

    async def test_no_artifact_produced(self, ws: Path) -> None:
        """
        命令输出不是 artifact。

        artifact 是"当前工作成果"且常驻上下文。命令输出是过程信息，
        每次都不一样，把它钉在上下文里只会挤掉真正需要的历史。
        """
        cmd = "Write-Output x" if WIN else "echo x"
        r = await RunShellTool().run(mk_ctx(ws), command=cmd)
        assert r.artifact is None

    async def test_description_warns_about_stdin(self) -> None:
        """
        工具描述必须告诉模型"不能读标准输入"。

        否则它会用 `pip install`（等 y/n）这类交互式命令，然后拿到
        一个莫名的 EOF 错误。说清楚了它会自己加 -y。
        """
        desc = RunShellTool.description
        assert "标准输入" in desc or "stdin" in desc.lower()
        assert "-y" in desc or "非交互" in desc

    async def test_description_explains_cd_not_persisted(self) -> None:
        """
        必须说明每次调用是独立进程。

        不说的话模型会先 `cd src` 再单独调一次 `ls`，然后困惑为什么
        看到的还是根目录。
        """
        desc = RunShellTool.description
        assert "cd" in desc


class TestRunPython:
    async def test_runs_code(self, ws: Path) -> None:
        r = await RunPythonTool().run(mk_ctx(ws), code="print(2 + 3)")
        assert r.is_error is False
        assert "5" in r.content

    async def test_empty_code_rejected(self, ws: Path) -> None:
        r = await RunPythonTool().run(mk_ctx(ws), code="\n  \n")
        assert r.is_error is True

    async def test_traceback_has_real_line_numbers(self, ws: Path) -> None:
        """
        报错要能看到真实行号。

        用 `python -c` 传代码时 traceback 显示的是 "<string>"，
        模型无法定位。写临时文件才有行号。
        """
        code = "x = 1\ny = 2\nraise ValueError('boom')\n"
        r = await RunPythonTool().run(mk_ctx(ws), code=code)
        assert r.is_error is True
        assert "ValueError" in r.content
        assert "boom" in r.content
        assert "line 3" in r.content, "看不到真实行号，模型无法定位"

    async def test_uses_project_interpreter(self, ws: Path) -> None:
        """
        必须用 sys.executable，不是 PATH 上的 python。

        虚拟环境里 PATH 上的 python 可能是系统解释器，
        那样 import 项目依赖会失败。
        """
        code = "import sys; print(sys.executable)"
        r = await RunPythonTool().run(mk_ctx(ws), code=code)
        assert r.is_error is False
        # 至少是同一个解释器目录
        assert Path(sys.executable).stem.lower() in r.content.lower()

    async def test_can_import_project_deps(self, ws: Path) -> None:
        """项目依赖要能 import —— 这是用 sys.executable 的实际收益。"""
        r = await RunPythonTool().run(
            mk_ctx(ws), code="import structlog; print('dep ok')"
        )
        assert r.is_error is False
        assert "dep ok" in r.content

    async def test_temp_script_cleaned_up(self, ws: Path) -> None:
        """
        临时脚本要清理，否则临时目录无限增长。

        python_code_runner.py:214-220 同样放在 finally 里。
        """
        before = set(settings.temp_dir.glob("snippet_*.py"))
        await RunPythonTool().run(mk_ctx(ws), code="print('x')")
        after = set(settings.temp_dir.glob("snippet_*.py"))
        assert after == before, "临时脚本没清理"

    async def test_temp_script_cleaned_up_on_timeout(self, ws: Path) -> None:
        """超时路径也要清理 —— finally 的意义就在这里。"""
        before = set(settings.temp_dir.glob("snippet_*.py"))
        await RunPythonTool().run(
            mk_ctx(ws), code="import time; time.sleep(30)", timeout=2
        )
        after = set(settings.temp_dir.glob("snippet_*.py"))
        assert after == before

    async def test_utf8_code_and_output(self, ws: Path) -> None:
        """
        中文代码和输出都要正常。

        Windows 上不设 PYTHONIOENCODING 会得到乱码或 UnicodeEncodeError。
        """
        r = await RunPythonTool().run(
            mk_ctx(ws), code="print('中文输出正常')"
        )
        assert r.is_error is False
        assert "中文输出正常" in r.content

    async def test_not_in_process_exec(self, ws: Path) -> None:
        """
        必须是子进程，不是同进程 exec。

        run_python 在同进程 exec，工具描述写着
        "在隔离环境中执行"，但 get_safe_builtins 保留了 __import__ ——
        一行 `import os; os.system(...)` 就绕过了它的路径白名单。

        这里验证：代码里拿到的 PID 与当前进程不同。
        """
        import os

        r = await RunPythonTool().run(mk_ctx(ws), code="import os; print(os.getpid())")
        assert r.is_error is False
        child_pid = int(r.content.strip().splitlines()[-1])
        assert child_pid != os.getpid(), "在同进程里执行了 —— 崩溃会拖垮整个服务"

    async def test_crash_does_not_kill_server(self, ws: Path) -> None:
        """子进程崩溃不影响主进程。这是不用同进程 exec 的核心收益。"""
        r = await RunPythonTool().run(
            mk_ctx(ws), code="import os; os._exit(42)"
        )
        assert r.display is not None
        assert r.display["exit_code"] == 42
        # 主进程还活着才能跑到这里
        assert r.is_error is True
