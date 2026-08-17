"""
运行中可变的会话级状态。

## 为什么不用 ContextVar

asyncio.create_task 会【复制】当前 context。task 启动后，外部对 ContextVar 的
set() 对它不可见。

在 api/agent/interaction.py 里为此刻意用了模块级 dict，注释写着：
「使用模块级 dict 而非 ContextVar，以便 WebSocket 主循环在处理
update_auto_approve 消息时写入的值能立即被正在运行的 Agent 任务中的
工具函数读取到」。

本项目会踩在同一个地方：用户在流式进行中把审批模式从 manual 切成 auto,
如果走 ContextVar，正在跑的 run 读不到新值，用户会觉得"我都切成自动了它还在弹框"。

## 划分规则

| 类别 | 载体 |
| 一次 run 内不变（run_id / span / 事件总线） | ContextVar |
| 运行中可能被改（审批模式、审批结果、交互回答） | 本模块的 dict |
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal, cast

ApprovalMode = Literal["manual", "auto"]


@dataclass
class PendingApproval:
    call_id: str
    future: asyncio.Future[bool]


@dataclass
class PendingInteract:
    call_id: str
    future: asyncio.Future[Any]


@dataclass
class SessionRuntime:
    """一个会话的运行时状态。进程内存，不落库。"""

    approval_mode: ApprovalMode = "manual"
    approvals: dict[str, PendingApproval] = field(default_factory=dict)
    interacts: dict[str, PendingInteract] = field(default_factory=dict)

    # 记忆提取状态
    memory_extracting: bool = False  # 是否正在提取
    memory_extraction_id: str = ""   # 当前提取的 ID


_sessions: dict[str, SessionRuntime] = {}

# 新建会话时的默认审批模式。
#
# 生产是 "manual"：执行类工具和写文件都要人确认。这是正确的默认值 ——
# 用户离开电脑时不该有命令自己跑起来。
#
# 但测试需要能改它。不能靠 patch dataclass 的字段默认值：dataclass 在类创建时
# 就把默认值烧进了 __init__ 签名，改 `SessionRuntime.approval_mode` 或
# `__dataclass_fields__[...].default` 对新建实例都【完全无效】（实测过两种都不行）。
# 所以把默认值提到模块级变量，由 get_runtime 读取。
_default_mode: ApprovalMode = "manual"


def set_default_approval_mode(mode: ApprovalMode) -> ApprovalMode:
    """
    改新建会话的默认审批模式，返回改之前的值。

    给测试和 setup 脚本用。已存在的会话不受影响 ——
    它们的模式是用户显式设过的，不该被默认值覆盖。
    """
    global _default_mode
    previous = _default_mode
    _default_mode = mode
    return previous


def get_runtime(session_id: str) -> SessionRuntime:
    rt = _sessions.get(session_id)
    if rt is None:
        rt = SessionRuntime(approval_mode=_default_mode)
        _sessions[session_id] = rt
    return rt


def set_approval_mode(session_id: str, mode: ApprovalMode) -> None:
    """
    立即对正在运行的 run 生效。这是本模块存在的全部理由。
    """
    get_runtime(session_id).approval_mode = mode


def restore_approval_mode(session_id: str, mode: str) -> None:
    """
    从持久化层恢复审批模式到运行时状态。

    与 set_approval_mode 的区别：DB 里的值是 str（不保证是合法 Literal），
    这里做防御性校验，非法值回落 manual。用于应用重启后 / 加载会话时
    把 DB 的 approval_mode 同步回 runtime_state。
    """
    get_runtime(session_id).approval_mode = cast(
        ApprovalMode, mode if mode in ("manual", "auto") else "manual"
    )


def get_approval_mode(session_id: str) -> ApprovalMode:
    return get_runtime(session_id).approval_mode


def register_approval(session_id: str, call_id: str) -> asyncio.Future[bool]:
    rt = get_runtime(session_id)
    fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    rt.approvals[call_id] = PendingApproval(call_id=call_id, future=fut)
    return fut


def resolve_approval(session_id: str, call_id: str, approved: bool) -> bool:
    """返回是否成功回填（False 表示该 call_id 不存在或已完成）。"""
    rt = get_runtime(session_id)
    pending = rt.approvals.pop(call_id, None)
    if pending is None or pending.future.done():
        return False
    pending.future.set_result(approved)
    return True


def register_interact(session_id: str, call_id: str) -> asyncio.Future[Any]:
    rt = get_runtime(session_id)
    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    rt.interacts[call_id] = PendingInteract(call_id=call_id, future=fut)
    return fut


def resolve_interact(session_id: str, call_id: str, answer: Any) -> bool:
    rt = get_runtime(session_id)
    pending = rt.interacts.pop(call_id, None)
    if pending is None or pending.future.done():
        return False
    pending.future.set_result(answer)
    return True


def cancel_all_pending(session_id: str) -> None:
    """
    取消时清空所有挂起的等待。

    用 set_result 而非 cancel（照抄 做法）：
    工具函数在 await 这个 future，set_result 让它能正常返回一个"被取消"的结果,
    而 cancel 会在工具内部抛 CancelledError，破坏工具自己的清理逻辑。
    """
    rt = get_runtime(session_id)
    for pending in list(rt.approvals.values()):
        if not pending.future.done():
            pending.future.set_result(False)
    rt.approvals.clear()
    for interact in list(rt.interacts.values()):
        if not interact.future.done():
            interact.future.set_result(None)
    rt.interacts.clear()


def drop_session(session_id: str) -> None:
    """
    会话删除时清理。

    这类 per-session 状态必须有显式清理路径 —— RetrieveMemoryNode._seen 是类变量且永不清理，进程级永久累积。
    """
    cancel_all_pending(session_id)
    _sessions.pop(session_id, None)


# ── 记忆提取状态 ──


def set_memory_extracting(session_id: str, extracting: bool, extraction_id: str = "") -> None:
    """
    设置会话的记忆提取状态。

    提取开始时调用 set_memory_extracting(session_id, True, extraction_id)
    提取结束时调用 set_memory_extracting(session_id, False)
    """
    rt = get_runtime(session_id)
    rt.memory_extracting = extracting
    rt.memory_extraction_id = extraction_id if extracting else ""


def is_memory_extracting(session_id: str) -> bool:
    """检查会话是否正在进行记忆提取。"""
    rt = _sessions.get(session_id)
    return rt.memory_extracting if rt else False


def get_memory_extraction_status(session_id: str) -> dict[str, Any]:
    """
    获取会话的记忆提取状态。

    返回：
    {
        "extracting": bool,
        "extraction_id": str
    }
    """
    rt = _sessions.get(session_id)
    if not rt:
        return {"extracting": False, "extraction_id": ""}
    return {
        "extracting": rt.memory_extracting,
        "extraction_id": rt.memory_extraction_id,
    }
