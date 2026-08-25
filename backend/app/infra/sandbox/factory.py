"""
沙箱后端选择。

## 为什么降级必须显式告知

配了 `backend=docker` 的用户**就是想要隔离**。检测不到 Docker 时静默
回落到本地执行等于骗他 —— 他会以为命令跑在容器里，于是放心地让 agent
执行危险操作。

所以降级时：
1. 记 warning（运维能看到）
2. 记录降级原因，通过 `/api/meta` 暴露给前端
3. 前端在该会话内**持续显示**提示条（不是一闪而过的 toast）

第 3 条是 sandbox.md 明确要求的 —— 用户需要【一直】知道当前不是隔离环境。

## 为什么缓存选择结果

`health()` 要跑 `docker version` + `docker image inspect`，两次子进程
调用约几百毫秒。每次执行命令都探一遍的话，一轮对话里调五次工具就多花
一两秒。

而 Docker 从"可用"变成"不可用"（Docker Desktop 被关掉）时，
`run()` 自己会失败并返回错误文本 —— 不需要靠 health 提前发现。
"""

from __future__ import annotations

import structlog
from app.core.config import settings
from app.infra.sandbox.docker import DockerSandbox
from app.infra.sandbox.local import LocalSandbox
from app.infra.sandbox.port import SandboxPort

log = structlog.get_logger(__name__)

_cached: dict[str, SandboxPort] = {}
_fallback_reason: str = ""


async def get_sandbox(ws_cfg: dict[str, str] | None = None) -> SandboxPort:
    """
    按工作区配置拿沙箱后端。结果按 (backend, image) 缓存。

    ws_cfg 来自工作区（Workspace 表的 sandbox_* 字段）：
    {"backend": "local"|"docker", "container": ..., "image": ..., "network": ...}
    空则回落全局 settings。
    """
    global _fallback_reason
    cfg = ws_cfg or {}
    backend = (cfg.get("backend") or settings.sandbox.backend or "local").strip().lower()
    image = cfg.get("image") or settings.sandbox.docker_image

    if backend == "docker":
        key = f"docker:{image}"
        if key in _cached:
            return _cached[key]

        docker = DockerSandbox()
        ok, reason = await docker.health()
        if ok:
            log.info(
                "sandbox_backend",
                backend="docker",
                image=image,
                network=cfg.get("network") or settings.sandbox.docker_network,
            )
            _cached[key] = docker
            _fallback_reason = ""
            return docker

        # 【不能静默回落】。
        #
        # 用户配了 docker 就是想要隔离。静默用本地执行的话，
        # 他会以为命令跑在容器里，于是放心地让 agent 执行危险操作。
        log.warning("docker_unavailable_fallback_to_local", reason=reason)
        _fallback_reason = reason
    elif backend not in ("local", ""):
        log.warning("sandbox_unknown_backend", backend=backend)
        _fallback_reason = f"未知的沙箱后端 {backend!r}，已用本地执行"

    if "local" not in _cached:
        _cached["local"] = LocalSandbox()
    return _cached["local"]


def fallback_reason() -> str:
    """
    降级原因。空字符串表示没降级。

    走 /api/meta 给前端 —— 前端据此显示"当前非隔离环境"提示条。
    """
    return _fallback_reason


def reset_cache() -> None:
    """测试用：清掉缓存，让下次 get_sandbox 重新探测。"""
    global _cached, _fallback_reason
    _cached = {}
    _fallback_reason = ""
