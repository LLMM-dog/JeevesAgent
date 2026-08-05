"""
子进程执行。

## 为什么单独一层

这件事很容易一处做对、别处做错，所以把要点集中在一个入口：

| 问题 | 常见做法 | 本项目 |
| --- | --- | --- | --- | --- |
| 超时杀进程 | 只杀 `docker exec` 客户端，容器内进程残留 | 无 | 杀整个进程树 | 杀整个进程树 |
| stdin | 继承，等输入的命令会挂死 | 无 | `ignore`，不会挂死 | `DEVNULL` |
| 输出截断 | 单一字符阈值，两个工具策略还不一致 | **不截断** | 行/字节双限 + 落盘 | 行/字节双限 + 落盘 |
| 退出码 | 命令工具完全忽略 | 无 | 非零抛错但保留输出 | 非零标记错误但保留输出 |
| 退出后排空管道 | 无 | 无 | idle grace 重置计时器 | idle grace 重置计时器 |

## 最容易被忽略的那个坑

有实现记录过相关 issue：

> A short-lived child can `exit` while a detached descendant keeps its
> stdout/stderr pipe open. We must not resolve and destroy the streams on a
> fixed deadline measured from `exit`, or output still being written past that
> deadline is silently lost.

翻译成本项目的场景：命令是 `python build.py &`（后台起一个进程）时，`python`
主进程立刻退出，但后台进程还持着 stdout 管道。如果在主进程 exit 后按固定
时限就关流，那些还在写的输出会被静默丢掉 —— 而且丢的通常正是错误信息。

解法是 exit 之后**等管道空闲**：每收到一个 chunk 就重置计时器。还在写的
进程能一直被读到，而只是继承了句柄却不写的进程也能在 grace 后放行。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import structlog
from app.core.config import settings

log = structlog.get_logger(__name__)


@dataclass
class ExecResult:
    """一次子进程执行的结果。"""

    output: str
    exit_code: int | None
    timed_out: bool
    truncated: bool
    total_lines: int
    shown_lines: int
    full_output_path: str | None
    duration_ms: int

    @property
    def failed(self) -> bool:
        """
        非零退出算失败，但【被信号杀死】（exit_code is None）不算。

        超时和取消走的正是信号路径，它们有各自的状态字段，不该再叠一层
        "失败"。这是通行的判断方式。
        """
        return self.exit_code is not None and self.exit_code != 0


def resolve_shell() -> tuple[str, list[str]]:
    """
    解析用哪个 shell。

    ## Windows 上选 PowerShell 而不是 cmd

    本项目的开发环境是 Windows，而 cmd 的问题很实际：没有 `&&`、
    引号规则古怪、UTF-8 要靠 chcp 折腾。PowerShell 至少行为可预测。

    ## 为什么不直接用 shell=True

    `shell=True` 在 POSIX 上会多套一层 `/bin/sh -c`，进程树多一层，
    杀进程时更容易漏。显式指定 shell 可执行文件 + argv 数组，
    进程结构是确定的，所以显式解析。
    """
    configured = settings.sandbox.shell_path
    if configured:
        return configured, _shell_args(configured)

    if sys.platform == "win32":
        for name in ("pwsh.exe", "powershell.exe"):
            found = shutil.which(name)
            if found:
                return found, ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]
        # 退到 cmd。行为差但总比没有好。
        return os.environ.get("COMSPEC", "cmd.exe"), ["/d", "/s", "/c"]

    for candidate in ("/bin/bash", "/usr/bin/bash"):
        if Path(candidate).exists():
            return candidate, ["-c"]
    found = shutil.which("bash") or shutil.which("sh") or "/bin/sh"
    return found, ["-c"]


def _shell_args(shell_path: str) -> list[str]:
    low = shell_path.lower()
    if "powershell" in low or "pwsh" in low:
        return ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]
    if low.endswith("cmd.exe") or low.endswith("cmd"):
        return ["/d", "/s", "/c"]
    return ["-c"]


def _wrap_for_exit_code(command: str, shell: str) -> str:
    """
    PowerShell 下补一句 `exit $LASTEXITCODE`，让子进程的真实退出码传出来。

    ## 为什么必须这么做

    实测：脚本里 `os._exit(42)`，PowerShell `-Command` 报出来的退出码是 **1**。
    它把所有非零退出都归一成 1，除非显式转发 `$LASTEXITCODE`。

        & "python.exe" "s.py"                      → exit=1
        & "python.exe" "s.py"; exit $LASTEXITCODE  → exit=42

    后果不只是"数字不对"：模型看到的退出码全是 1，就无法区分
    "测试失败了 3 个"（pytest 返回 1）和"命令根本没找到"（返回 127）。
    很多工具靠退出码传递语义（git diff --quiet 用 1 表示有差异，
    grep 用 1 表示没匹配到），归一成 1 之后这些信息全丢了。

    bash 不需要这个 —— 它默认就用最后一条命令的退出码。
    """
    low = shell.lower()
    if "powershell" not in low and "pwsh" not in low:
        return command
    # 用换行而不是分号连接：命令里可能有未闭合的 here-string 或注释，
    # 分号会被吞进去。换行在 PowerShell 里是可靠的语句分隔。
    #
    # $LASTEXITCODE 在【没执行过任何外部程序】时是 $null，
    # exit $null 会变成 exit 0 —— 这正是想要的（纯 PowerShell 命令成功）。
    return f"{command}\nexit $LASTEXITCODE"


def build_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """
    子进程的环境变量。

    ## 为什么要剔除敏感变量

    很多实现只剔掉自己的会话变量，API Key 照样透传。

    本项目的 Key 存在加密的 DB 里、不走环境变量，所以这里主要防的是
    另一种情况：用户为了方便在系统里设了 `OPENAI_API_KEY`，然后模型
    执行 `env` 或 `printenv` 把它打印出来 —— 那就直接进了上下文，
    再进日志，再进摘要。

    命中即删，不做精确匹配 —— 宁可多删几个无关变量，也不要漏掉一个 Key。
    """
    env = dict(os.environ)
    for key in list(env):
        upper = key.upper()
        if any(marker in upper for marker in settings.sandbox.env_deny_markers):
            env.pop(key, None)
    # 让子进程的 Python 输出不被缓冲，否则超时杀进程时什么都拿不到
    env["PYTHONUNBUFFERED"] = "1"
    # 强制 UTF-8。Windows 上 Python 默认用 GBK 写 stdout，
    # 中文输出会变成乱码或直接抛 UnicodeEncodeError。
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update(extra)
    return env


def kill_process_tree(pid: int) -> None:
    """
    杀掉进程及其全部子进程。

    ## 为什么必须杀整棵树

    cmd.py:129 是 `process.kill()` —— 只杀直接子进程。它执行的是
    `docker exec`，杀掉客户端后容器内的进程继续跑，这是实际存在的资源泄漏。

    本项目的场景同理：命令是 `npm run build` 时，npm 会 fork 出 node，
    只杀 npm 会留下 node 继续占 CPU 和端口。

    ## 两个平台两套机制

    POSIX：进程组。创建时用 `start_new_session=True` 让子进程成为新进程组
    的组长，然后 `killpg(-pid)` 一次杀干净。

    Windows：没有进程组语义。用 `taskkill /F /T`（/T 表示连带子进程）。
    这是通行做法。
    """
    if sys.platform == "win32":
        try:
            # taskkill 自己也是个进程，用 Popen 不等它 —— 等它反而可能
            # 因为目标进程已死而卡在错误处理上
            import subprocess

            subprocess.Popen(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("taskkill_failed", pid=pid, err=str(e))
        return

    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except ProcessLookupError:
        pass  # 已经死了
    except Exception as e:  # noqa: BLE001
        # 进程组不存在时退回杀单个进程
        log.debug("killpg_failed_fallback", pid=pid, err=str(e))
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class _OutputDrain:
    """
    读子进程输出，边读边落盘，并记录"最后一次收到数据的时刻"。

    ## 为什么必须边读边落盘

    内存里只保留尾部（否则一个 `while true; do echo x; done` 能吃掉几个 GB）。
    但那样"完整输出已保存到某文件"这个承诺就是假的 —— 实测过：4000 行的
    输出，内存只留了 247 行，落盘文件也就只有 247 行。模型按提示去读，
    读到的还是残缺的。

    所以落盘发生在【读的时候】而不是截断的时候：文件拿到全部字节，
    内存只留尾部。两者各自完整。

    ## 那个时间戳是关键

    进程 exit 后不能按固定时限关流。有实现记录过
    issue #5303：短命的主进程可能已退出，而它 fork 出的进程还持着 stdout
    继续写。按固定时限关流会静默丢掉那些输出。

    实测过这个 bug（我第一版就是固定时限）：模型执行
    `powershell -Command "1..3000 | ForEach-Object { Write-Output $_ }"`
    —— 外层 shell 很快退出，内层 powershell 继续写，结果 3000 行只收到 **863 行**。
    而且没有任何报错，输出就是少了。

    正确做法是**等管道空闲**：每收到一个 chunk 就更新 last_data_at，
    调用方据此判断"还在写"还是"真的没了"。
    """

    def __init__(self, limit: int) -> None:
        self.chunks: list[bytes] = []
        self.total = 0
        self.limit = limit
        self.last_data_at = 0.0
        self.eof = False
        # 落盘句柄。只在【真的超了上限】时才创建 ——
        # 绝大多数命令输出很短，为它们建临时文件纯属浪费。
        self._sink_path: Path | None = None
        self._sink = None
        # 被内存上限丢掉的行数。
        #
        # 不单独记的话，"共 N 行"会算成【留下来的行数】而不是真实总数 ——
        # 实测：3000 行的输出因为超字节上限只留了 863 行，
        # 于是提示写着"仅显示第 1~863 行（共 863 行）"，自相矛盾，
        # 模型会以为输出就这么多，不去读完整文件。
        self.dropped_lines = 0
        self.dropped_bytes = 0

    async def run(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            self.eof = True
            return
        loop = asyncio.get_running_loop()
        self.last_data_at = loop.time()
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                self.eof = True
                return
            self.last_data_at = loop.time()
            self.total += len(chunk)
            self.chunks.append(chunk)
            # 保留字节数超过上限两倍时压缩一次，避免长时间运行的命令
            # 把内存吃满（一个 while true 的 echo 能产出几个 GB）。
            # 必须继续读完，否则子进程会因为管道满而阻塞。
            if self.total > self.limit * 2:
                joined = b"".join(self.chunks)
                kept = joined[-self.limit :]
                dropped = joined[: len(joined) - len(kept)]
                # 丢之前先落盘，否则"完整输出"就是假的
                self._spill(dropped)
                # 记下丢掉了多少行，否则"共 N 行"会算成留下来的行数
                self.dropped_lines += dropped.count(b"\n")
                self.dropped_bytes += len(dropped)
                self.chunks.clear()
                self.chunks.append(kept)

    def _spill(self, data: bytes) -> None:
        """把要丢掉的字节写进落盘文件。"""
        if not data:
            return
        if self._sink is None:
            try:
                settings.temp_dir.mkdir(parents=True, exist_ok=True)
                fd, path = tempfile.mkstemp(
                    prefix="jeeves_output_", suffix=".txt", dir=settings.temp_dir
                )
                self._sink = os.fdopen(fd, "wb")
                self._sink_path = Path(path)
            except OSError as e:
                log.warning("spill_open_failed", err=str(e))
                self._sink = False  # type: ignore[assignment]
                return
        if self._sink is False:
            return
        try:
            self._sink.write(data)  # type: ignore[union-attr]
        except OSError as e:
            log.warning("spill_write_failed", err=str(e))

    def finish(self) -> Path | None:
        """
        收尾：把内存里剩的尾部也写进落盘文件，返回文件路径。

        只在真的溢出过时才有文件 —— 没溢出的话完整输出就在内存里，
        不需要落盘。
        """
        if self._sink is None or self._sink is False:
            return None
        try:
            self._sink.write(b"".join(self.chunks))  # type: ignore[union-attr]
            self._sink.close()  # type: ignore[union-attr]
        except OSError as e:
            log.warning("spill_close_failed", err=str(e))
            return None
        return self._sink_path

    def truncated_head(self) -> bool:
        """是否因为超字节上限丢过头部。"""
        return self.dropped_bytes > 0


async def _wait_for_idle(drain: _OutputDrain, task: asyncio.Task[None], grace: float) -> None:
    """
    等到管道空闲（grace 秒内没有新数据）或读到 EOF。

    ## 为什么不能用 wait_for(task, timeout=grace)

    那正是 #5303 描述的错误做法 —— 一个从 exit 时刻起算的固定截止时间。
    还在写的进程会被砍断。

    这里用轮询看 last_data_at：只要还在来数据，计时器就相当于被重置，
    循环继续等；真的静默了 grace 秒才放弃。
    """
    loop = asyncio.get_running_loop()
    poll = min(grace / 4, 0.1)
    while True:
        if task.done():
            return
        idle = loop.time() - drain.last_data_at
        if idle >= grace:
            # 静默够久了。有进程死死握着管道不放（Windows 上 daemon 化的
            # 子进程会这样），不能无限等。
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=poll)
            return
        except TimeoutError:
            continue


# ASYNC109 建议让调用方用 asyncio.timeout 而不是收 timeout 参数。
# 这里不适用：timeout 是【工具暴露给模型的参数】，模型按命令性质自己决定
# （跑测试给 300 秒、看版本号给 5 秒）。而且超时后要杀进程树、要保留已捕获的
# 输出、要在结果里标注 timed_out —— 这些都得在函数内部做，外层的
# asyncio.timeout 只会抛异常，拿不到部分输出。
async def run_process(
    command: str,
    *,
    cwd: Path,
    timeout: int | None = None,  # noqa: ASYNC109
    env_extra: dict[str, str] | None = None,
    max_bytes: int | None = None,
    max_lines: int | None = None,
) -> ExecResult:
    """
    执行一条命令，返回结构化结果。

    永不抛异常（除 CancelledError）—— 命令失败是 agent 的正常工作状态。
    """
    shell, shell_args = resolve_shell()
    command = _wrap_for_exit_code(command, shell)
    limit_bytes = max_bytes or settings.sandbox.max_output_bytes
    limit_lines = max_lines or settings.sandbox.max_output_lines
    wait_timeout = timeout if timeout is not None else settings.sandbox.timeout_default

    loop = asyncio.get_running_loop()
    started = loop.time()

    # POSIX 下 start_new_session 让子进程成为新进程组组长，
    # 这是后面 killpg 能一次杀干净的前提。
    kwargs: dict[str, object] = {}
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    else:
        # Windows 上不弹控制台窗口
        import subprocess

        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        proc = await asyncio.create_subprocess_exec(
            shell,
            *shell_args,
            command,
            cwd=str(cwd),
            # stdin 必须显式关掉 ——
            # 让子进程继承 stdin，于是 `read` / `apt install`（等 y/n）
            # 这类命令会一直阻塞到超时。DEVNULL 让它们立刻拿到 EOF。
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=build_env(env_extra),
            **kwargs,  # type: ignore[arg-type]
        )
    except FileNotFoundError:
        return ExecResult(
            output=f"找不到 shell：{shell}",
            exit_code=None,
            timed_out=False,
            truncated=False,
            total_lines=0,
            shown_lines=0,
            full_output_path=None,
            duration_ms=0,
        )
    except OSError as e:
        return ExecResult(
            output=f"无法启动进程：{e}",
            exit_code=None,
            timed_out=False,
            truncated=False,
            total_lines=0,
            shown_lines=0,
            full_output_path=None,
            duration_ms=0,
        )

    drain_state = _OutputDrain(limit_bytes)
    timed_out = False

    # stderr 已经用 STDOUT 合并了，所以只需读一个流。
    # 合并的理由：模型看到的是命令的完整输出，顺序也是真实的交错顺序。
    # 分开读再拼接（cmd.py:102 的做法）会丢掉时序关系 ——
    # 报错到底发生在哪一步输出之后，模型就看不出来了。
    drain = asyncio.create_task(drain_state.run(proc.stdout))

    try:
        await asyncio.wait_for(proc.wait(), timeout=wait_timeout)
    except TimeoutError:
        timed_out = True
        if proc.pid:
            kill_process_tree(proc.pid)
        # 杀完还要等它真的死掉，否则下面读不到 EOF
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            log.warning("process_survived_kill", pid=proc.pid)
    except asyncio.CancelledError:
        # 取消必须杀进程 —— 否则用户点了停止，命令还在后台跑
        if proc.pid:
            kill_process_tree(proc.pid)
        drain.cancel()
        raise

    # 进程退出后【等管道空闲】而不是按固定时限关流。
    #
    # 第一版我写的是 `wait_for(drain, timeout=grace)`，并且在注释里说它
    # "效果等同于每个 chunk 重置计时器" —— 那是错的，它就是
    # issue #5303 警告的那个固定截止时间。
    #
    # 实测暴露了它：模型执行
    #   powershell -Command "1..3000 | ForEach-Object { Write-Output $_ }"
    # 外层 shell 很快退出，内层 powershell 继续写，3000 行只收到 **863 行**。
    # 没有任何报错，输出就是少了 —— 而模型会基于不完整的输出做判断。
    #
    # _wait_for_idle 看的是 last_data_at：只要还在来数据就继续等，
    # 真的静默了 grace 秒才放弃。
    try:
        await _wait_for_idle(drain_state, drain, settings.sandbox.drain_grace)
    except asyncio.CancelledError:
        drain.cancel()
        raise

    raw = b"".join(drain_state.chunks)
    text = raw.decode("utf-8", errors="replace")
    # 溢出过就有完整文件（边读边落的），没溢出则为 None
    spilled = drain_state.finish()
    duration_ms = int((loop.time() - started) * 1000)

    trimmed, truncated, total_lines, shown_lines, full_path = _truncate_tail(
        text,
        truncated_bytes=drain_state.truncated_head(),
        dropped_lines=drain_state.dropped_lines,
        limit_lines=limit_lines,
        spilled_path=spilled,
    )

    if timed_out:
        trimmed = _append_status(
            trimmed, f"命令超时（{wait_timeout} 秒），已终止进程及其全部子进程"
        )

    return ExecResult(
        output=trimmed,
        exit_code=None if timed_out else proc.returncode,
        timed_out=timed_out,
        truncated=truncated,
        total_lines=total_lines,
        shown_lines=shown_lines,
        full_output_path=full_path,
        duration_ms=duration_ms,
    )


def _append_status(text: str, status: str) -> str:
    """
    把状态附在已捕获的输出后面，而不是替换它。

    有些命令工具完全忽略退出码，
    模型只能从 stderr 文本猜。而超时/失败时最有用的信息恰好是
    "在输出到哪一步的时候失败的"。
    """
    return f"{text}\n\n{status}" if text.strip() else status


def _truncate_tail(
    text: str,
    *,
    truncated_bytes: bool,
    limit_lines: int,
    dropped_lines: int = 0,
    spilled_path: Path | None = None,
) -> tuple[str, bool, int, int, str | None]:
    """
    按行数截断，保留尾部，并把完整输出落盘。

    返回 (截断后文本, 是否截断, 总行数, 保留行数, 完整输出路径)。

    ## 为什么保留尾部

    命令输出的关键信息在末尾：编译错误、测试结果、最终状态。
    保留头部（python_code_runner.py:154 那样）会把 traceback 切掉。

    文件读取相反 —— 那时头部才是关键（import、类定义）。有实现用两个不同的
    函数区分（truncateHead / truncateTail），本项目同理。

    ## 为什么要落盘

    截断后模型知道"有东西被截了"但拿不到。落盘并把路径给它，
    它可以自己决定要不要 read_file 去看。这是有意的分层
    ，另一些实现没有。
    """
    lines = text.splitlines()
    # 真实总行数 = 留在内存里的 + 因为超字节上限被丢掉的。
    # 不加 dropped_lines 的话会写出"仅显示第 1~863 行（共 863 行）"这种
    # 自相矛盾的提示，模型会以为输出就这么多，不去读完整文件。
    total_lines = len(lines) + dropped_lines
    over_lines = len(lines) > limit_lines
    if not over_lines and not truncated_bytes:
        return text, False, total_lines, total_lines, None

    kept = lines[-limit_lines:] if over_lines else lines
    body = "\n".join(kept)

    full_path: str | None = None
    if spilled_path is not None:
        # 已经边读边落好了（内存只留尾部，文件拿到全部字节）。
        # 不能在这里重写文件 —— text 只是尾部，那样"完整输出"又变残缺了。
        full_path = str(spilled_path)
    else:
        try:
            # 就地建目录。不能只依赖启动时创建 —— 测试、脚本、SubAgent 都可能
            # 在没走 startup 的情况下用到这里，那时落盘会静默失败（只留一条
            # warning），模型拿到的路径是 None，"完整输出可取回"就没了。
            settings.temp_dir.mkdir(parents=True, exist_ok=True)
            fd, path = tempfile.mkstemp(
                prefix="jeeves_output_", suffix=".txt", dir=settings.temp_dir
            )
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            full_path = path
        except OSError as e:  # noqa: BLE001
            # 落盘失败不影响主流程，只是模型拿不到完整输出
            log.warning("full_output_write_failed", err=str(e))

    if over_lines or dropped_lines:
        start = total_lines - len(kept) + 1
        note = f"[输出过长，仅显示第 {start}~{total_lines} 行（共 {total_lines} 行）"
    else:
        note = "[输出过长，已截断头部"
    if full_path:
        note += f"。完整输出：{full_path}]"
    else:
        note += "]"

    return f"{note}\n{body}", True, total_lines, len(kept), full_path


class LocalSandbox:
    """
    宿主直接执行。默认后端。

    ## 为什么是个类而不是直接用 run_process

    要和 DockerSandbox 可替换（同一个 SandboxPort 协议）。
    没有这层的话，`exec.py` 里就得写 `if backend == "docker"` 分支 ——
    每加一个执行类工具都要改一遍。

    这个类本身几乎没有状态，方法基本是转发。
    """

    name = "local"
    # 【不是隔离环境】。
    #
    # PathGuard 限制了文件访问范围、白名单限制了可执行的命令，
    # 但命令一旦跑起来就是宿主进程 —— 能访问网络、能读环境变量、
    # 资源不受限。前端据此显示提示。
    isolated = False

    async def health(self) -> tuple[bool, str]:
        """本地执行总是可用 —— 没有外部依赖。"""
        return True, ""

    async def run(
        self,
        command: str,
        *,
        cwd: Path,
        session_id: str = "",
        timeout: int | None = None,  # noqa: ASYNC109
        env_extra: dict[str, str] | None = None,
        ws_root: Path | None = None,
    ) -> ExecResult:
        # session_id / ws_root 都用不上：本地执行没有 per-session 的
        # 资源要管，也没有挂载点的概念（cwd 就是真实路径）
        return await run_process(
            command, cwd=cwd, timeout=timeout, env_extra=env_extra
        )

    async def cleanup_session(self, session_id: str) -> None:
        """本地执行没有 per-session 资源。"""

    async def shutdown(self) -> None:
        """子进程在 run_process 里就已经等到结束或被杀，没有遗留。"""
