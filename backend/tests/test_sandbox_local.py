"""
子进程执行层的测试。

这些测试**真的起进程**，不 mock。原因是这一层要解决的问题全都只在真实
进程上出现：进程树、管道时序、平台差异。mock 掉之后测的就是 mock 本身。

常见实现的教训对应关系见 app/infra/sandbox/local.py 的模块 docstring。
"""

import sys
import time
from pathlib import Path

import pytest
from app.core.config import settings
from app.infra.sandbox.local import (
    ExecResult,
    build_env,
    resolve_shell,
    run_process,
)

# 跨平台的命令写法。Windows 上是 PowerShell，POSIX 上是 bash。
WIN = sys.platform == "win32"


def echo(text: str) -> str:
    return f"Write-Output '{text}'" if WIN else f"echo '{text}'"


def sleep_cmd(seconds: float) -> str:
    return f"Start-Sleep -Seconds {seconds}" if WIN else f"sleep {seconds}"


def exit_with(code: int) -> str:
    return f"exit {code}"


class TestBasicExecution:
    async def test_captures_stdout(self, tmp_path: Path) -> None:
        r = await run_process(echo("hello"), cwd=tmp_path, timeout=30)
        assert "hello" in r.output
        assert r.exit_code == 0
        assert r.failed is False
        assert r.timed_out is False

    async def test_captures_stderr_merged(self, tmp_path: Path) -> None:
        """
        stderr 合并进 stdout，保留真实交错顺序。

        分开读再拼接（cmd.py:102 的做法）会丢掉时序 ——
        报错发生在哪一步输出之后，模型就看不出来了。
        """
        cmd = (
            "Write-Output 'out1'; [Console]::Error.WriteLine('err1'); Write-Output 'out2'"
            if WIN
            else "echo out1; echo err1 >&2; echo out2"
        )
        r = await run_process(cmd, cwd=tmp_path, timeout=30)
        assert "out1" in r.output
        assert "err1" in r.output
        assert "out2" in r.output

    async def test_nonzero_exit_marked_failed_but_output_kept(
        self, tmp_path: Path
    ) -> None:
        """
        非零退出要连同已捕获的输出一起返回。

        按同类实现的做法
        模型只能猜。而失败时最有用的信息恰好是"输出到哪一步失败的"。
        """
        cmd = (
            f"Write-Output 'partial work'; {exit_with(3)}"
            if WIN
            else f"echo 'partial work'; {exit_with(3)}"
        )
        r = await run_process(cmd, cwd=tmp_path, timeout=30)
        assert r.exit_code == 3
        assert r.failed is True
        assert "partial work" in r.output, "失败时丢掉了已捕获的输出"

    async def test_cwd_respected(self, tmp_path: Path) -> None:
        (tmp_path / "marker.txt").write_text("x", encoding="utf-8")
        cmd = "Get-ChildItem -Name" if WIN else "ls"
        r = await run_process(cmd, cwd=tmp_path, timeout=30)
        assert "marker.txt" in r.output

    async def test_utf8_output(self, tmp_path: Path) -> None:
        """
        中文输出不能变成乱码。

        Windows 上 Python 默认用 GBK 写 stdout，不设 PYTHONIOENCODING
        会得到乱码或 UnicodeEncodeError。
        """
        r = await run_process(echo("中文测试"), cwd=tmp_path, timeout=30)
        assert "中文测试" in r.output


class TestExitCodeFidelity:
    """
    退出码必须是子进程的真实值，不能被 shell 归一化。

    实测踩过：PowerShell `-Command` 把所有非零退出都变成 1，
    除非显式 `exit $LASTEXITCODE`。

    后果不只是"数字不对"：很多工具靠退出码传语义 ——
    pytest 用 1 表示有测试失败、grep 用 1 表示没匹配到、
    git diff --quiet 用 1 表示有差异、命令找不到是 127。
    全归一成 1 之后模型无法区分这些情况。
    """

    @pytest.mark.parametrize("code", [1, 2, 7, 42, 127])
    async def test_exact_exit_code_preserved(
        self, tmp_path: Path, code: int
    ) -> None:
        script = tmp_path / "e.py"
        script.write_text(f"import os; os._exit({code})", encoding="utf-8")
        prefix = "& " if WIN else ""
        cmd = f'{prefix}"{sys.executable}" "{script}"'
        r = await run_process(cmd, cwd=tmp_path, timeout=30)
        assert r.exit_code == code, f"退出码被归一化了：期望 {code} 实得 {r.exit_code}"

    async def test_zero_stays_zero(self, tmp_path: Path) -> None:
        """成功的命令不能因为补了 exit $LASTEXITCODE 而变成失败。"""
        r = await run_process(echo("ok"), cwd=tmp_path, timeout=30)
        assert r.exit_code == 0
        assert r.failed is False

    async def test_pure_shell_command_succeeds(self, tmp_path: Path) -> None:
        """
        纯 shell 命令（没调用任何外部程序）也要返回 0。

        PowerShell 下 $LASTEXITCODE 此时是 $null，
        `exit $null` 等价于 `exit 0` —— 这正是想要的。
        """
        cmd = "$x = 1 + 1" if WIN else "x=$((1+1))"
        r = await run_process(cmd, cwd=tmp_path, timeout=30)
        assert r.exit_code == 0
        assert r.failed is False


class TestTimeout:
    async def test_timeout_kills_and_marks(self, tmp_path: Path) -> None:
        started = time.monotonic()
        r = await run_process(sleep_cmd(30), cwd=tmp_path, timeout=2)
        elapsed = time.monotonic() - started

        assert r.timed_out is True
        assert r.exit_code is None
        assert "超时" in r.output
        # 必须真的在超时后就返回，不能等命令自己结束
        assert elapsed < 15, f"超时没生效，耗了 {elapsed:.1f}s"

    async def test_partial_output_kept_on_timeout(self, tmp_path: Path) -> None:
        """
        超时前已经产出的输出必须保留 —— 那往往是判断卡在哪里的唯一线索。
        """
        cmd = (
            f"Write-Output 'before hang'; {sleep_cmd(30)}"
            if WIN
            else f"echo 'before hang'; {sleep_cmd(30)}"
        )
        r = await run_process(cmd, cwd=tmp_path, timeout=3)
        assert r.timed_out is True
        assert "before hang" in r.output, "超时时丢掉了已产出的输出"

    @pytest.mark.skipif(not WIN, reason="taskkill 是 Windows 专有")
    async def test_timeout_kills_child_processes_windows(self, tmp_path: Path) -> None:
        """
        Windows 上超时要杀整棵进程树（taskkill /F /T）。

        Windows 没有进程组语义，所以走的是和 POSIX 完全不同的代码路径 ——
        必须单独验证。实测过这个用例：不加 /T 时子进程会存活并在超时后写文件。
        """
        import asyncio

        marker = tmp_path / "child_wrote.txt"
        child = f"Start-Sleep -Seconds 6; Set-Content -Path '{marker}' -Value alive"
        cmd = (
            f"Start-Process -FilePath powershell "
            f"-ArgumentList '-NoProfile','-Command',\"{child}\" -WindowStyle Hidden; "
            f"Start-Sleep -Seconds 30"
        )
        r = await run_process(cmd, cwd=tmp_path, timeout=2)
        assert r.timed_out is True

        await asyncio.sleep(8)
        assert not marker.exists(), "子进程没被杀掉，它在超时后仍然写了文件"

    @pytest.mark.skipif(WIN, reason="进程组是 POSIX 概念，Windows 走 taskkill /T")
    async def test_timeout_kills_child_processes(self, tmp_path: Path) -> None:
        """
        超时要杀【整棵进程树】，不能只杀直接子进程。

        cmd.py:129 是 process.kill，只杀 docker exec 客户端，
        容器内进程继续跑 —— 实际存在的资源泄漏。

        这里验证：shell 起一个后台 sleep 然后自己 sleep，超时后
        后台的那个也必须死。
        """
        marker = tmp_path / "child_alive.txt"
        # 后台进程 5 秒后写文件。如果进程树被正确杀掉，文件不会出现。
        cmd = f"(sleep 5; touch '{marker}') & sleep 30"
        r = await run_process(cmd, cwd=tmp_path, timeout=2)
        assert r.timed_out is True

        # 等超过后台进程的 5 秒
        import asyncio

        await asyncio.sleep(6)
        assert not marker.exists(), "子进程没被杀掉，它在超时后仍然写了文件"


class TestStdinClosed:
    async def test_command_waiting_for_input_does_not_hang(
        self, tmp_path: Path
    ) -> None:
        """
        等 stdin 输入的命令必须立刻拿到 EOF，不能挂死到超时。

        少见实现做了（stdio 的 stdin 设成 ignore）。
        让子进程继承 stdin，于是 `read` / 等 y/n 确认的命令
        会一直阻塞到 600 秒超时。
        """
        cmd = "$x = Read-Host; Write-Output \"got:$x\"" if WIN else "read x; echo got:$x"
        started = time.monotonic()
        r = await run_process(cmd, cwd=tmp_path, timeout=20)
        elapsed = time.monotonic() - started

        # 关键：没有挂到超时
        assert r.timed_out is False, "等输入的命令挂死了"
        assert elapsed < 15, f"耗了 {elapsed:.1f}s，说明在等 stdin"


class TestOutputTruncation:
    async def test_long_output_truncated_keeping_tail(self, tmp_path: Path) -> None:
        """
        输出过长时保留尾部。

        命令输出的关键信息在末尾：编译错误、测试结果、最终状态。
        保留头部（python_code_runner.py:154 那样）会把 traceback 切掉。
        """
        n = settings.sandbox.max_output_lines + 500
        cmd = (
            f"1..{n} | ForEach-Object {{ Write-Output \"line$_\" }}"
            if WIN
            else f"seq 1 {n} | sed 's/^/line/'"
        )
        r = await run_process(cmd, cwd=tmp_path, timeout=60)

        assert r.truncated is True
        assert r.total_lines >= n
        # 最后一行必须在
        assert f"line{n}" in r.output
        # 第一行应该被截掉
        assert "line1\n" not in r.output

    async def test_full_output_saved_to_file(self, tmp_path: Path) -> None:
        """
        截断后完整输出要落盘，并把路径告知模型 —— 它可以自己 read_file 去看。

        这是 同类实现。
        """
        n = settings.sandbox.max_output_lines + 200
        cmd = (
            f"1..{n} | ForEach-Object {{ Write-Output \"row$_\" }}"
            if WIN
            else f"seq 1 {n} | sed 's/^/row/'"
        )
        r = await run_process(cmd, cwd=tmp_path, timeout=60)

        assert r.truncated is True
        assert r.full_output_path is not None
        saved = Path(r.full_output_path)
        assert saved.exists()
        content = saved.read_text(encoding="utf-8")
        # 完整输出里第一行和最后一行都要有
        assert "row1\n" in content
        assert f"row{n}" in content
        # 路径要出现在给模型的文本里
        assert r.full_output_path in r.output
        saved.unlink(missing_ok=True)

    async def test_full_output_path_is_readable_by_read_file(
        self, tmp_path: Path
    ) -> None:
        """
        落盘位置必须在路径白名单能覆盖的地方。

        放系统临时目录的话模型拿到路径也读不了 —— 那落盘就白做了。
        这里验证它落在 data/tmp 下。
        """
        n = settings.sandbox.max_output_lines + 50
        cmd = (
            f"1..{n} | ForEach-Object {{ Write-Output $_ }}"
            if WIN
            else f"seq 1 {n}"
        )
        r = await run_process(cmd, cwd=tmp_path, timeout=60)
        assert r.full_output_path is not None
        assert settings.temp_dir.resolve() in Path(r.full_output_path).resolve().parents
        Path(r.full_output_path).unlink(missing_ok=True)

    async def test_total_lines_counts_bytes_dropped_from_memory(
        self, tmp_path: Path
    ) -> None:
        """
        "共 N 行"必须是真实总数，不能是留在内存里的行数。

        实测踩过：4000 行输出因为超字节上限只留了 247 行，提示写成
        "仅显示第 1~247 行（共 247 行）" —— 自相矛盾，而且模型会以为
        输出就这么多，不去读完整文件。
        """
        script = tmp_path / "big.py"
        # 每行 200 字符，4000 行 ≈ 800KB，远超 50KB 的字节上限
        script.write_text(
            "for i in range(1, 4001):\n    print(f'{i:05d} ' + 'x'*200)\n",
            encoding="utf-8",
        )
        prefix = "& " if WIN else ""
        r = await run_process(
            f'{prefix}"{sys.executable}" "{script}"', cwd=tmp_path, timeout=90
        )

        assert r.truncated is True
        assert r.total_lines == 4000, f"总行数算错了：{r.total_lines}"
        assert r.shown_lines < r.total_lines
        # 提示里的总数也要对
        assert "共 4000 行" in r.output
        if r.full_output_path:
            Path(r.full_output_path).unlink(missing_ok=True)

    async def test_spilled_file_really_has_everything(self, tmp_path: Path) -> None:
        """
        "完整输出已保存"这个承诺必须是真的。

        实测踩过：内存只保留尾部（防止 while true 的 echo 吃掉几个 GB），
        而落盘是在截断时写内存内容 —— 于是 4000 行的输出，落盘文件也只有
        247 行。模型按提示去读，读到的还是残缺的。

        修法是【边读边落盘】：文件拿全部字节，内存只留尾部，两者各自完整。
        """
        script = tmp_path / "big2.py"
        script.write_text(
            "for i in range(1, 4001):\n    print(f'{i:05d} ' + 'y'*200)\n",
            encoding="utf-8",
        )
        prefix = "& " if WIN else ""
        r = await run_process(
            f'{prefix}"{sys.executable}" "{script}"', cwd=tmp_path, timeout=90
        )

        assert r.full_output_path is not None
        saved = Path(r.full_output_path)
        lines = saved.read_text(encoding="utf-8", errors="replace").splitlines()
        assert len(lines) == 4000, f"落盘文件不完整：只有 {len(lines)} 行"
        # 头尾都要在 —— 头部是被从内存里丢掉的那部分
        assert lines[0].startswith("00001")
        assert lines[-1].startswith("04000")
        saved.unlink(missing_ok=True)

    async def test_spill_dir_is_readable_by_read_file(self) -> None:
        """
        落盘目录必须在 read_file 的白名单里。

        实测踩过：输出被截断时我们告诉模型"完整输出在 data/tmp/xxx.txt"，
        但那个目录不在白名单里 —— 模型照提示去 read_file，拿到的是
        "路径不在白名单内"。落盘做了、路径给了、读不到，整条链路白费。

        这个测试直接验证 startup 注册的白名单包含 temp_dir。
        """
        from app.modules.agent.pathguard import AllowedPath, get_guard, set_allowed

        # 模拟 startup 的注册（main.py 里会把 temp_dir 追加进去）
        set_allowed(
            [
                AllowedPath(path=settings.workspace_dir.resolve(), can_write=True),
                AllowedPath(path=settings.temp_dir.resolve(), can_write=False),
            ]
        )
        settings.temp_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.temp_dir / "probe_readable.txt"
        probe.write_text("x", encoding="utf-8")
        try:
            # 读要放行
            get_guard().check(probe, write=False)
            # 写要拒绝 —— 这些文件是我们写给模型看的，它没理由改
            from app.core.exceptions import PathDeniedError

            with pytest.raises(PathDeniedError):
                get_guard().check(probe, write=True)
        finally:
            probe.unlink(missing_ok=True)

    async def test_no_spill_file_when_output_small(self, tmp_path: Path) -> None:
        """
        输出没超上限时不该建落盘文件 ——
        绝大多数命令输出很短，为它们建临时文件纯属浪费。
        """
        before = set(settings.temp_dir.glob("jeeves_output_*.txt"))
        r = await run_process(echo("tiny"), cwd=tmp_path, timeout=30)
        after = set(settings.temp_dir.glob("jeeves_output_*.txt"))
        assert r.full_output_path is None
        assert after == before

    async def test_short_output_not_truncated(self, tmp_path: Path) -> None:
        r = await run_process(echo("short"), cwd=tmp_path, timeout=30)
        assert r.truncated is False
        assert r.full_output_path is None


class TestEnvSanitization:
    def test_sensitive_vars_removed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        含 KEY / TOKEN / SECRET 的环境变量不能传给子进程。

        防的场景很具体：用户在系统里设了 OPENAI_API_KEY，模型执行 `env`
        把它打印出来 —— 那就直接进了上下文、日志、摘要。
        """
        monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
        monkeypatch.setenv("MY_SECRET_VALUE", "hunter2")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_xxx")
        monkeypatch.setenv("DB_PASSWORD", "pw")
        monkeypatch.setenv("HARMLESS_VAR", "keep-me")

        env = build_env()

        assert "OPENAI_API_KEY" not in env
        assert "MY_SECRET_VALUE" not in env
        assert "GITHUB_TOKEN" not in env
        assert "DB_PASSWORD" not in env
        assert env.get("HARMLESS_VAR") == "keep-me"

    def test_utf8_forced(self) -> None:
        env = build_env()
        assert env["PYTHONIOENCODING"] == "utf-8"
        assert env["PYTHONUNBUFFERED"] == "1"

    def test_extra_overrides(self) -> None:
        env = build_env({"CUSTOM": "v"})
        assert env["CUSTOM"] == "v"

    async def test_secret_not_visible_to_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """端到端验证：子进程真的看不到那个变量。"""
        monkeypatch.setenv("LEAK_TEST_API_KEY", "sk-secret-value")
        cmd = (
            "Write-Output $env:LEAK_TEST_API_KEY"
            if WIN
            else "echo ${LEAK_TEST_API_KEY:-<empty>}"
        )
        r = await run_process(cmd, cwd=tmp_path, timeout=30)
        assert "sk-secret-value" not in r.output


class TestShellResolution:
    def test_resolves_to_existing_shell(self) -> None:
        shell, args = resolve_shell()
        assert shell
        assert args
        if WIN:
            assert "powershell" in shell.lower() or "pwsh" in shell.lower() or "cmd" in shell.lower()
        else:
            assert args == ["-c"]

    def test_configured_shell_respected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings.sandbox, "shell_path", "/usr/bin/zsh")
        shell, args = resolve_shell()
        assert shell == "/usr/bin/zsh"
        assert args == ["-c"]


class TestErrorPaths:
    async def test_bad_shell_returns_error_not_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        shell 找不到时返回错误结果，不抛异常 ——
        工具层的铁律是永不向上抛。
        """
        monkeypatch.setattr(
            settings.sandbox, "shell_path", "/definitely/not/a/real/shell"
        )
        r = await run_process("echo x", cwd=tmp_path, timeout=10)
        assert isinstance(r, ExecResult)
        assert r.exit_code is None
        assert "shell" in r.output.lower() or "找不到" in r.output

    async def test_missing_cwd_returns_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        r = await run_process("echo x", cwd=missing, timeout=10)
        assert isinstance(r, ExecResult)
        # 不管是哪种失败方式，都不能抛异常
        assert r.exit_code != 0 or r.output
