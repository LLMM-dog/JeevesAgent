"""
run 注册表。

进程内 dict：run_id → asyncio.Task。单进程单用户，不需要分布式方案。
"""

import asyncio
from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)


@dataclass
class RunHandle:
    run_id: str
    session_id: str
    task: asyncio.Task[None]


_runs: dict[str, RunHandle] = {}
# session_id → run_id。用于拦"一个会话同时只允许一个 run"：
# 前端在流未结束时会禁用发送按钮，但后端必须也拦 —— 用户可能开两个标签页。
_active_by_session: dict[str, str] = {}


def register(run_id: str, session_id: str, task: asyncio.Task[None]) -> None:
    _runs[run_id] = RunHandle(run_id=run_id, session_id=session_id, task=task)
    _active_by_session[session_id] = run_id


def unregister(run_id: str) -> None:
    handle = _runs.pop(run_id, None)
    if handle is not None and _active_by_session.get(handle.session_id) == run_id:
        _active_by_session.pop(handle.session_id, None)


def get(run_id: str) -> RunHandle | None:
    return _runs.get(run_id)


def active_run_of(session_id: str) -> str | None:
    run_id = _active_by_session.get(session_id)
    if run_id is None:
        return None
    handle = _runs.get(run_id)
    if handle is None or handle.task.done():
        # 兜底清理：任务已结束但注册表没清（异常路径）
        _active_by_session.pop(session_id, None)
        return None
    return run_id


def cancel(run_id: str) -> bool:
    """
    返回是否真的发出了取消。已结束的 run 返回 False。

    取消幂等：路由层对 False 也返回 200，因为用户可能连点两次。
    """
    handle = _runs.get(run_id)
    if handle is None or handle.task.done():
        return False
    handle.task.cancel()
    log.info("run_cancel_requested", run_id=run_id)
    return True


async def cancel_all() -> None:
    """服务关闭时取消所有进行中的 run。"""
    handles = list(_runs.values())
    for h in handles:
        if not h.task.done():
            h.task.cancel()
    for h in handles:
        try:
            await h.task
        except (asyncio.CancelledError, Exception):  # noqa: B014
            pass
    _runs.clear()
    _active_by_session.clear()
