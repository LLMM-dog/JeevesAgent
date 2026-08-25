"""
Docker 沙箱。

## 与 实现的差异

Docker 沙箱不常见（参考实现里那个 368 行的
`AgentSandboxManager`），容器复用、TTL、并发锁都做对了。这里照抄。

但它的 `docker run` 参数有四个问题：

| | 这里 | 为什么 |
| --- | --- | --- |
| `--network host` | `--network none` | host 模式下容器能打宿主 localhost、内网、云元数据端点 —— 文件系统隔离做了但网络完全没挡 |
| 无资源限制 | `--memory` / `--cpus` / `--pids-limit` | 一个死循环或 fork 炸弹能把宿主拖死 |
| 保留全部 capability | `--cap-drop ALL` + `no-new-privileges` | 没有理由留着 CAP_SETUID 之类 |
| 只在正常退出时清理 | 启动时清理遗留 | `--rm` 只在容器自己停止时生效，而保活命令不会自己停；kill -9 后容器一直跑 |

## 为什么用 docker CLI 而不是 docker SDK

`docker` python SDK 是同步的，每个调用都要 `to_thread`。而它提供的
抽象（Container 对象、流式 attach）这里几乎用不上 —— 需要的就是
`run` / `exec` / `rm` 三条命令。

CLI 的另一个好处是错误信息可以直接给用户看："docker: command not found"
比 SDK 的 `DockerException` 更容易懂。

代价是要自己拼参数，且参数拼错只能在运行时发现。所以有专门的测试
断言关键安全参数都在。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import time
from pathlib import Path

import structlog
from app.core.config import settings
from app.infra.sandbox.local import ExecResult, _truncate_tail

log = structlog.get_logger(__name__)

# 容器名前缀。
#
# 【必须固定】—— 启动时靠它找出上次遗留的容器。
# 用 agent_sandbox_<uuid4()>，前缀固定但它没利用这一点做启动清理。
NAME_PREFIX = "jeeves-"

# 容器空闲多久后回收（秒）。
#
# 不能太短：pip install 装的包、cd 进的目录都在容器里，
# 容器一删这些状态就没了。
# 也不能太长：每个容器占几十 MB 内存。
IDLE_TTL = 30 * 60

# 容器内的工作区挂载点。
CONTAINER_WORKSPACE = "/workspace"

# docker 命令自身的超时。
#
# 与被执行命令的超时是两件事：这个是"docker 客户端连不上守护进程"
# 的保护。守护进程挂了的话 docker CLI 会一直等。
DOCKER_CLI_TIMEOUT = 30.0

# 保活命令。
#
# 容器要长期存在（复用），所以主进程不能退出。
# `tail -f /dev/null` 是最省资源的做法（也用这个）。
KEEPALIVE = ["tail", "-f", "/dev/null"]


def to_docker_path(p: Path) -> str:
    """
    宿主路径 → Docker `-v` 能接受的形式。

    ## 为什么要转

    Docker 的 `-v` 语法只认正斜杠。Windows 上 `D:\\a\\b` 直接传会被
    解析成奇怪的东西 —— 而错误信息通常是 "invalid mode" 之类，
    完全不指向"你的路径分隔符不对"。

    Docker Desktop 接受 `D:/a/b` 这种形式（保留盘符加冒号）。
    老的 toolbox 需要 `/d/a/b`，但那个已经废弃多年。
    """
    s = str(p.resolve()).replace("\\", "/")
    return s


class DockerSandbox:
    """
    每个会话一个长期容器。

    ## 为什么复用容器而不是每次新建

    每次新建的话 `pip install` 装的包下次就没了，`cd` 进的目录也丢了。
    而一次容器启动 0.5~2 秒 —— 每个工具调用都付这个代价太贵。

    同样的选择（key 是 hash(client_id + work_dir)）。这里用
    session_id 作 key，因为本项目一个会话对应一个工作区。
    """

    name = "docker"
    isolated = True

    def __init__(self) -> None:
        # session_id → {"cid": str, "expire_at": float}
        self._containers: dict[str, dict] = {}
        # per-session 锁。
        #
        # 【不能只用一把全局锁】：容器创建要 0.5~2 秒，全局锁会让所有
        # 会话的执行请求串行 —— 会话 A 建容器时会话 B 的 ls 也得等。
        #
        # 【也不能不加锁】：同一会话并发两次执行（模型一轮发两个
        # tool_call）会创建两个容器，其中一个泄漏。
        self._locks: dict[str, asyncio.Lock] = {}
        # 保护 _locks 本身 —— 两个协程同时为同一 key 创建锁的话，
        # 各自拿到不同的锁对象，等于没锁
        self._meta_lock = asyncio.Lock()
        self._startup_cleaned = False

    # ─────────────────────────── 探活 ───────────────────────────

    async def health(self) -> tuple[bool, str]:
        """
        三层检查：CLI 存在 → 守护进程响应 → 镜像存在。

        分三层是因为三种失败的处理动作完全不同：装 Docker、启动 Docker、
        拉镜像。只说"Docker 不可用"的话用户不知道该做哪个。
        """
        if shutil.which("docker") is None:
            return False, "找不到 docker 命令。安装 Docker Desktop 后重启终端"

        code, out = await self._docker(["version", "--format", "{{.Server.Version}}"])
        if code != 0:
            return False, (
                "Docker 守护进程没响应。检查 Docker Desktop 是否已启动"
                f"（docker version 退出码 {code}）"
            )

        image = settings.sandbox.docker_image
        code, _ = await self._docker(["image", "inspect", image])
        if code != 0:
            # 【不自动 pull】。
            #
            # 拉镜像要几分钟且需要网络，而这里是在"用户刚发了一条消息、
            # 正等着回复"的路径上 —— 卡几分钟且没有进度提示，
            # 用户会以为卡死了。
            return False, (
                f"镜像 {image} 不在本地。先手动拉取：docker pull {image}"
                "（拉取要几分钟，所以不自动做）"
            )

        return True, ""

    # ─────────────────────────── 执行 ───────────────────────────

    async def run(
        self,
        command: str,
        *,
        cwd: Path,
        session_id: str,
        timeout: int | None = None,  # noqa: ASYNC109
        env_extra: dict[str, str] | None = None,
        ws_root: Path | None = None,
        image: str = "",
        network: str = "",
    ) -> ExecResult:
        """
        在会话的容器里执行命令。永不抛异常。

        ws_root 是【该会话真实的工作区根】（来自 workspace.root_path）。
        不传的话回落到全局配置，多工作区场景下会挂错目录。
        """
        started = time.monotonic()
        wait_timeout = timeout if timeout is not None else settings.sandbox.timeout_default

        try:
            cid = await self._ensure_container(session_id, cwd, ws_root, image=image, network=network)
        except Exception as e:  # noqa: BLE001
            log.warning("docker_container_failed", err=str(e)[:300], session=session_id)
            return ExecResult(
                output=f"无法准备容器：{str(e)[:400]}",
                exit_code=None,
                timed_out=False,
                truncated=False,
                total_lines=1,
                shown_lines=1,
                full_output_path=None,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        # 容器内的相对路径。
        #
        # cwd 是宿主路径（工作区下的某个子目录），要换算成容器里的位置。
        rel = self._rel_in_container(cwd, ws_root)
        args = ["exec", "-w", rel]
        for k, v in (env_extra or {}).items():
            args += ["-e", f"{k}={v}"]
        # sh -c 而不是直接传命令：命令里可能有管道、重定向、&&
        args += [cid, "sh", "-c", command]
        old_cid = cid

        code, out, timed_out = await self._docker_stream(args, wait_timeout)

        # 容器被打坏时重建一次再重试。
        #
        # ## 真实发现的问题
        #
        # 实测：容器里跑一个 fork 炸弹之后，pids-limit 确实挡住了它
        # （fork 返回 OSError），但**容器的 PID 表被占满且不恢复** ——
        # 此后每一次 docker exec 都失败：
        #
        #   OCI runtime exec failed: unable to start container process:
        #   procReady not received      → 退出码 128
        #
        # `docker top` 只显示 2 个进程，容器状态是 running，
        # 但就是没法再 exec。
        #
        # 不处理的话后果很严重：模型跑一次 fork 炸弹（甚至只是
        # 一个失控的多进程脚本），这个会话【后续所有命令】都返回
        # 这条看不懂的 OCI 错误，而它完全不指向真因。
        #
        # 而容器里的状态（pip 装的包）本来就已经不可用了，
        # 重建的代价可以接受。
        if not timed_out and _is_container_broken(code, out):
            log.warning(
                "docker_container_broken_recreate",
                session=session_id,
                cid=cid[:12],
                detail=out[:200],
            )
            await self.cleanup_session(session_id)
            try:
                cid = await self._ensure_container(session_id, cwd, ws_root, image=image, network=network)
            except Exception as e:  # noqa: BLE001
                return ExecResult(
                    output=(
                        "容器已损坏且无法重建（很可能是刚才的命令耗尽了进程数）："
                        f"{str(e)[:300]}"
                    ),
                    exit_code=None,
                    timed_out=False,
                    truncated=False,
                    total_lines=1,
                    shown_lines=1,
                    full_output_path=None,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            # 换掉参数里的容器 id。
            #
            # 用 index 查找而不是写死 args[-4]：后者依赖"命令部分正好是
            # 最后三个元素"这个巧合，将来给 exec 加一个选项就会错位，
            # 而错位的表现是"把容器 id 替换到了别的位置"——
            # docker 会报一个完全无关的参数错误。
            args[args.index(old_cid)] = cid
            code, out, timed_out = await self._docker_stream(args, wait_timeout)
            # 重试后仍然坏就如实告诉模型，别让它以为是自己的命令有问题
            if _is_container_broken(code, out):
                out = (
                    "容器无法执行命令（重建后仍然失败）。"
                    "上一条命令可能耗尽了容器的进程数，"
                    f"请换一种实现方式。原始错误：{out[:200]}"
                )

        # 字节上限先切，再按行数切 —— 与 LocalSandbox 同样的双限策略。
        #
        # 顺序不能反：先按行切的话，一行几 MB 的输出（minified JS、
        # base64）行数不超限但字节数爆了。
        limit_bytes = settings.sandbox.max_output_bytes
        raw = out.encode("utf-8", errors="replace")
        truncated_bytes = len(raw) > limit_bytes
        if truncated_bytes:
            # 从尾部保留 —— 错误信息通常在最后
            raw = raw[-limit_bytes:]
            # 可能切出半个多字节字符，errors="replace" 会处理
            out = raw.decode("utf-8", errors="replace")

        text, truncated, total_lines, shown, spilled = _truncate_tail(
            out,
            truncated_bytes=truncated_bytes,
            limit_lines=settings.sandbox.max_output_lines,
        )

        if timed_out:
            text = (
                f"{text}\n\n[命令超时（{wait_timeout}s）已终止。"
                "如果是长时间任务，改成后台运行或增大 timeout]"
            )

        return ExecResult(
            output=text,
            exit_code=None if timed_out else code,
            timed_out=timed_out,
            truncated=truncated,
            total_lines=total_lines,
            shown_lines=shown,
            full_output_path=str(spilled) if spilled else None,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # ─────────────────────────── 容器管理 ───────────────────────────

    async def _get_lock(self, session_id: str) -> asyncio.Lock:
        async with self._meta_lock:
            lk = self._locks.get(session_id)
            if lk is None:
                lk = asyncio.Lock()
                self._locks[session_id] = lk
            return lk

    async def _ensure_container(
        self,
        session_id: str,
        cwd: Path,
        ws_root: Path | None = None,
        *,
        image: str = "",
        network: str = "",
    ) -> str:
        """
        拿到可用的容器 id，不存在就创建。
        """
        if not self._startup_cleaned:
            # 第一次执行时清理遗留容器。
            #
            # 放在这里而不是 lifespan：lifespan 里做的话，配了 docker
            # 但从不执行命令的用户也要付这个开销（几百毫秒的 docker ps）。
            await self.cleanup_orphans()
            self._startup_cleaned = True

        lock = await self._get_lock(session_id)
        async with lock:
            entry = self._containers.get(session_id)
            if entry:
                cid = entry["cid"]
                # 【必须检查容器真的活着】。
                #
                # 缓存里有记录不代表容器还在 —— 用户可能手动 docker rm、
                # Docker 守护进程可能重启过。不检查的话后续 exec 报
                # "No such container"，而错误不指向"缓存过期了"。
                if await self._alive(cid):
                    entry["expire_at"] = time.time() + IDLE_TTL
                    return cid
                log.info("docker_container_gone_recreate", session=session_id, cid=cid[:12])
                self._containers.pop(session_id, None)

            cid = await self._create(session_id, cwd, ws_root, image=image, network=network)
            self._containers[session_id] = {
                "cid": cid,
                "expire_at": time.time() + IDLE_TTL,
                "workspace": cwd,
            }
            return cid

    async def _create(
        self,
        session_id: str,
        cwd: Path,
        ws_root: Path | None = None,
        *,
        image: str = "",
        network: str = "",
    ) -> str:
        """
        创建容器。

        ## 挂载：只挂工作区

        绝不挂项目目录 —— 否则容器里能改 agent 自己的代码、读 .env
        （里面有 ENCRYPTION_KEY 和所有 API Key）。那等于沙箱形同虚设，
        逃逸不需要技巧，直接读文件就行。
        """
        ws = self._workspace_root(cwd, ws_root)
        cfg = settings.sandbox
        # 工作区级配置优先（非空覆盖全局 settings）
        image = image or cfg.docker_image
        network = network or cfg.docker_network
        name = f"{NAME_PREFIX}{session_id}"

        # 同名容器可能还在（上次没清干净）。先删掉，
        # 否则 docker run 报 "name already in use"
        await self._docker(["rm", "-f", name])

        args = [
            "run",
            "-d",
            # --rm 让容器停止时自动删除。
            # 注意它【不能替代主动清理】：保活命令 tail -f 不会自己停，
            # 所以正常情况下这个标志永远不触发。它只在容器被 OOM kill
            # 之类的情况下起作用。
            "--rm",
            "--name",
            name,
            # ── 网络隔离 ──
            #
            # 默认 none。用 --network host，
            # 那意味着容器直接用宿主的网络命名空间：能 curl 宿主的
            # localhost 服务（包括 agent 自己的 API）、能访问内网、
            # 能打 169.254.169.254 拿云凭证。
            #
            # 文件系统隔离做了但网络没做，等于沙箱只有一半 ——
            # 而在云主机上网络恰恰是最危险的那一面。
            "--network",
            network,
            # ── 资源限制 ──
            #
            # 完全没有这些。后果是容器里一个 while True 或 fork 炸弹
            # 能把宿主拖死 —— 而"限制资源"本来就是沙箱的目的之一。
            "--memory",
            cfg.docker_memory,
            "--cpus",
            str(cfg.docker_cpus),
            # pids-limit 专门防 fork 炸弹。
            # --memory 挡不住它：fork 炸弹每个进程都很小，
            # 靠数量把系统压垮而不是靠内存。
            "--pids-limit",
            str(cfg.docker_pids_limit),
            # ── 权限收紧 ──
            #
            # 去掉所有 capability。默认容器保留 CAP_CHOWN / CAP_SETUID
            # 等一堆，没有任何理由留着。
            #
            # 不加 --user：镜像里 pip install 要写 site-packages，
            # 改 uid 会让常见操作失败。容器 root + cap-drop ALL
            # 已经把大部分提权路径堵掉了。
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            # ── 挂载 ──
            "-v",
            f"{to_docker_path(ws)}:{CONTAINER_WORKSPACE}",
            "-w",
            CONTAINER_WORKSPACE,
            image,
            *KEEPALIVE,
        ]

        code, out = await self._docker(args)
        if code != 0:
            raise RuntimeError(f"docker run 失败（退出码 {code}）：{out[:300]}")
        cid = out.strip().splitlines()[-1].strip() if out.strip() else ""
        if not cid:
            raise RuntimeError("docker run 没返回容器 id")

        log.info(
            "docker_container_created",
            session=session_id,
            cid=cid[:12],
            image=image,
            network=network,
        )
        return cid

    def _workspace_root(self, cwd: Path, ws_root: Path | None = None) -> Path:
        """
        找到工作区根。

        cwd 可能是工作区下的子目录（模型 cd 进去了），
        但挂载点必须是工作区根 —— 只挂子目录的话模型访问不到同级文件。

        ## 为什么必须由调用方传入 ws_root

        原来是读 `settings.workspace_dir`，而那是【硬编码属性】
        （`PROJECT_ROOT / "workspace"`，不可配置）。

        但真实的工作区路径来自数据库：`workspace.root_path`。
        用户建多个工作区时，任务/会话可能绑在一个完全不同的目录上。

        读全局配置的后果：`cwd.relative_to(root)` 抛 ValueError，
        落到"挂 cwd 自己"的回退分支。而挂载点在容器创建时就固化了，
        之后同一会话的所有命令都复用它 —— 如果首次执行时模型正好
        cd 到了某个子目录，整个会话的 /workspace 就指向那个子目录，
        模型看不到同级文件，且【没有任何报错】。
        """
        cwd = cwd.resolve()
        root = (ws_root or Path(settings.workspace_dir)).resolve()
        try:
            cwd.relative_to(root)
            return root
        except ValueError:
            # cwd 不在给定的工作区里。
            #
            # 多工作区场景下这可能是正常的（调用方没传 ws_root 时），
            # 保守起见挂 cwd 自己而不是挂一个更大的范围
            return cwd

    def _rel_in_container(self, cwd: Path, ws_root: Path | None = None) -> str:
        """
        宿主 cwd → 容器内路径。

        `D:\\...\\workspace\\src` → `/workspace/src`
        """
        root = self._workspace_root(cwd, ws_root)
        try:
            rel = cwd.resolve().relative_to(root)
        except ValueError:
            return CONTAINER_WORKSPACE
        if str(rel) in (".", ""):
            return CONTAINER_WORKSPACE
        return f"{CONTAINER_WORKSPACE}/" + str(rel).replace("\\", "/")

    async def _alive(self, cid: str) -> bool:
        code, out = await self._docker(
            ["inspect", "-f", "{{.State.Running}}", cid]
        )
        return code == 0 and out.strip().lower() == "true"

    # ─────────────────────────── 清理 ───────────────────────────

    async def cleanup_session(self, session_id: str) -> None:
        lock = await self._get_lock(session_id)
        async with lock:
            entry = self._containers.pop(session_id, None)
        # 即使缓存里没有也要按名字删一次 —— 缓存可能因为重启丢了，
        # 但容器还在
        await self._docker(["rm", "-f", f"{NAME_PREFIX}{session_id}"])
        if entry:
            log.info("docker_container_removed", session=session_id, cid=entry["cid"][:12])
        async with self._meta_lock:
            self._locks.pop(session_id, None)

    async def cleanup_expired(self) -> int:
        """回收空闲超时的容器。返回回收数量。"""
        now = time.time()
        stale = [sid for sid, e in self._containers.items() if e["expire_at"] < now]
        for sid in stale:
            await self.cleanup_session(sid)
        if stale:
            log.info("docker_containers_expired", count=len(stale))
        return len(stale)

    async def cleanup_orphans(self) -> int:
        """
        清理上次遗留的容器。

        ## 为什么必须有

        `--rm` 只在容器【自己停止】时生效，而保活命令 `tail -f /dev/null`
        不会自己停。进程被 kill -9、机器断电、Docker Desktop 重启时，
        清理钩子不执行，容器会一直运行占内存。

        而新启动的进程完全不知道它们存在（缓存是内存态），
        于是每次崩溃都累积一批。

        有 cleanup_all() 但只注册在正常退出路径上，缺这一条。

        这和启动脚本的端口检查是同一个道理：
        任何依赖退出钩子做清理的设计，都要在入口处能容忍
        "上次没清理干净"。
        """
        code, out = await self._docker(
            ["ps", "-aq", "--filter", f"name={NAME_PREFIX}"]
        )
        if code != 0:
            return 0
        ids = [x.strip() for x in out.splitlines() if x.strip()]
        # 排掉本进程正在用的（正常启动时 _containers 是空的，
        # 但 cleanup_orphans 也可能被手动调用）
        mine = {e["cid"] for e in self._containers.values()}
        ids = [i for i in ids if not any(m.startswith(i) or i.startswith(m) for m in mine)]
        if not ids:
            return 0
        await self._docker(["rm", "-f", *ids])
        log.info("docker_orphans_removed", count=len(ids))
        return len(ids)

    async def shutdown(self) -> None:
        for sid in list(self._containers):
            with contextlib.suppress(Exception):
                await self.cleanup_session(sid)

    # ─────────────────────────── docker CLI ───────────────────────────

    async def _docker(self, args: list[str]) -> tuple[int, str]:
        """
        跑一条 docker 命令，返回 (退出码, 合并输出)。

        永不抛异常 —— 调用方全都要判断退出码，抛异常会让每个调用点
        都得包 try。
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except (FileNotFoundError, OSError) as e:
            return 127, f"无法执行 docker：{e}"

        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=DOCKER_CLI_TIMEOUT)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return 124, f"docker 命令超时（{DOCKER_CLI_TIMEOUT:.0f}s），守护进程可能没响应"
        return proc.returncode or 0, out.decode("utf-8", errors="replace")

    async def _docker_stream(
        self, args: list[str], timeout: float  # noqa: ASYNC109
    ) -> tuple[int, str, bool]:
        """
        跑 docker exec，带被执行命令的超时。

        ## 超时后为什么还要杀容器里的进程

        `docker exec` 客户端被杀掉的话，**容器里的进程还在跑** ——
        它的父进程是容器的 init，不是 docker CLI。
        不管的话一个死循环会一直烧 CPU 直到容器被回收。

        所以超时后要在容器内 pkill。这是 host 执行时用
        kill_process_tree 的等价物。
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except (FileNotFoundError, OSError) as e:
            return 127, f"无法执行 docker：{e}", False

        timed_out = False
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            timed_out = True
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            out = b""
            # 容器 id 在 args 里 exec 参数之后 —— 找出它来 pkill
            cid = _extract_cid(args)
            if cid:
                await self._docker(["exec", cid, "pkill", "-9", "-f", "sh -c"])
        return (
            (124 if timed_out else (proc.returncode or 0)),
            out.decode("utf-8", errors="replace"),
            timed_out,
        )


def _is_container_broken(code: int, out: str) -> bool:
    """
    判断这次失败是"容器坏了"而不是"命令本身失败"。

    ## 为什么要靠字符串匹配

    Docker 没有为这种情况提供专门的退出码 —— 128 是通用的
    "容器内无法启动进程"，正常命令也可能返回 128（比如被 SIGKILL）。

    所以必须看 stderr 内容。这几个片段是 runc / containerd 在
    容器无法再创建进程时的固定输出。

    ## 为什么不能只看退出码 128

    `sh -c "kill -9 $$"` 也会得到 128，那是命令自己的行为，
    重建容器解决不了任何问题，只会白白丢掉容器状态。
    """
    if code != 128:
        return False
    low = out.lower()
    return any(
        m in low
        for m in (
            "procready not received",
            "oci runtime exec failed",
            "unable to start container process",
        )
    )


def _extract_cid(args: list[str]) -> str:
    """
    从 docker exec 参数里取容器 id。

    参数形如 ["exec", "-w", "/workspace", "-e", "K=V", <cid>, "sh", "-c", cmd]
    —— cid 是第一个不以 - 开头、且前一个不是带值选项的位置参数。
    """
    valued = {"-w", "-e", "-u", "--workdir", "--env", "--user"}
    i = 1  # 跳过 "exec"
    while i < len(args):
        a = args[i]
        if a in valued:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a
    return ""


def looks_like_container_name(s: str) -> bool:
    """容器名是否是本项目创建的。清理时用。"""
    return bool(re.match(rf"^{re.escape(NAME_PREFIX)}[\w-]+$", s))
