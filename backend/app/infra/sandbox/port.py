"""
沙箱端口定义。

## 为什么现在才抽这个接口

之前只有一个 `LocalSandbox`，`exec.py` 直接 `from ...local import run_process`。
只有一个实现时抽接口是过度设计。

现在要加 Docker 后端，两个实现必须可替换 —— 而替换点不能是
`if backend == "docker"` 散落在工具里，那样每加一个执行类工具都要
改一遍分支。

## 为什么返回同一个 ExecResult

`ExecResult` 里的字段（截断标记、总行数、完整输出路径、耗时）与
"在哪执行"无关，是**执行结果的固有属性**。

两个后端各自定义结果类型的话，`exec.py` 就得写两套解读逻辑 ——
而那些逻辑（怎么把截断信息告诉模型）本来就该只有一份。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.infra.sandbox.local import ExecResult


class SandboxPort(Protocol):
    """
    执行后端。

    实现要求：
    - `run` 永不抛异常（除 CancelledError）—— 命令失败是 agent 的正常
      工作状态，抛异常会让整轮对话中断
    - `health` 要能在几百毫秒内返回，它在每次选择后端时被调用
    """

    name: str
    """后端标识，进日志和 /api/meta。用户需要知道当前是不是隔离环境。"""

    isolated: bool
    """是否真隔离。前端据此决定要不要显示"非隔离环境"提示条。"""

    async def health(self) -> tuple[bool, str]:
        """
        探活。返回 (可用, 不可用原因)。

        原因必须可执行 —— "Docker 不可用"没用，
        "Docker 守护进程没响应，检查 Docker Desktop 是否启动"才有用。
        """
        ...

    async def run(
        self,
        command: str,
        *,
        cwd: Path,
        session_id: str,
        timeout: int | None = None,  # noqa: ASYNC109
        env_extra: dict[str, str] | None = None,
        # 该会话真实的工作区根（来自 workspace.root_path）。
        #
        # Docker 后端用它决定挂载点。不传的话回落到全局配置的
        # workspace_dir —— 而那是硬编码的 PROJECT_ROOT/workspace，
        # 用户建了多个工作区时会挂错目录（且静默，无报错）。
        ws_root: Path | None = None,
        # 工作区级的容器配置（Docker 后端用，非空覆盖全局 settings）。
        image: str = "",
        network: str = "",
    ) -> ExecResult: ...

    async def cleanup_session(self, session_id: str) -> None:
        """
        会话结束时清理该会话的资源。

        本地后端是空操作。Docker 后端要删容器 ——
        不删的话每个会话留一个容器，跑一天宿主上几十个容器在占内存。
        """
        ...

    async def shutdown(self) -> None:
        """进程退出时清理全部资源。"""
        ...
