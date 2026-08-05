"""
定时任务的执行。

## 为什么必须无头

触发时用户可能根本没打开浏览器 —— 这是最典型的无头场景。

`subagent.md` 里已经强调过同一条：绝不能走 那种
"等外部连接来触发执行"的路。
那样定时任务永远不会执行，而没有任何报错。

具体做法是【把 stream() 完整抽干】。ChatService.stream() 是个 async
生成器，产出 SSE 文本给前端。没有消费者的话生成器根本不会执行 ——
调用一个 async 生成器函数不执行任何函数体代码。

所以这里 `async for _ in stream(...)` 把它跑完，丢掉产出的文本。
副作用（消息落库、工具执行、追踪）都在生成器内部发生。
"""

from __future__ import annotations

import contextlib

import structlog

from app.core.time import now_ms
from app.modules.cron import models as m
from app.modules.cron import repo

log = structlog.get_logger(__name__)

# 注入进来的 ChatService。
#
# 不在这里 import app.main：那会形成循环（main 导入 cron 的路由，
# cron 又导入 main）。而循环导入的表现是"某个名字在导入时还不存在"，
# 报错指向一个看起来无关的位置。
_chat_service = None


def bind_chat_service(svc: object) -> None:
    """lifespan 里调一次。"""
    global _chat_service
    _chat_service = svc


async def run_task(task_id: str, scheduled_ms: int) -> None:
    """
    执行一个定时任务：建会话 → 发消息 → 跑完 agent 循环。

    异常往上抛，由 scheduler._run_guarded 统一记账。
    """
    from app.modules.session import repo as srepo

    if _chat_service is None:
        raise RuntimeError("ChatService 未注入，定时任务无法执行")

    # 会话工厂从 scheduler 拿 —— 它可以被测试替换。
    #
    # 直接 get_sessionmaker() 的话测试无法拦截，会往真实库写。
    from app.modules.cron.scheduler import scheduler

    sm = scheduler._sm()  # noqa: SLF001

    # ── 建会话 ──
    async with sm() as db:
        task = await repo.get_task(db, task_id)
        if task is None:
            log.warning("cron_task_gone", task=task_id)
            return
        if not task.enabled:
            log.info("cron_task_disabled_skip", task=task_id)
            return

        title = f"[定时] {task.name or task.cron}"
        session = await srepo.create_session(
            db, title=title, workspace_id=task.workspace_id
        )
        # 【强制 auto 审批】。
        #
        # manual 模式下 agent 会停下来等审批，而【没有人在旁边点】——
        # 任务会挂到审批超时（本项目 5 分钟）然后失败。
        #
        # 这个风险必须在创建任务时就告诉用户（接口文档和界面都写了），
        # 不能埋在代码里。
        session.approval_mode = "auto"
        sid = session.id
        prompt = task.prompt

        # 把 running 记录关联上会话 —— 用户要能点进去看 agent 做了什么
        await repo.finish_run(
            db,
            task_id=task_id,
            scheduled_at=scheduled_ms,
            status=m.RUN_RUNNING,
            session_id=sid,
        )
        await repo.touch_fired(db, task_id, scheduled_ms)
        await db.commit()

    log.info("cron_task_start", task=task_id, session=sid)

    # ── 跑对话 ──
    ok = True
    detail = ""
    try:
        prep = await _chat_service.prepare(session_id=sid, content=prompt)  # type: ignore[attr-defined]
        # 【必须完整消费】—— 见模块 docstring
        async for _chunk in _chat_service.stream(prep):  # type: ignore[attr-defined]
            pass
    except Exception as e:
        ok = False
        detail = f"{type(e).__name__}: {e}"[:400]
        raise
    finally:
        async with sm() as db:
            await repo.finish_run(
                db,
                task_id=task_id,
                scheduled_at=scheduled_ms,
                status=m.RUN_OK if ok else m.RUN_FAILED,
                detail=detail,
                session_id=sid,
            )
            if ok:
                await repo.bump_run(db, task_id)
            else:
                await repo.bump_fail(db, task_id)
            await db.commit()
        log.info(
            "cron_task_finish",
            task=task_id,
            session=sid,
            ok=ok,
            elapsed_ms=now_ms() - scheduled_ms,
        )

        # 【必须清掉这个会话的沙箱资源】。
        #
        # Docker 后端是每会话一个容器，而定时任务【每次触发都建一个新
        # 会话】—— 不清的话每次触发泄漏一个容器。
        #
        # 实测：模拟三次触发就留下三个常驻容器。一个"每小时"的任务
        # 跑一天是 24 个，每个占几十 MB 且吃 2g memory limit 的额度。
        # 不重启就没有上界。
        #
        # 而普通会话有删除入口（用户手动删会话时清理），
        # 定时任务的会话用户没有动力去删 —— 那不是他建的。
        #
        # IDLE_TTL 和 cleanup_expired() 都存在，但【没有任何东西调用
        # 它们】，所以不能指望 TTL 兜底。
        with contextlib.suppress(Exception):
            from app.infra.sandbox.factory import get_sandbox

            await (await get_sandbox()).cleanup_session(sid)
