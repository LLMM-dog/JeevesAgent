"""
执行类工具：run_shell。

## 为什么只有 run_shell、没有 run_python

run_shell 本身就能执行任意命令 —— `python script.py`、`java Main`、
`gcc a.c && ./a.out` 都在其能力范围内。单独的 run_python 工具是冗余的：
它只是"写临时脚本 + python 执行"的薄封装，而模型完全可以用
write_file 写脚本再用 run_shell 执行（这条路径同样覆盖 java / c 等其它语言，
一个工具比两个更简单、更不易选错）。

## 为什么 run_shell 标 requires_approval

它能做的事没有上界。文件工具受路径白名单约束，而一条 shell 命令可以
`curl | sh`、可以删库、可以把 SSH key 传出去。白名单在这里帮不上忙 ——
命令字符串的正则匹配不是安全边界（那是"防手滑"而非
安全机制的例子，它自己都把 curl 放在白名单里）。

真正的边界只有两个：人工确认，或者容器/OS 级隔离。本项目默认前者
（approval_mode="manual"），并在文档里明确说清这一点，不假装安全。
"""

from __future__ import annotations

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


async def _workspace_sandbox_cfg(ctx: ToolContext) -> dict[str, str]:
    """查当前工作区的执行环境配置。

    默认 local。工作区在设置里选了 Docker 时，返回容器名 / 镜像 / 网络，
    执行时据此创建/复用该工作区的容器。
    """
    cfg: dict[str, str] = {"backend": "local", "container": "", "image": "", "network": ""}
    if ctx.db is None:
        return cfg
    from sqlalchemy import select

    from app.modules.session.models import Workspace

    row = (
        await ctx.db.execute(
            select(Workspace).where(Workspace.root_path == str(ctx.workspace))
        )
    ).scalar_one_or_none()
    if row is None or row.sandbox_backend != "docker":
        return cfg
    cfg["backend"] = "docker"
    cfg["container"] = row.docker_container or ""
    cfg["image"] = row.docker_image or ""
    cfg["network"] = row.docker_network or ""
    return cfg


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
    description = "执行 shell 命令。用 ; 连接多条，用 cwd 指定工作目录。命令必须非交互（不能读 stdin，加 -y/--yes）。"
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
        # 工作区级执行环境：每个工作区可独立选本机 / Docker 容器。
        ws_cfg = await _workspace_sandbox_cfg(ctx)
        sandbox = await get_sandbox(ws_cfg)
        result = await sandbox.run(
            command,
            cwd=cwd,
            # Docker 后端用工作区容器名作隔离单元（同工作区多会话共享容器）
            session_id=ws_cfg.get("container") or ctx.session_id,
            timeout=timeout,
            image=ws_cfg.get("image", ""),
            network=ws_cfg.get("network", ""),
            # 传【真实的工作区根】而不是让沙箱读全局配置。
            #
            # settings.workspace_dir 是硬编码的 PROJECT_ROOT/workspace，
            # 而 ctx.workspace 是从 workspace.root_path 一路传下来的。
            # 用户建了多个工作区时两者不同，读全局配置会挂错目录且无报错。
            ws_root=ctx.workspace,
        )
        return _format(result, what="命令")
