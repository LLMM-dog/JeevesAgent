"""
定时任务接口。
"""

from __future__ import annotations

from typing import Any

import structlog
from app.core.exceptions import BadRequestError, NotFoundError
from app.infra.db.session import get_db
from app.modules.cron import models as m
from app.modules.cron import repo
from app.modules.cron import schedule as sch
from app.modules.cron.scheduler import scheduler
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)
# 不带 prefix —— main.py 的 include_router 会加 settings.api_prefix。
# 自己写死 /api 前缀，改配置时这个路由会漏掉，
# 表现是"其它接口都变了但定时任务还在老路径上"。
router = APIRouter()


class CreateTaskRequest(BaseModel):
    name: str = Field("", max_length=200)
    prompt: str = Field(..., min_length=1)
    cron: str = Field(..., min_length=1, max_length=120)
    workspace_id: str = ""
    timezone: str = ""
    on_missed: str = m.ON_MISSED_SKIP
    enabled: bool = True


class PatchTaskRequest(BaseModel):
    name: str | None = None
    prompt: str | None = None
    cron: str | None = None
    timezone: str | None = None
    on_missed: str | None = None
    enabled: bool | None = None


class TaskOut(BaseModel):
    id: str
    name: str
    prompt: str
    cron: str
    cron_text: str
    timezone: str
    workspace_id: str
    enabled: bool
    on_missed: str
    last_fired_at: int
    next_fire_at: int
    run_count: int
    fail_count: int
    created_at: int


class RunOut(BaseModel):
    id: str
    task_id: str
    scheduled_at: int
    started_at: int
    finished_at: int
    status: str
    detail: str
    session_id: str


def _out(t: m.CronTask, next_ms: int = 0) -> TaskOut:
    return TaskOut(
        id=t.id,
        name=t.name,
        prompt=t.prompt,
        cron=t.cron,
        # 把表达式翻成中文一起返回 —— 列表页显示 "0 9 * * *" 的话
        # 用户每次都要在心里解析一遍
        cron_text=sch.describe(t.cron),
        timezone=t.timezone,
        workspace_id=t.workspace_id,
        enabled=bool(t.enabled),
        on_missed=t.on_missed,
        last_fired_at=t.last_fired_at,
        next_fire_at=next_ms or t.next_fire_at,
        run_count=t.run_count,
        fail_count=t.fail_count,
        created_at=t.created_at,
    )


async def _reload_safely(what: str) -> None:
    """
    reload 调度器，失败只记日志。

    ## 为什么不能让它抛

    调用方都是【已经 commit 成功】的写操作。reload 抛出去会变成 500，
    而客户端看到 500 会以为创建失败并重试 —— 于是建出两个一样的任务。

    调度器状态过期的后果轻得多：任务最多晚一个周期才被看到，
    而下次任何写操作或重启都会重新装载。
    """
    try:
        await scheduler.reload()
    except Exception as e:  # noqa: BLE001
        log.warning("cron_reload_failed", after=what, err=str(e)[:200])


def _check_cron(expr: str) -> None:
    """
    入口校验。

    只在调度时 catch 异常——
    非法表达式能存进库，然后每次调度都抛，而用户以为任务建好了。
    """
    err = sch.validate(expr)
    if err:
        raise BadRequestError(err, code="bad_cron")


def _check_on_missed(v: str) -> None:
    if v not in (m.ON_MISSED_SKIP, m.ON_MISSED_RUN):
        raise BadRequestError(
            f"on_missed 只能是 {m.ON_MISSED_SKIP} 或 {m.ON_MISSED_RUN}",
            code="bad_on_missed",
        )


@router.get("/cron/tasks", summary="定时任务列表")
async def list_tasks(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    tasks = await repo.list_tasks(db)
    return {
        "items": [_out(t, scheduler.next_fire_ms(t.id)) for t in tasks],
        "scheduler_loaded": scheduler.loaded_count,
        "scheduler_inflight": scheduler.inflight_count,
    }


@router.post("/cron/tasks", response_model=TaskOut, status_code=201, summary="新建定时任务")
async def create_task(
    body: CreateTaskRequest, db: AsyncSession = Depends(get_db)
) -> TaskOut:
    """
    新建定时任务。

    ## 注意：任务触发的会话强制 auto 审批

    manual 模式下 agent 会停下来等审批，而没有人在旁边点 ——
    任务会挂到审批超时然后失败。

    所以定时任务里的 agent 能不经确认执行命令。这一点在界面上也有提示。
    """
    _check_cron(body.cron)
    _check_on_missed(body.on_missed)
    if body.timezone:
        # 时区名写错的话任务会按错误的时间触发 —— 入口拦掉
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(body.timezone)
        except (ZoneInfoNotFoundError, ValueError, OSError) as e:
            raise BadRequestError(
                f"时区名非法：{body.timezone!r}（应为 IANA 名称，如 Asia/Shanghai）",
                code="bad_timezone",
            ) from e

    ws = body.workspace_id
    if not ws:
        # 复用 session repo 里已有的默认工作区解析逻辑，
        # 而不是自己再查一遍 is_default —— 两处各写一遍的话，
        # 将来"默认工作区"的定义改了只会改到一处
        from app.modules.session.models import Workspace
        from sqlalchemy import select

        w = (
            await db.execute(select(Workspace).where(Workspace.is_default == 1))
        ).scalar_one_or_none()
        if w is None:
            raise BadRequestError("没有可用的工作区", code="no_workspace")
        ws = w.id

    t = await repo.create_task(
        db,
        name=body.name,
        prompt=body.prompt,
        cron=body.cron,
        workspace_id=ws,
        timezone=body.timezone,
        on_missed=body.on_missed,
        enabled=body.enabled,
    )
    await db.commit()

    # 让调度器立刻看到新任务。
    #
    # 不 reload 的话它要等到下一次唤醒才知道 —— 而如果堆里最近的任务
    # 在 6 小时后，新建的"1 分钟后执行"就会被推迟 6 小时。
    await _reload_safely("create")
    return _out(t, scheduler.next_fire_ms(t.id))


@router.patch("/cron/tasks/{task_id}", response_model=TaskOut, summary="修改定时任务")
async def patch_task(
    task_id: str, body: PatchTaskRequest, db: AsyncSession = Depends(get_db)
) -> TaskOut:
    t = await repo.get_task(db, task_id)
    if t is None:
        raise NotFoundError("定时任务不存在", code="task_not_found")

    if body.cron is not None:
        _check_cron(body.cron)
        t.cron = body.cron
    if body.on_missed is not None:
        _check_on_missed(body.on_missed)
        t.on_missed = body.on_missed
    if body.name is not None:
        t.name = body.name
    if body.prompt is not None:
        if not body.prompt.strip():
            raise BadRequestError("prompt 不能为空", code="empty_prompt")
        t.prompt = body.prompt
    if body.timezone is not None:
        t.timezone = body.timezone
    was_enabled = bool(t.enabled)
    if body.enabled is not None:
        t.enabled = 1 if body.enabled else 0
        # 【停用要停掉正在跑的那次】。
        #
        # 不停的话 agent 对话会继续跑完（可能几分钟，且是强制 auto
        # 审批下执行工具）—— 用户点了"停用"却发现助手还在动。
        if was_enabled and not body.enabled:
            n = scheduler.cancel_running(task_id)
            if n:
                log.info("cron_cancelled_on_disable", task=task_id, count=n)

    # 改了 cron/时区后，上次触发时间的语义变了。
    #
    # 不处理的话：停用一段时间再启用，错过检测会认为期间的窗口都错过了
    # —— 落一条 missed 记录，或者（on_missed=run_once 且在 6 小时宽限内）
    # 【立刻补跑一次】。用户只是关了又开，却看到任务自己跑了。
    if body.enabled is True and not was_enabled:
        from app.core.time import now_ms

        t.last_fired_at = now_ms()

    await db.commit()
    await _reload_safely("patch")
    return _out(t, scheduler.next_fire_ms(t.id))


@router.delete("/cron/tasks/{task_id}", summary="删除定时任务")
async def delete_task(task_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, bool]:
    # 【先取消正在跑的，再删】。
    #
    # 顺序不能反：删完再取消的话，那次执行的 finally 会去写 cron_run，
    # 而 task 行已经没了 —— CASCADE 把 running 记录也删了，
    # 于是 finish_run 走 add_run 分支撞外键，IntegrityError 被
    # 两层 suppress 静默吞掉。表现是对话跑完了但账面上什么都没发生。
    #
    # （repo.finish_run 现在也会检查任务是否还在，这里是双保险：
    #  取消掉能省下真实的 LLM 调用。）
    n = scheduler.cancel_running(task_id)
    if n:
        log.info("cron_cancelled_on_delete", task=task_id, count=n)

    if not await repo.delete_task(db, task_id):
        raise NotFoundError("定时任务不存在", code="task_not_found")
    await db.commit()
    await _reload_safely("delete")
    return {"ok": True}


@router.get("/cron/tasks/{task_id}/runs", summary="执行历史")
async def list_runs(
    task_id: str, limit: int = 50, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    t = await repo.get_task(db, task_id)
    if t is None:
        raise NotFoundError("定时任务不存在", code="task_not_found")
    runs = await repo.list_runs(db, task_id, limit=max(1, min(200, limit)))
    return {
        "items": [
            RunOut(
                id=r.id,
                task_id=r.task_id,
                scheduled_at=r.scheduled_at,
                started_at=r.started_at,
                finished_at=r.finished_at,
                status=r.status,
                detail=r.detail,
                session_id=r.session_id,
            )
            for r in runs
        ]
    }


@router.post("/cron/tasks/{task_id}/run", summary="立即执行一次")
async def run_now(task_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """
    手动触发一次。

    ## 为什么需要这个

    定时任务的反馈周期很长 —— 建一个"每天 9:00"的任务，要等到明天
    才知道 prompt 写得对不对。

    而调试期反复改 cron 表达式去凑一个"1 分钟后"再等一分钟，
    是很糟的体验。
    """
    t = await repo.get_task(db, task_id)
    if t is None:
        raise NotFoundError("定时任务不存在", code="task_not_found")

    # 【禁用的任务必须在这里拒掉】。
    #
    # 原来是照样插一条 running 记录再 _spawn —— 而 runner.run_task
    # 开头发现 enabled=0 就直接 return，不做任何收尾。
    #
    # 结果那条记录【永久停在 running】，用户看到"任务卡在执行中"，
    # 只能等下次重启由 clear_stale_running 兜底。
    #
    # 而且 run_task 是正常返回（无异常），所以 _run_guarded 的
    # _mark_failed 也不会执行。
    if not t.enabled:
        raise BadRequestError(
            "任务已停用。手动执行请先启用它 —— "
            "否则执行会在开始时被跳过，只留下一条无法收尾的记录",
            code="task_disabled",
        )

    from app.core.time import now_ms

    when = now_ms()
    await repo.add_run(
        db, task_id=task_id, scheduled_at=when, status=m.RUN_RUNNING, detail="手动触发"
    )
    await db.commit()

    scheduler.spawn(task_id, when)
    return {"ok": True, "scheduled_at": when}


@router.post("/cron/validate", summary="校验 cron 表达式")
async def validate_cron(body: dict[str, Any]) -> dict[str, Any]:
    """
    校验并预览接下来几次触发时间。

    ## 为什么要预览

    cron 表达式很容易写错且错得不明显 —— `0 9 * * 1` 到底是周一还是周日？
    看到"接下来三次是 8/10 09:00、8/17 09:00、8/24 09:00"就一目了然。
    """
    expr = str(body.get("cron") or "")
    tz = str(body.get("timezone") or "")
    err = sch.validate(expr)
    if err:
        return {"valid": False, "error": err, "next": []}

    import datetime as dt

    out: list[int] = []
    cur: dt.datetime | None = None
    try:
        for _ in range(5):
            cur = sch.next_after(expr, tz, after=cur)
            out.append(sch.to_ms(cur))
    except Exception as e:  # noqa: BLE001
        return {"valid": False, "error": f"计算触发时间失败：{e}"[:200], "next": []}

    return {"valid": True, "error": "", "text": sch.describe(expr), "next": out}
