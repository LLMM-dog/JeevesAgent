"""
定时任务的数据访问。
"""

from __future__ import annotations

import structlog
from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ids
from app.core.time import now_ms
from app.modules.cron import models as m

log = structlog.get_logger(__name__)


async def create_task(
    db: AsyncSession,
    *,
    prompt: str,
    # 名字可以不填 —— 界面上会退回显示 cron 表达式的中文描述。
    # 强制填名字只是给用户增加一道摩擦，而"每天 18:00"本身就够识别了。
    name: str = "",
    cron: str,
    workspace_id: str,
    timezone: str = "",
    on_missed: str = m.ON_MISSED_SKIP,
    enabled: bool = True,
) -> m.CronTask:
    t = m.CronTask(
        id=ids.cron_task_id(),
        name=name,
        prompt=prompt,
        cron=cron,
        timezone=timezone,
        workspace_id=workspace_id,
        on_missed=on_missed,
        enabled=1 if enabled else 0,
        # 新建任务的 last_fired_at 设成【现在】而不是 0。
        #
        # 设 0 的话错过检测会认为"从 1970 年至今的所有窗口都错过了"——
        # 而 prev_before 只返回最近一个，所以表现是：新建一个"每天 9:00"
        # 的任务，如果现在是 10:00，它会立刻触发一次（因为今天 9:00
        # 晚于 last_fired_at=0）。
        #
        # 用户刚建好任务就看到它立刻跑了一次，会以为自己配错了。
        last_fired_at=now_ms(),
    )
    db.add(t)
    await db.flush()
    return t


async def get_task(db: AsyncSession, task_id: str) -> m.CronTask | None:
    return await db.get(m.CronTask, task_id)


async def list_tasks(db: AsyncSession) -> list[m.CronTask]:
    rows = await db.execute(select(m.CronTask).order_by(desc(m.CronTask.created_at)))
    return list(rows.scalars())


async def list_enabled(db: AsyncSession) -> list[m.CronTask]:
    rows = await db.execute(select(m.CronTask).where(m.CronTask.enabled == 1))
    return list(rows.scalars())


async def delete_task(db: AsyncSession, task_id: str) -> bool:
    t = await db.get(m.CronTask, task_id)
    if t is None:
        return False
    await db.delete(t)
    return True


async def touch_fired(db: AsyncSession, task_id: str, when_ms: int) -> None:
    """
    记录"这个窗口已经处理过了"。

    错过检测靠它 —— 不更新的话，同一个错过的窗口会在每次重启时
    重复产生一条 missed 记录。
    """
    await db.execute(
        update(m.CronTask)
        .where(m.CronTask.id == task_id)
        .values(last_fired_at=when_ms, updated_at=now_ms())
    )


async def set_next_fire(db: AsyncSession, task_id: str, when_ms: int) -> None:
    await db.execute(
        update(m.CronTask).where(m.CronTask.id == task_id).values(next_fire_at=when_ms)
    )


async def bump_run(db: AsyncSession, task_id: str) -> None:
    t = await db.get(m.CronTask, task_id)
    if t:
        t.run_count += 1


async def bump_fail(db: AsyncSession, task_id: str) -> None:
    t = await db.get(m.CronTask, task_id)
    if t:
        t.fail_count += 1


# ─────────────────────────── 执行历史 ───────────────────────────


async def add_run(
    db: AsyncSession,
    *,
    task_id: str,
    scheduled_at: int,
    status: str = m.RUN_RUNNING,
    detail: str = "",
    session_id: str = "",
) -> m.CronRun:
    r = m.CronRun(
        id=ids.cron_run_id(),
        task_id=task_id,
        scheduled_at=scheduled_at,
        started_at=now_ms() if status == m.RUN_RUNNING else 0,
        status=status,
        detail=detail,
        session_id=session_id,
    )
    db.add(r)
    await db.flush()
    return r


async def finish_run(
    db: AsyncSession,
    *,
    task_id: str,
    scheduled_at: int,
    status: str,
    detail: str = "",
    session_id: str = "",
) -> None:
    """
    收尾一条执行记录。

    按 (task_id, scheduled_at) 定位而不是按 run_id：调用方
    （scheduler 的异常处理路径）拿不到 run_id —— 它只知道
    "哪个任务的哪次触发失败了"。
    """
    rows = await db.execute(
        select(m.CronRun)
        .where(m.CronRun.task_id == task_id, m.CronRun.scheduled_at == scheduled_at)
        .order_by(desc(m.CronRun.created_at))
        .limit(1)
    )
    r = rows.scalar_one_or_none()
    if r is None:
        # 没有对应的 running 记录，补一条 —— 比丢掉这次执行的信息好。
        #
        # 【但要先确认任务还在】。
        #
        # 任务在执行中途被删掉时，cron_run.task_id 的 CASCADE 会把那条
        # running 记录一起删掉。这里如果直接 add_run，就是往一个
        # 已不存在的 task_id 上插 —— 外键不成立，抛 IntegrityError。
        #
        # 而那个异常从 runner 的 finally 里抛出，会掩盖原始异常，
        # 然后被 scheduler._mark_failed 外面的 contextlib.suppress
        # 整个吞掉。表现是：对话跑完了、会话建出来了，
        # 但执行历史一条不留，日志里只有一条 cron_run_failed。
        if await db.get(m.CronTask, task_id) is None:
            log.info(
                "cron_run_discarded_task_gone",
                task=task_id,
                scheduled_at=scheduled_at,
                status=status,
            )
            return
        await add_run(
            db,
            task_id=task_id,
            scheduled_at=scheduled_at,
            status=status,
            detail=detail,
            session_id=session_id,
        )
        return
    r.status = status
    r.finished_at = now_ms()
    if detail:
        r.detail = detail
    if session_id:
        r.session_id = session_id


async def list_runs(db: AsyncSession, task_id: str, limit: int = 50) -> list[m.CronRun]:
    rows = await db.execute(
        select(m.CronRun)
        .where(m.CronRun.task_id == task_id)
        .order_by(desc(m.CronRun.scheduled_at))
        .limit(limit)
    )
    return list(rows.scalars())


async def clear_stale_running(db: AsyncSession) -> int:
    """
    把遗留的 running 记录标成失败。

    ## 为什么需要

    进程被 kill -9 时，正在执行的任务的记录永远停在 running ——
    而用户看到的是"任务卡在执行中"，且分不清是真在跑还是上次没退干净。

    和 Docker 沙箱的遗留容器清理、启动脚本的端口检查是同一类问题
    ：依赖退出钩子做的清理，入口处都要能兜住。
    """
    rows = await db.execute(select(m.CronRun).where(m.CronRun.status == m.RUN_RUNNING))
    stale = list(rows.scalars())
    for r in stale:
        r.status = m.RUN_FAILED
        r.finished_at = now_ms()
        r.detail = (r.detail + " / " if r.detail else "") + "服务重启，执行中断"
    return len(stale)
