"""
调度器。

## 抄 两个结构

**最小堆 + 版本号懒删除**：
改任务或取消任务时只 bump 版本号，不去堆里找那条记录删。worker 弹出堆顶
后对账，版本不符就跳过。

理由：`heapq` 没有 O(log n) 的删除。要删中间某条只能重建整个堆，
而任务的增删改在管理界面上是频繁操作。

**Event + wait_for 的可中断等待**：
不能用 `asyncio.sleep(timeout)` —— sleep 6 小时期间新增一个"1 分钟后执行"
的任务，那个新任务要等 6 小时后才被看到。

## 修 三个问题

| | 这里 |
| --- | --- |
| `await self._execute_task(...)` 串行阻塞 | `create_task` 并发 |
| 重启后重算下一次，错过的窗口静默消失 | `last_fired_at` + 错过检测 + 落库记录 |
| `execute` 字段用 `exec()` 跑任意代码 | 不做这个字段 |
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import heapq
import time
from typing import Any

import structlog

from app.core.time import now_ms
from app.modules.cron import schedule as sch

log = structlog.get_logger(__name__)

# 错过多久之后就不补了（毫秒）。
#
# 昨天的日报今天补出来意义不大，而"服务停了三天"的场景下补偿会在启动瞬间
# 触发一堆任务 —— 如果是每小时的任务，那是 72 次。
MISSED_GRACE_MS = 6 * 60 * 60 * 1000

# 同时执行的任务数上限。
#
# 一次任务触发的是一轮完整的 agent 对话（可能几分钟、多次 LLM 调用）。
# 不限的话，一个"每分钟"的任务配上慢对话，会不断堆积并发对话，
# 把 LLM 配额和内存吃光。
MAX_CONCURRENT = 3

# 多久扫一次空闲的沙箱容器（秒）。
#
# 比 DockerSandbox.IDLE_TTL（30 分钟）短一些，否则容器最多会多活一个
# 扫描周期。5 分钟意味着最坏情况多活 5 分钟，代价可以接受。
SWEEP_INTERVAL = 5 * 60


class CronScheduler:
    def __init__(self) -> None:
        # task_id -> (cron, tz, 下次触发的毫秒)
        self._tasks: dict[str, tuple[str, str, int]] = {}
        # task_id -> 版本号。改/删任务时 bump，让堆里的旧条目失效
        self._versions: dict[str, int] = {}
        # (触发毫秒, task_id, 版本)
        self._heap: list[tuple[int, str, int]] = []
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._sweeper: asyncio.Task[None] | None = None
        self._running = False
        # 正在执行的任务，用于优雅关闭时等它们收尾
        self._inflight: set[asyncio.Task[None]] = set()
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)
        # 数据库会话工厂。
        #
        # ## 为什么必须可注入
        #
        # 原来是在方法里直接 `from ...session import get_sessionmaker` ——
        # 那意味着【测试无法拦截】：FastAPI 的
        # `dependency_overrides[get_db]` 只影响走依赖注入的路由，
        # 而调度器是自己开会话的。
        #
        # 实测后果：12 个路由测试往【真实的 data/jeeves.db】写了 7 条
        # cron_run、建了 3 个真实会话。而且如果开发机上恰好有一个
        # on_missed=run_once 的任务在补偿窗口内，跑一次 pytest 会真的
        # 拉起一次 agent 对话（强制 auto 审批，能无确认执行工具）。
        #
        # 当时没炸只是因为测试里 ChatService 没注入，所以在
        # "ChatService 未注入" 那一步就失败了 —— 纯属运气。
        self._sessionmaker: Any = None

    def bind_sessionmaker(self, sm: Any) -> None:
        """测试用：换掉会话工厂，避免碰真实数据库。"""
        self._sessionmaker = sm

    def _sm(self) -> Any:
        if self._sessionmaker is not None:
            return self._sessionmaker

        # 【测试里禁止回落到真实库】。
        #
        # 只给全局单例绑 sessionmaker 是不够的 —— 测试里
        # `CronScheduler()` 直接 new 出来的实例拿不到那个绑定，
        # 于是静默回落真实数据库。
        #
        # 实测：这样漏掉的两个测试仍往真实库写了 2 条 cron_run。
        # 而"少了 5 条"这种部分修复最危险 —— 看起来像修好了。
        #
        # 所以在 pytest 下直接抛，让漏绑的测试立刻红。
        # 生产路径不受影响（没有 PYTEST_CURRENT_TEST 这个环境变量）。
        import os

        if "PYTEST_CURRENT_TEST" in os.environ:
            raise RuntimeError(
                "测试里的 CronScheduler 没绑 sessionmaker，"
                "会写真实数据库。用 conftest 的 db fixture，"
                "或显式调 bind_sessionmaker()"
            )

        from app.infra.db.session import get_sessionmaker

        return get_sessionmaker()

    # ─────────────────────────── 生命周期 ───────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self.reload()
        self._worker = asyncio.create_task(self._loop(), name="cron-worker")
        self._sweeper = asyncio.create_task(self._sweep_loop(), name="sandbox-sweeper")
        log.info("cron_scheduler_started", tasks=len(self._tasks))

    async def stop(self) -> None:
        self._running = False
        self._wake.set()
        for name in ("_worker", "_sweeper"):
            t = getattr(self, name, None)
            if t is not None:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
                setattr(self, name, None)
        # 等正在跑的任务收尾。
        #
        # 不等的话进程退出时它们被硬中断，cron_run 表里留下一堆
        # status=running 的记录 —— 而那些永远不会变成 ok 或 failed，
        # 用户看到的是"任务卡在执行中"。
        if self._inflight:
            log.info("cron_waiting_inflight", count=len(self._inflight))
            with contextlib.suppress(Exception):
                await asyncio.wait(self._inflight, timeout=10)
        log.info("cron_scheduler_stopped")

    # ─────────────────────────── 装载 ───────────────────────────

    async def reload(self) -> None:
        """
        从数据库重建调度状态。

        ## 为什么数据库是 source of truth

        内存状态在重启后就没了，而任务定义必须持久。同样的选择
        （`sync_tasks()` 清空内存并从库重建）。

        每次改任务后调一次 reload 比维护增量更新简单，而任务数量级
        （个人项目里几十个）下全量重建的开销可以忽略。
        """
        from app.modules.cron import repo

        self._tasks.clear()
        self._heap.clear()

        sm = self._sm()
        async with sm() as db:
            tasks = await repo.list_enabled(db)

        now = now_ms()
        missed: list[tuple[str, int]] = []

        for t in tasks:
            err = sch.validate(t.cron)
            if err:
                # 非法表达式跳过而不是让整个 reload 失败 ——
                # 一条坏数据不该废掉所有任务
                log.warning("cron_task_invalid", task=t.id, cron=t.cron, err=err)
                continue

            # ── 错过检测 ──
            #
            # 算出"上一个应该触发的时间点"。如果它晚于 last_fired_at，
            # 说明在服务没运行的那段时间里错过了一次。
            #
            # 没有这一步 —— 它直接算下一个时间点，错过的完全消失
            # 且无任何记录。用户视角是"我的日报今天没发"，
            # 而没有线索指向"服务当时没在跑"。
            try:
                prev_ms = sch.to_ms(sch.prev_before(t.cron, t.timezone))
            except Exception as e:  # noqa: BLE001
                log.warning("cron_prev_failed", task=t.id, err=str(e)[:120])
                prev_ms = 0

            if prev_ms and t.last_fired_at and prev_ms > t.last_fired_at:
                missed.append((t.id, prev_ms))

            self._schedule_next(t.id, t.cron, t.timezone, base_ms=now)

        # 错过的窗口统一处理 —— 放在装载循环之后，
        # 避免补偿执行影响后面任务的装载
        for task_id, when in missed:
            await self._handle_missed(task_id, when)

        self._wake.set()

    def _schedule_next(self, task_id: str, cron: str, tz: str, *, base_ms: int) -> int:
        """算下一次触发并入堆。返回触发毫秒。"""
        base = sch.from_ms(base_ms, tz)
        try:
            nxt = sch.to_ms(sch.next_after(cron, tz, after=base))
        except Exception as e:  # noqa: BLE001
            log.warning("cron_next_failed", task=task_id, cron=cron, err=str(e)[:120])
            return 0
        self._tasks[task_id] = (cron, tz, nxt)
        ver = self._versions.get(task_id, 0) + 1
        self._versions[task_id] = ver
        heapq.heappush(self._heap, (nxt, task_id, ver))
        return nxt

    # ─────────────────────────── 错过处理 ───────────────────────────

    async def _handle_missed(self, task_id: str, when_ms: int) -> None:
        """
        处理一个错过的窗口。

        无论哪种策略都【要落一条记录】—— 静默是最糟的选择。
        用户至少要能在历史里看到"这次因为服务没运行而错过"。
        """
        from app.modules.cron import models as m
        from app.modules.cron import repo

        sm = self._sm()
        async with sm() as db:
            task = await repo.get_task(db, task_id)
            if task is None:
                return
            late_ms = now_ms() - when_ms
            if task.on_missed == m.ON_MISSED_RUN and late_ms <= MISSED_GRACE_MS:
                log.info("cron_missed_catchup", task=task_id, late_min=late_ms // 60000)
                await repo.add_run(
                    db,
                    task_id=task_id,
                    scheduled_at=when_ms,
                    status=m.RUN_RUNNING,
                    detail=f"补偿执行（原定 {sch.from_ms(when_ms, task.timezone):%Y-%m-%d %H:%M}）",
                )
                await db.commit()
                self.spawn(task_id, when_ms)
                return

            # skip，或者超过补偿窗口
            why = (
                f"服务未运行，跳过（原定 {sch.from_ms(when_ms, task.timezone):%Y-%m-%d %H:%M}）"
                if task.on_missed == m.ON_MISSED_SKIP
                else f"错过超过 {MISSED_GRACE_MS // 3600000} 小时，不再补偿"
            )
            await repo.add_run(
                db,
                task_id=task_id,
                scheduled_at=when_ms,
                status=m.RUN_MISSED,
                detail=why,
            )
            await repo.touch_fired(db, task_id, when_ms)
            await db.commit()
        log.info("cron_missed_skipped", task=task_id, when=when_ms)

    # ─────────────────────────── worker ───────────────────────────

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                # worker 绝不能死。
                #
                # 死了的话所有任务静默失效 —— 没有报错，只是再也不触发了，
                # 而用户完全不会察觉。
                log.exception("cron_loop_error", err=str(e)[:200])
                await asyncio.sleep(5)

    async def _sweep_loop(self) -> None:
        """
        周期回收空闲的沙箱容器。

        ## 为什么需要一个独立的循环

        `DockerSandbox` 有 `IDLE_TTL` 和 `cleanup_expired()`，但实测
        **没有任何东西调用它们** —— TTL 是死配置。

        不能挂在 cron 的 `_tick` 里：那个循环只在任务到点时才醒，
        没有定时任务的用户永远不会触发回收。

        `--rm` 也救不了：保活命令 `tail -f /dev/null` 永不退出，
        所以 `--rm` 永不触发。
        """
        while self._running:
            try:
                await asyncio.sleep(SWEEP_INTERVAL)
                if not self._running:
                    return
                from app.infra.sandbox.factory import get_sandbox

                sb = await get_sandbox()
                fn = getattr(sb, "cleanup_expired", None)
                if fn is None:
                    return  # 本地后端没有这个概念，不用再循环
                n = await fn()
                if n:
                    log.info("sandbox_expired_reclaimed", count=n)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("sandbox_sweep_failed", err=str(e)[:200])

    async def _tick(self) -> None:
        if not self._heap:
            self._wake.clear()
            await self._wake.wait()
            return

        when, task_id, ver = self._heap[0]
        now = now_ms()

        if now < when:
            # 可中断等待。
            #
            # 【不能用 asyncio.sleep】—— sleep 6 小时期间新增一个
            # "1 分钟后执行"的任务，那个新任务要等 6 小时后才被看到。
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=(when - now) / 1000)
            except TimeoutError:
                pass  # 到点了
            else:
                return  # 被唤醒，重新看堆顶
            if not self._running:
                return

        if not self._heap:
            return
        heapq.heappop(self._heap)

        # 版本对账：改过或删过的任务，旧条目直接丢弃
        if self._versions.get(task_id, -1) != ver or task_id not in self._tasks:
            return

        cron, tz, _ = self._tasks[task_id]
        self.spawn(task_id, when)
        # 立刻排下一次。
        #
        # 【必须在执行之前排】—— 放在执行之后的话，一个跑五分钟的任务
        # 会让下一次触发也推迟五分钟，而 cron 的语义是按绝对时间触发。
        self._schedule_next(task_id, cron, tz, base_ms=when)

    def spawn(self, task_id: str, scheduled_ms: int) -> None:
        """
        起一个后台任务执行。

        ## 为什么必须 create_task 而不是 await

        一次任务触发的是一轮完整的 agent 对话，可能跑几分钟。

        直接 `await self._execute_task(...)`——
        两个都设在 9:00 的任务，第一个跑 5 分钟，第二个就 9:05 才执行。
        而如果第一个卡住（LLM 请求 hang），后面【所有】任务永久不执行。
        """
        t = asyncio.create_task(
            self._run_guarded(task_id, scheduled_ms), name=f"cron-run-{task_id}"
        )
        self._inflight.add(t)
        # 【必须 discard】—— 不清理的话 _inflight 会无限增长，
        # 而 stop() 时会去 await 一堆早就完成的任务
        t.add_done_callback(self._inflight.discard)

    def cancel_running(self, task_id: str) -> int:
        """
        取消某个任务正在进行的执行。返回取消数量。

        ## 为什么需要

        删任务或停用任务时，正在跑的 asyncio task【不会自动停】——
        agent 对话会继续跑完（继续烧 LLM 配额、继续执行工具，
        而且是强制 auto 审批下的执行）。

        用户点"删除"却发现助手还在动，是很难理解的行为。

        靠 task 名字匹配而不是另建一个 dict：名字在 spawn 时就定好了
        （`cron-run-<task_id>`），多维护一个 dict 就多一处要同步，
        而 asyncio 已经把这个信息存着了。
        """
        prefix = f"cron-run-{task_id}"
        n = 0
        for t in list(self._inflight):
            if t.get_name() == prefix and not t.done():
                t.cancel()
                n += 1
        return n

    async def _run_guarded(self, task_id: str, scheduled_ms: int) -> None:
        """
        执行一个任务。异常在这里全部吞掉。

        ## 为什么异常必须在任务内部处理

        `create_task` 之后如果异常逃出去，会变成 unhandled task exception
        —— 只在 GC 时打一条警告，而调度器完全不知道执行失败了，
        cron_run 表里那条记录永远停在 running。
        """
        async with self._sem:
            started = time.monotonic()
            from app.modules.cron.runner import run_task

            try:
                await run_task(task_id, scheduled_ms)
            except asyncio.CancelledError:
                # 关闭时被取消 —— 把记录标成失败，
                # 否则它永远停在 running
                with contextlib.suppress(Exception):
                    await self._mark_failed(task_id, scheduled_ms, "服务关闭时中断")
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("cron_run_failed", task=task_id, err=str(e)[:200])
                with contextlib.suppress(Exception):
                    await self._mark_failed(task_id, scheduled_ms, str(e)[:400])
            finally:
                log.info(
                    "cron_run_done",
                    task=task_id,
                    ms=int((time.monotonic() - started) * 1000),
                )

    async def _mark_failed(self, task_id: str, scheduled_ms: int, why: str) -> None:
        from app.modules.cron import models as m
        from app.modules.cron import repo

        sm = self._sm()
        async with sm() as db:
            await repo.finish_run(
                db, task_id=task_id, scheduled_at=scheduled_ms, status=m.RUN_FAILED, detail=why
            )
            await repo.bump_fail(db, task_id)
            await db.commit()

    # ─────────────────────────── 查询 ───────────────────────────

    def next_fire_ms(self, task_id: str) -> int:
        e = self._tasks.get(task_id)
        return e[2] if e else 0

    @property
    def loaded_count(self) -> int:
        return len(self._tasks)

    @property
    def inflight_count(self) -> int:
        return len(self._inflight)


# 全局单例。
#
# 【本项目假设单实例】—— 多实例下两个调度器会各自触发同一个任务。
# 真正解决需要数据库层面抢锁（UPDATE ... WHERE last_fired_at < ?），
# 那是分布式调度的复杂度，个人项目不需要。
#
# 但这个假设必须写清楚，否则将来有人部署两份会发现任务重复执行，
# 而完全找不到原因。启动脚本的端口占用检查顺带保证了这一点。
scheduler = CronScheduler()


def dt_now(tz: str = "") -> dt.datetime:
    return dt.datetime.now(sch.get_tz(tz))
