"""
执行类工具：run_shell / run_python。

## 为什么这两个都标 requires_approval

它们能做的事没有上界。文件工具受路径白名单约束，而一条 shell 命令可以
`curl | sh`、可以删库、可以把 SSH key 传出去。白名单在这里帮不上忙 ——
命令字符串的正则匹配不是安全边界（那是"防手滑"而非
安全机制的例子，它自己都把 `curl` 放在白名单里）。

真正的边界只有两个：人工确认，或者容器/OS 级隔离。本项目默认前者
（`approval_mode="manual"`），并在文档里明确说清这一点，不假装安全。

## run_python 是个反面教材

它的工具描述写着"在隔离环境中执行 Python 代码"，实现是同进程 `exec()`，
而 `get_safe_builtins` 的 docstring 自己承认：

    保留全部内置函数（包括 __import__、eval 等），
    仅将 open() 替换为经过 check_path_whitelisted() 审查的包装版本。

保留 `__import__` 意味着 `import os; os.system(...)` 完全可用 ——
所谓的路径白名单只 hook 了 `open()`，一行 import 就绕过去了。

所以本项目的 run_python **不在同进程 exec**，而是写临时文件 + 起子进程。
这样超时能杀、输出能截断、崩溃不影响主进程。代价是启动慢一点（几十毫秒），
换来的是行为可预测。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import structlog

from app.core.config import settings
from app.core.exceptions import PathDeniedError
from app.infra.sandbox.factory import get_sandbox
from app.infra.sandbox.local import ExecResult
from app.modules.agent.pathguard import get_guard
from app.modules.agent.tools.base import ToolContext, ToolResult

log = structlog.get_logger(__name__)


def _clamp_timeout(value: Any) -> int:
    """
    把模型给的 timeout 夹到合理区间。

    模型会给出各种值：字符串 "60"、0、-1、999999。不夹的话
    `asyncio.wait_for(timeout=0)` 会立刻超时，而 999999 等于没有超时。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return settings.sandbox.timeout_default
    if n <= 0:
        return settings.sandbox.timeout_default
    return min(n, settings.sandbox.timeout_max)


def _resolve_cwd(ctx: ToolContext, cwd: str | None) -> Path:
    """
    确定工作目录，并确保它在白名单内。

    ## 为什么执行目录也要过白名单

    直觉上"命令能干任何事，限制 cwd 有什么意义"。但意义在于**默认行为**：
    模型说 `rm -rf *` 时，cwd 决定了它删的是工作区还是用户主目录。
    限制 cwd 不能阻止刻意的破坏，但能挡住绝大多数"路径写错了"的意外。

    靠容器的 `-w /workspace`，有的实现完全不限制（cwd 校验只检查存在性）。
    本项目走白名单，与文件工具同一套机制。
    """
    if not cwd:
        return ctx.workspace
    guard = get_guard()
    # 相对路径按工作区解析 —— 与文件工具一致，模型不用手抄绝对路径
    candidate = Path(cwd)
    if not candidate.is_absolute():
        candidate = ctx.workspace / candidate
    resolved = guard.check(candidate, write=False)
    if not resolved.is_dir():
        raise NotADirectoryError(f"{cwd} 不是目录")
    return resolved


def _format(result: ExecResult, *, what: str) -> ToolResult:
    """
    把执行结果转成给模型和前端的两份表示。

    失败时【输出和状态一起给】，不是只给一句错误。
    。命令工具完全忽略退出码，模型只能从 stderr 猜。
    """
    body = result.output or "（无输出）"
    if result.timed_out:
        text = body  # run_process 已经把超时说明附在末尾
    elif result.failed:
        text = f"{body}\n\n{what}以退出码 {result.exit_code} 结束"
    else:
        text = body

    return ToolResult(
        content=text,
        display={
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "truncated": result.truncated,
            "total_lines": result.total_lines,
            "shown_lines": result.shown_lines,
            "full_output_path": result.full_output_path,
            "duration_ms": result.duration_ms,
        },
        # 超时和非零退出都算错误态 —— 前端把卡片标红，模型看到 is_error
        # 会知道要处理。这是 ToolResult 的双消费者设计。
        is_error=result.timed_out or result.failed,
    )


class RunShellTool:
    name = "run_shell"
    description = (
        "在工作区执行一条 shell 命令（Windows 上是 PowerShell，"
        "Linux/macOS 上是 bash），返回合并后的 stdout 与 stderr。\n"
        "\n"
        "注意：\n"
        "- 输出过长会只保留末尾若干行，完整输出会写到临时文件并在结果里给出路径，"
        "需要时可以用 read_file 读它\n"
        "- 命令不能读取标准输入，交互式命令（等 y/n 确认的）会直接拿到 EOF，"
        "请改用非交互参数，例如 -y / --yes / --non-interactive\n"
        "- 超时会终止该命令及其全部子进程\n"
        "- 每次调用都是独立的进程，`cd` 不会影响下一次调用；"
        "需要切目录请用 cwd 参数，或者在一条命令里用 `;` 连接"
    )
    # 一条命令能做的事没有上界，路径白名单在这里帮不上忙。
    requires_approval = True

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的命令。可以用 ; 连接多条",
                },
                "cwd": {
                    "type": "string",
                    "description": (
                        "工作目录，相对于工作区根目录。省略则用工作区根目录"
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        f"超时秒数，默认 {settings.sandbox.timeout_default}，"
                        f"上限 {settings.sandbox.timeout_max}。"
                        "跑测试或构建这类耗时操作请显式给大一些"
                    ),
                },
            },
            "required": ["command"],
        }

    async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
        command = str(kw.get("command") or "").strip()
        if not command:
            return ToolResult(content="command 不能为空", is_error=True)

        try:
            cwd = _resolve_cwd(ctx, kw.get("cwd"))
        except NotADirectoryError as e:
            return ToolResult(content=str(e), is_error=True)
        except PathDeniedError as e:
            # registry.execute 也会兜住这个异常，但在这里处理能给出更具体的
            # 提示（是 cwd 被拒，不是命令里的某个路径），模型据此改 cwd 就行。
            #
            # hint 一起给 —— 里面有白名单清单。不给的话模型只能靠猜，
            # 实测会连着换好几种写法全部被拒。
            return ToolResult(
                content=(
                    f"工作目录被拒绝：{e.message}。请改用工作区内的相对路径。"
                    f"{getattr(e, 'hint', '')}"
                ),
                is_error=True,
            )

        timeout = _clamp_timeout(kw.get("timeout"))
        log.info(
            "run_shell",
            run_id=ctx.run_id,
            cwd=str(cwd),
            timeout=timeout,
            command=command[:200],
        )
        sandbox = await get_sandbox()
        result = await sandbox.run(
            command,
            cwd=cwd,
            session_id=ctx.session_id,
            timeout=timeout,
            # 传【真实的工作区根】而不是让沙箱读全局配置。
            #
            # settings.workspace_dir 是硬编码的 PROJECT_ROOT/workspace，
            # 而 ctx.workspace 是从 workspace.root_path 一路传下来的。
            # 用户建了多个工作区时两者不同，读全局配置会挂错目录且无报错。
            ws_root=ctx.workspace,
        )
        return _format(result, what="命令")


class RunPythonTool:
    name = "run_python"
    description = (
        "执行一段 Python 代码。代码会被写成临时文件后用子进程运行，"
        "返回合并后的 stdout 与 stderr。\n"
        "\n"
        "注意：\n"
        "- 每次调用是全新的进程，变量与导入不跨调用保留\n"
        "- 不能读取标准输入（input() 会拿到 EOF）\n"
        "- 需要保留的代码请用 write_file 写成正式文件，"
        "这个工具适合一次性的计算、验证、探查"
    )
    requires_approval = True

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "要执行的 Python 代码"},
                "cwd": {
                    "type": "string",
                    "description": "工作目录，相对于工作区根目录",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"超时秒数，默认 {settings.sandbox.timeout_default}",
                },
            },
            "required": ["code"],
        }

    async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
        code = str(kw.get("code") or "")
        if not code.strip():
            return ToolResult(content="code 不能为空", is_error=True)

        try:
            cwd = _resolve_cwd(ctx, kw.get("cwd"))
        except NotADirectoryError as e:
            return ToolResult(content=str(e), is_error=True)
        except PathDeniedError as e:
            return ToolResult(
                content=f"工作目录被拒绝：{e.message}。请改用工作区内的相对路径",
                is_error=True,
            )

        timeout = _clamp_timeout(kw.get("timeout"))

        # 写临时脚本再执行，而不是 `python -c`。
        #
        # 三个理由：
        # 1. `-c` 传长代码时会撞命令行长度上限（Windows 约 8191 字符）
        # 2. traceback 里能看到真实行号，`-c` 显示的是 "<string>"
        # 3. 代码里的引号不用转义 —— `-c` 要处理嵌套引号，很容易出错
        #
        # 脚本放哪取决于后端。
        #
        # 本地执行：放 temp_dir（data/tmp），不污染用户的项目目录。
        #
        # Docker：【必须放工作区内】—— 容器只挂载了工作区，
        # data/tmp 在容器里根本不存在。放外面的话报
        # "can't open file '/workspace/snippet_x.py'"，
        # 而真因是"这个文件在宿主上，容器看不到"。
        #
        # 工作区内用 .jeeves/ 子目录（已在 PathGuard 的忽略列表里，
        # 不会被 glob/grep 搜到，也不会让 agent 在自己的临时脚本里
        # 搜到自己的代码）。
        sandbox = await get_sandbox()
        if sandbox.name == "docker":
            script_dir = cwd / ".jeeves"
        else:
            script_dir = settings.temp_dir
        try:
            script_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return ToolResult(content=f"无法创建临时目录：{e}", is_error=True)
        script = script_dir / f"snippet_{uuid.uuid4().hex}.py"
        try:
            script.write_text(code, encoding="utf-8", newline="")
        except OSError as e:
            return ToolResult(content=f"无法写入临时脚本：{e}", is_error=True)

        log.info(
            "run_python",
            run_id=ctx.run_id,
            cwd=str(cwd),
            timeout=timeout,
            code_chars=len(code),
        )
        try:
            if sandbox.name == "docker":
                # 容器里不能用 sys.executable。
                #
                # 那是【宿主】的解释器路径（比如
                # D:\...\.venv\Scripts\python.exe），容器里根本不存在 ——
                # 报的是 "no such file or directory"，而错误信息里出现一个
                # Windows 路径会让人完全看不懂发生了什么。
                #
                # 容器里用镜像自带的 python3（默认镜像是 python:3.12-slim）。
                # 脚本写在 cwd/.jeeves/ 下，而 docker exec 的工作目录就是
                # cwd 对应的容器路径 —— 所以用相对路径 .jeeves/xxx.py 即可。
                command = f'python3 ".jeeves/{script.name}"'
            else:
                # 用 sys.executable 而不是 "python"：虚拟环境里 PATH 上的
                # python 可能是系统解释器，那样 import 项目依赖会失败。
                import sys

                # PowerShell 里带引号的可执行文件路径必须用调用运算符 `&`，
                # 否则它把引号内容当字符串字面量，报 "Unexpected token"。
                # 实测：`"C:\...\python.exe" "script.py"` 直接 exit=1。
                # bash 没有这个问题，但加了 & 也不影响 —— 所以不分平台处理。
                prefix = "& " if sys.platform == "win32" else ""
                command = f'{prefix}"{sys.executable}" "{script}"'

            result = await sandbox.run(
                command,
                cwd=cwd,
                session_id=ctx.session_id,
                timeout=timeout,
                ws_root=ctx.workspace,
            )
            return _format(result, what="脚本")
        finally:
            # 无论成功失败都清理。python_code_runner.py:214-220
            # 同样放在 finally 里 —— 不清理的话临时目录会无限增长。
            script.unlink(missing_ok=True)
