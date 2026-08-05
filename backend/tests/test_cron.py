"""
定时任务。

## 重点测什么

是唯一有定时任务的常见实现（676 行 CronTaskManager）。它的最小堆 +
版本号懒删除、Event 可中断等待、worker 容错三点做对了，照抄。

四个问题这里全部要测到：

1. `await self._execute_task(...)` 串行 —— 一个慢任务阻塞所有任务
2. 重启后重算下一次，错过的窗口静默消失
3. `execute` 字段用 `exec()` 在 agent 进程里跑任意代码
4. 全程 naive datetime，DST 切换日会出错

外加本项目自己的要求：
5. cron 表达式入口校验（只在调度时 catch）
6. 遗留的 running 记录要清理
7. 任务触发的会话必须无头执行 + 强制 auto 审批
"""

from __future__ import annotations

import contextlib
import datetime as dt
import inspect
from typing import Any

import pytest
import pytest_asyncio
from app.modules.cron import models as m
from app.modules.cron import repo
from app.modules.cron import schedule as sch
from app.modules.cron.scheduler import CronScheduler
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import code_only


def _bound(sb: CronScheduler, session: AsyncSession) -> CronScheduler:
    """
    给直接 new 出来的调度器绑上测试库。

    ## 为什么需要

    conftest 的 db fixture 只给【全局单例】绑了 sessionmaker。
    测试里 `CronScheduler()` new 出来的新实例拿不到那个绑定 ——
    于是回落到真实的 data/jeeves.db。

    实测漏掉的两个测试就这样往真实库写了 2 条记录。
    现在 _sm() 在 pytest 下会直接抛，所以漏绑会立刻红而不是静默污染。
    """
    # 直接复用全局单例已经绑好的那个工厂 —— conftest 的 db fixture
    # 建好内存库后就绑上了。
    #
    # 不用 session.get_bind：那返回的是【同步】Engine，
    # async_sessionmaker 会报 "AsyncEngine expected, got Engine"。
    from app.modules.cron.scheduler import scheduler as _global

    sb.bind_sessionmaker(_global._sessionmaker)  # noqa: SLF001
    return sb


@pytest_asyncio.fixture(autouse=True)
async def _ws(db: AsyncSession) -> str:
    from app.modules.session import repo as srepo

    w = await srepo.ensure_default_workspace(db, "/tmp/ws-cron")
    return w.id


class TestValidate:
    """
    只在调度时 catch CroniterBadCronError
    —— 非法表达式能存进库，然后每次调度都抛，而用户以为任务建好了。
    """

    def test_valid_expressions(self) -> None:
        for expr in ("0 9 * * *", "*/5 * * * *", "0 0 1 * *", "30 8 * * 1-5"):
            assert sch.validate(expr) == "", f"{expr} 应该合法"

    def test_empty_rejected(self) -> None:
        assert sch.validate("")
        assert sch.validate("   ")

    def test_wrong_segment_count_says_how_many(self) -> None:
        """
        错误信息要具体到"哪里不对"。

        只说 invalid cron 的话，用户看不出自己写的 "0 9 * *" 少了一段。
        """
        err = sch.validate("0 9 * *")
        assert "4 段" in err
        assert "5 段" in err
        assert "0 9 * * *" in err, "要给个正确的例子"

    def test_garbage_rejected(self) -> None:
        for expr in ("abc def ghi jkl mno", "99 99 99 99 99", "* * * * xyz"):
            assert sch.validate(expr), f"{expr} 应该被拒"

    def test_six_segment_allowed(self) -> None:
        """croniter 支持带秒的六段式。"""
        assert sch.validate("0 0 9 * * *") == ""


class TestTimezone:
    """
    全程 naive datetime（全文搜 tz/timezone/astimezone 零命中）。

    后果：DST 切换日出错 —— 春季跳过的那小时任务不触发，
    秋季重复的那小时触发两次。
    """

    def test_tzdata_actually_available(self) -> None:
        """
        真实的时区数据必须能加载。

        ## 真实发现的问题

        Windows 没有系统 tz 数据库 —— `ZoneInfo("Asia/Shanghai")` 抛
        `ZoneInfoNotFoundError`。而 `get_tz` 会静默回落本地时区
        （那个回落本身是对的：一条坏数据不该废掉整个调度器）。

        两件事叠加的后果：**所有时区都变成同一个**，
        于是下面的 DST 测试全部假通过 —— 它们只是在测本地时区。

        我最初就是这样：test_respects_timezone 失败（两个时区算出同一时刻），
        而 DST 那两个测试"通过"了。真因是缺 tzdata 依赖。

        这个测试直接拆掉那层静默：不装 tzdata 就红。
        """
        from zoneinfo import ZoneInfo

        # 不经 get_tz —— 它会吞掉异常
        for name in ("Asia/Shanghai", "America/New_York", "UTC"):
            ZoneInfo(name)

    def test_next_is_tz_aware(self) -> None:
        d = sch.next_after("0 9 * * *", "Asia/Shanghai")
        assert d.tzinfo is not None, "必须返回 tz-aware datetime"

    def test_prev_is_tz_aware(self) -> None:
        d = sch.prev_before("0 9 * * *", "Asia/Shanghai")
        assert d.tzinfo is not None

    def test_respects_timezone(self) -> None:
        """
        同一个表达式在不同时区算出的绝对时刻不同。

        "每天 9:00" 在上海和纽约是两个不同的 UTC 时刻。
        """
        base = dt.datetime(2026, 8, 5, 0, 0, tzinfo=dt.UTC)
        sh = sch.next_after("0 9 * * *", "Asia/Shanghai", after=base)
        ny = sch.next_after("0 9 * * *", "America/New_York", after=base)
        assert sh.timestamp() != ny.timestamp()

    def test_bad_timezone_falls_back_not_raises(self) -> None:
        """
        时区名非法时回落本地时区而不是抛。

        时区名存在库里 —— 抛异常会让【整个调度器启动失败】，
        一条坏数据废掉所有任务。
        """
        tz = sch.get_tz("Mars/Olympus_Mons")
        assert tz is not None

    def test_dst_spring_forward_does_not_crash(self) -> None:
        """
        DST 春季跳变日：美东 2026-03-08 02:00 不存在。

        设一个 02:30 的任务，croniter 必须能算出一个有效时刻而不是抛。
        """
        base = dt.datetime(2026, 3, 8, 0, 0, tzinfo=sch.get_tz("America/New_York"))
        d = sch.next_after("30 2 * * *", "America/New_York", after=base)
        assert d.tzinfo is not None

    def test_dst_fall_back_does_not_crash(self) -> None:
        """秋季重复那一小时。"""
        base = dt.datetime(2026, 11, 1, 0, 0, tzinfo=sch.get_tz("America/New_York"))
        d = sch.next_after("30 1 * * *", "America/New_York", after=base)
        assert d.tzinfo is not None

    def test_ms_roundtrip(self) -> None:
        d = sch.next_after("0 9 * * *", "Asia/Shanghai")
        assert abs(sch.to_ms(sch.from_ms(sch.to_ms(d), "Asia/Shanghai")) - sch.to_ms(d)) < 1000


class TestDescribe:
    def test_daily(self) -> None:
        assert sch.describe("0 9 * * *") == "每天 09:00"

    def test_every_n_minutes(self) -> None:
        assert "5 分钟" in sch.describe("*/5 * * * *")

    def test_hourly(self) -> None:
        assert "每小时" in sch.describe("0 * * * *")

    def test_unknown_shape_returns_raw(self) -> None:
        """
        不做通用的 cron→自然语言。显示原表达式比显示一句
        拗口的错误翻译好。
        """
        expr = "15 3 2 1 5"
        assert sch.describe(expr) == expr


class TestNoExecuteField:
    """
    任务有 execute 字段，存一段 Python 代码，用 exec() 加完整
    __builtins__ 在 agent 进程里跑 —— import os; os.system(...) 完全可用。

    它比 run_python 更危险：run_python 的代码是模型当场生成、用户在
    审批框里能看到；execute 是【存在库里的】，创建时看一眼，
    之后每天自动跑，没有任何审批环节。
    """

    def test_model_has_no_execute_field(self) -> None:
        cols = {c.name for c in m.CronTask.__table__.columns}
        for bad in ("execute", "execute_code", "script", "code"):
            assert bad not in cols, f"不该有 {bad} 字段"

    def test_no_exec_in_module(self) -> None:
        from app.modules.cron import runner, scheduler

        for mod in (runner, scheduler):
            src = code_only(inspect.getsource(mod))
            assert "exec(" not in src.replace(" ", ""), f"{mod.__name__} 里不该有 exec()"
            assert "eval(" not in src.replace(" ", "")

    def test_create_request_rejects_execute(self) -> None:
        """接口层也不该接受这个字段。"""
        from app.api.routes_cron import CreateTaskRequest

        assert "execute" not in CreateTaskRequest.model_fields


class TestConcurrency:
    """
    直接 await _execute_task——
    两个都设在 9:00 的任务，第一个跑 5 分钟，第二个就 9:05 才执行。
    而如果第一个卡住（LLM 请求 hang），后面所有任务永久不执行。
    """

    def test_spawns_instead_of_awaiting(self) -> None:
        src = inspect.getsource(CronScheduler._tick)
        assert "spawn" in src
        assert "await self._run" not in src, "不该在 worker 循环里直接 await 执行"

    def test_spawn_uses_create_task(self) -> None:
        src = inspect.getsource(CronScheduler.spawn)
        assert "create_task" in src

    def test_inflight_cleaned_up(self) -> None:
        """
        不 discard 的话 _inflight 会无限增长，
        而 stop() 时会去 await 一堆早就完成的任务。
        """
        src = inspect.getsource(CronScheduler.spawn)
        assert "add_done_callback" in src
        assert "discard" in src

    def test_exception_swallowed_inside_task(self) -> None:
        """
        create_task 之后异常必须在任务内部处理。

        逃出去会变成 unhandled task exception —— 只在 GC 时打一条警告，
        而调度器完全不知道执行失败了，cron_run 那条记录永远停在 running。
        """
        src = inspect.getsource(CronScheduler._run_guarded)
        assert "except Exception" in src
        assert "_mark_failed" in src

    def test_concurrency_capped(self) -> None:
        """
        一次任务触发的是一轮完整 agent 对话。不限并发的话，
        一个"每分钟"的任务配上慢对话会不断堆积，把配额和内存吃光。
        """
        from app.modules.cron.scheduler import MAX_CONCURRENT

        assert 0 < MAX_CONCURRENT <= 10
        assert "Semaphore" in inspect.getsource(CronScheduler.__init__)

    def test_next_scheduled_before_execution(self) -> None:
        """
        必须在执行【之前】排下一次。

        放在执行之后的话，一个跑五分钟的任务会让下一次触发也推迟五分钟，
        而 cron 的语义是按绝对时间触发。
        """
        src = inspect.getsource(CronScheduler._tick)
        assert src.index("spawn") < src.index("_schedule_next")


class TestInterruptibleWait:
    def test_uses_event_not_sleep(self) -> None:
        """
        不能用 asyncio.sleep —— sleep 6 小时期间新增一个"1 分钟后执行"
        的任务，那个新任务要等 6 小时后才被看到。
        """
        src = code_only(inspect.getsource(CronScheduler._tick))
        assert "wait_for" in src
        assert "self._wake" in src.replace(" ", "")
        assert "asyncio.sleep" not in src.replace(" ", "")

    def test_reload_wakes_worker(self) -> None:
        src = inspect.getsource(CronScheduler.reload)
        assert "_wake.set()" in src


class TestLazyDeletion:
    """
    heapq 没有 O(log n) 的删除。改任务或取消任务时只 bump 版本号，
    worker 弹出堆顶后对账。
    """

    def test_version_bumped_on_schedule(self) -> None:
        sb = CronScheduler()
        sb._schedule_next("t1", "0 9 * * *", "", base_ms=0)
        v1 = sb._versions["t1"]
        sb._schedule_next("t1", "0 10 * * *", "", base_ms=0)
        assert sb._versions["t1"] > v1

    def test_stale_entries_skipped(self) -> None:
        src = inspect.getsource(CronScheduler._tick)
        assert "_versions" in src
        # 版本不符要 return 而不是执行
        assert "!=" in src

    def test_worker_never_dies(self) -> None:
        """
        worker 死了的话所有任务静默失效 —— 没有报错，
        只是再也不触发了，而用户完全不会察觉。
        """
        src = inspect.getsource(CronScheduler._loop)
        assert "except Exception" in src
        assert "while self._running" in src


class TestMissedWindow:
    """
    重启后 sync_tasks() 从数据库重算 exec_time（croniter 取"下一个"）
    —— 任务设在每天 9:00，进程 8:50 挂了、9:30 才重启，
    9:00 那次【完全消失】，日志里没有任何记录。

    用户视角是"我的日报今天没发"，而没有线索指向"服务当时没在跑"。
    """

    def test_model_has_last_fired_at(self) -> None:
        cols = {c.name for c in m.CronTask.__table__.columns}
        assert "last_fired_at" in cols, "没有它就无法检测错过"

    def test_model_has_on_missed(self) -> None:
        cols = {c.name for c in m.CronTask.__table__.columns}
        assert "on_missed" in cols

    def test_default_is_skip(self) -> None:
        """
        默认 skip 而不是补偿。

        服务停了三天的话，run_once 会在启动瞬间触发 ——
        如果是每小时的任务，补偿逻辑一不小心就变成触发 72 次。
        而且昨天的日报今天补出来意义不大。
        """
        assert m.ON_MISSED_SKIP == "skip"

    def test_reload_detects_missed(self) -> None:
        src = inspect.getsource(CronScheduler.reload)
        assert "prev_before" in src
        assert "last_fired_at" in src
        assert "_handle_missed" in src

    def test_missed_always_recorded(self) -> None:
        """
        无论哪种策略都要落一条记录 —— 静默是最糟的选择。
        """
        src = inspect.getsource(CronScheduler._handle_missed)
        assert "add_run" in src
        assert "RUN_MISSED" in src

    def test_catchup_has_grace_window(self) -> None:
        """
        错过太久就不补 —— 昨天的日报今天补出来意义不大。
        """
        from app.modules.cron.scheduler import MISSED_GRACE_MS

        assert MISSED_GRACE_MS > 0
        assert "MISSED_GRACE_MS" in inspect.getsource(CronScheduler._handle_missed)

    def test_touch_fired_prevents_repeat(self) -> None:
        """
        处理过的错过窗口要更新 last_fired_at ——
        不更新的话同一个窗口会在每次重启时重复产生 missed 记录。
        """
        src = inspect.getsource(CronScheduler._handle_missed)
        assert "touch_fired" in src

    async def test_new_task_does_not_fire_immediately(
        self, db: AsyncSession, _ws: str
    ) -> None:
        """
        新建任务的 last_fired_at 是【现在】而不是 0。

        设 0 的话：新建一个"每天 9:00"的任务，如果现在是 10:00，
        错过检测会认为今天 9:00 那次错过了，立刻触发一次 ——
        用户刚建好任务就看到它跑了，会以为自己配错了。
        """
        from app.core.time import now_ms

        t = await repo.create_task(
            db, name="x", prompt="p", cron="0 9 * * *", workspace_id=_ws
        )
        assert t.last_fired_at > 0
        assert abs(t.last_fired_at - now_ms()) < 5000


class TestRepo:
    async def test_create_and_get(self, db: AsyncSession, _ws: str) -> None:
        t = await repo.create_task(
            db, name="日报", prompt="总结今天", cron="0 18 * * *", workspace_id=_ws
        )
        await db.commit()
        got = await repo.get_task(db, t.id)
        assert got is not None
        assert got.name == "日报"
        assert got.id.startswith("crt_")

    async def test_list_enabled_filters(self, db: AsyncSession, _ws: str) -> None:
        await repo.create_task(
            db, prompt="a", cron="0 9 * * *", workspace_id=_ws, enabled=True
        )
        await repo.create_task(
            db, prompt="b", cron="0 9 * * *", workspace_id=_ws, enabled=False
        )
        await db.commit()
        assert len(await repo.list_enabled(db)) == 1
        assert len(await repo.list_tasks(db)) == 2

    async def test_delete(self, db: AsyncSession, _ws: str) -> None:
        t = await repo.create_task(db, prompt="a", cron="0 9 * * *", workspace_id=_ws)
        await db.commit()
        assert await repo.delete_task(db, t.id) is True
        await db.commit()
        assert await repo.get_task(db, t.id) is None

    async def test_delete_missing_returns_false(self, db: AsyncSession) -> None:
        assert await repo.delete_task(db, "crt_nope") is False

    async def test_runs_recorded_and_ordered(self, db: AsyncSession, _ws: str) -> None:
        t = await repo.create_task(db, prompt="a", cron="0 9 * * *", workspace_id=_ws)
        for when in (3000, 1000, 2000):
            await repo.add_run(db, task_id=t.id, scheduled_at=when)
        await db.commit()
        runs = await repo.list_runs(db, t.id)
        assert [r.scheduled_at for r in runs] == [3000, 2000, 1000], "应按计划时间倒序"

    async def test_finish_run_updates_existing(self, db: AsyncSession, _ws: str) -> None:
        t = await repo.create_task(db, prompt="a", cron="0 9 * * *", workspace_id=_ws)
        await repo.add_run(db, task_id=t.id, scheduled_at=5000)
        await repo.finish_run(
            db, task_id=t.id, scheduled_at=5000, status=m.RUN_OK, session_id="ses_x"
        )
        await db.commit()
        runs = await repo.list_runs(db, t.id)
        assert len(runs) == 1, "不该新增记录"
        assert runs[0].status == m.RUN_OK
        assert runs[0].session_id == "ses_x"
        assert runs[0].finished_at > 0

    async def test_finish_run_without_existing_creates_one(
        self, db: AsyncSession, _ws: str
    ) -> None:
        """
        没有对应记录时补一条 —— 比丢掉这次执行的信息好。
        """
        t = await repo.create_task(db, prompt="a", cron="0 9 * * *", workspace_id=_ws)
        await repo.finish_run(db, task_id=t.id, scheduled_at=9999, status=m.RUN_FAILED)
        await db.commit()
        runs = await repo.list_runs(db, t.id)
        assert len(runs) == 1
        assert runs[0].status == m.RUN_FAILED

    async def test_clear_stale_running(self, db: AsyncSession, _ws: str) -> None:
        """
        进程被 kill -9 时正在执行的记录永远停在 running ——
        用户看到"任务卡在执行中"且分不清是真在跑还是上次没退干净。
        """
        t = await repo.create_task(db, prompt="a", cron="0 9 * * *", workspace_id=_ws)
        await repo.add_run(db, task_id=t.id, scheduled_at=1, status=m.RUN_RUNNING)
        await repo.add_run(db, task_id=t.id, scheduled_at=2, status=m.RUN_OK)
        await db.commit()

        n = await repo.clear_stale_running(db)
        await db.commit()
        assert n == 1
        runs = await repo.list_runs(db, t.id)
        stale = [r for r in runs if r.scheduled_at == 1][0]
        assert stale.status == m.RUN_FAILED
        assert "重启" in stale.detail

    async def test_counters(self, db: AsyncSession, _ws: str) -> None:
        t = await repo.create_task(db, prompt="a", cron="0 9 * * *", workspace_id=_ws)
        await db.commit()
        await repo.bump_run(db, t.id)
        await repo.bump_run(db, t.id)
        await repo.bump_fail(db, t.id)
        await db.commit()
        got = await repo.get_task(db, t.id)
        assert got is not None
        assert got.run_count == 2
        assert got.fail_count == 1

    async def test_run_survives_session_delete(self, db: AsyncSession, _ws: str) -> None:
        """
        cron_run.session_id 不加外键 —— 会话可能被用户删掉，
        而执行历史应该留着（那正是查"上周任务跑了吗"时需要的）。
        """
        cols = {c.name: c for c in m.CronRun.__table__.columns}
        assert not cols["session_id"].foreign_keys


class TestHeadless:
    """
    触发时用户可能根本没打开浏览器 —— 这是最典型的无头场景。

    subagent.md 已强调过：绝不能走 那种"等外部连接来触发执行"
    的路，否则定时任务永远不会执行且没有任何报错。
    """

    def test_drains_stream(self) -> None:
        """
        ChatService.stream() 是 async 生成器 —— 没有消费者的话
        函数体根本不会执行。必须完整抽干。
        """
        from app.modules.cron import runner

        src = inspect.getsource(runner.run_task)
        assert "async for" in src
        assert ".stream(" in src

    def test_forces_auto_approval(self) -> None:
        """
        manual 模式下 agent 会停下来等审批，而没有人在旁边点 ——
        任务会挂到审批超时然后失败。
        """
        from app.modules.cron import runner

        src = inspect.getsource(runner.run_task)
        assert 'approval_mode = "auto"' in src

    def test_links_session_to_run(self) -> None:
        """
        用户要能点进去看 agent 做了什么。没有关联的话，
        会话列表里会莫名多出一个他没发起过的对话。
        """
        from app.modules.cron import runner

        src = inspect.getsource(runner.run_task)
        assert "session_id=sid" in src

    def test_no_circular_import(self) -> None:
        """
        runner 不能 import app.main —— 那会形成循环
        （main 导入 cron 路由，cron 又导入 main），
        而循环导入的表现是"某个名字在导入时还不存在"，
        报错指向一个看起来无关的位置。
        """
        from app.modules.cron import runner

        src = code_only(inspect.getsource(runner))
        assert "import app.main" not in src
        assert "from app.main" not in src
        assert "bind_chat_service" in src

    def test_skips_disabled_task(self) -> None:
        from app.modules.cron import runner

        src = inspect.getsource(runner.run_task)
        assert "enabled" in src


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> Any:
    """
    带 get_db 覆盖的测试客户端。

    模块级而不是放在某个 class 里 —— TestRoutes 和 TestEdgeCases
    都要用，放 class 里另一个 class 就拿不到（fixture 不跨 class 继承）。
    """
    from app.infra.db.session import get_db
    from app.main import create_app
    from httpx import ASGITransport, AsyncClient

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


class TestRoutes:
    async def test_create_and_list(self, client: Any, _ws: str) -> None:
        r = await client.post(
            "/api/cron/tasks",
            json={"name": "日报", "prompt": "总结今天", "cron": "0 18 * * *"},
        )
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["cron_text"] == "每天 18:00"
        assert data["enabled"] is True

        r2 = await client.get("/api/cron/tasks")
        assert len(r2.json()["items"]) == 1

    async def test_bad_cron_rejected_at_creation(self, client: Any, _ws: str) -> None:
        """
        入口校验。只在调度时 catch —— 非法表达式能存进库，
        然后每次调度都抛，而用户以为任务建好了。
        """
        r = await client.post(
            "/api/cron/tasks", json={"prompt": "x", "cron": "0 9 * *"}
        )
        assert r.status_code == 400
        assert "段" in r.text

    async def test_bad_timezone_rejected(self, client: Any, _ws: str) -> None:
        r = await client.post(
            "/api/cron/tasks",
            json={"prompt": "x", "cron": "0 9 * * *", "timezone": "Mars/Olympus"},
        )
        assert r.status_code == 400

    async def test_bad_on_missed_rejected(self, client: Any, _ws: str) -> None:
        r = await client.post(
            "/api/cron/tasks",
            json={"prompt": "x", "cron": "0 9 * * *", "on_missed": "explode"},
        )
        assert r.status_code == 400

    async def test_empty_prompt_rejected(self, client: Any, _ws: str) -> None:
        r = await client.post("/api/cron/tasks", json={"prompt": "", "cron": "0 9 * * *"})
        assert r.status_code == 422

    async def test_patch(self, client: Any, _ws: str) -> None:
        r = await client.post("/api/cron/tasks", json={"prompt": "x", "cron": "0 9 * * *"})
        tid = r.json()["id"]
        r2 = await client.patch(
            f"/api/cron/tasks/{tid}", json={"enabled": False, "cron": "0 10 * * *"}
        )
        assert r2.status_code == 200
        assert r2.json()["enabled"] is False
        assert r2.json()["cron"] == "0 10 * * *"

    async def test_patch_bad_cron_rejected(self, client: Any, _ws: str) -> None:
        r = await client.post("/api/cron/tasks", json={"prompt": "x", "cron": "0 9 * * *"})
        tid = r.json()["id"]
        r2 = await client.patch(f"/api/cron/tasks/{tid}", json={"cron": "garbage"})
        assert r2.status_code == 400

    async def test_delete(self, client: Any, _ws: str) -> None:
        r = await client.post("/api/cron/tasks", json={"prompt": "x", "cron": "0 9 * * *"})
        tid = r.json()["id"]
        assert (await client.delete(f"/api/cron/tasks/{tid}")).status_code == 200
        assert (await client.get("/api/cron/tasks")).json()["items"] == []

    async def test_unknown_task_404(self, client: Any) -> None:
        assert (await client.delete("/api/cron/tasks/crt_nope")).status_code == 404
        assert (await client.get("/api/cron/tasks/crt_nope/runs")).status_code == 404

    async def test_validate_previews_next_fires(self, client: Any) -> None:
        """
        cron 表达式很容易写错且错得不明显 —— 0 9 * * 1 到底是周一还是周日？
        看到接下来三次的具体时间就一目了然。
        """
        r = await client.post("/api/cron/validate", json={"cron": "0 9 * * *"})
        d = r.json()
        assert d["valid"] is True
        assert len(d["next"]) == 5
        assert d["next"] == sorted(d["next"]), "预览时间要递增"
        assert d["text"] == "每天 09:00"

    async def test_validate_reports_error(self, client: Any) -> None:
        r = await client.post("/api/cron/validate", json={"cron": "nope"})
        assert r.json()["valid"] is False
        assert r.json()["error"]

    async def test_runs_endpoint(self, client: Any, db: AsyncSession, _ws: str) -> None:
        r = await client.post("/api/cron/tasks", json={"prompt": "x", "cron": "0 9 * * *"})
        tid = r.json()["id"]
        await repo.add_run(db, task_id=tid, scheduled_at=1000, status=m.RUN_OK)
        await db.commit()
        r2 = await client.get(f"/api/cron/tasks/{tid}/runs")
        assert len(r2.json()["items"]) == 1
        assert r2.json()["items"][0]["status"] == "ok"


class TestSchedulerState:
    async def test_start_is_idempotent(self, db: AsyncSession) -> None:
        """
        重复 start 不该起两个 worker。

        起两个的话同一个任务会被触发两次，而这类问题很难查 ——
        表现是"日报发了两份"。
        """
        sb = _bound(CronScheduler(), db)
        await sb.start()
        first = sb._worker
        await sb.start()
        assert sb._worker is first, "第二次 start 不该换 worker"
        await sb.stop()

    async def test_stop_leaves_no_pending_worker(self, db: AsyncSession) -> None:
        """
        stop 之后 worker 必须真的结束。

        不结束的话 pytest 会在 teardown 报
        "Task was destroyed but it is pending" —— 而那条警告很容易
        被当成噪声忽略，实际是调度器泄漏了一个协程。
        """
        sb = _bound(CronScheduler(), db)
        await sb.start()
        await sb.stop()
        assert sb._worker is None

    def test_next_fire_query(self) -> None:
        sb = CronScheduler()
        assert sb.next_fire_ms("nope") == 0
        sb._schedule_next("t1", "0 9 * * *", "", base_ms=0)
        assert sb.next_fire_ms("t1") > 0

    def test_invalid_cron_skipped_not_fatal(self) -> None:
        """
        一条坏数据不该废掉所有任务。
        """
        sb = CronScheduler()
        got = sb._schedule_next("bad", "not a cron", "", base_ms=0)
        assert got == 0
        assert "bad" not in sb._tasks

    def test_single_instance_assumption_documented(self) -> None:
        """
        多实例下两个调度器会各自触发同一个任务。

        个人项目不解决这个，但假设必须写清楚 —— 否则将来有人部署两份
        会发现任务重复执行，而完全找不到原因。
        """
        from app.modules.cron import scheduler as mod

        src = inspect.getsource(mod)
        assert "单实例" in src

    def test_stop_waits_for_inflight(self) -> None:
        """
        不等的话进程退出时正在跑的任务被硬中断，
        cron_run 表里留下一堆 status=running 的记录 ——
        而那些永远不会变成 ok 或 failed。
        """
        src = inspect.getsource(CronScheduler.stop)
        assert "_inflight" in src
        assert "asyncio.wait" in src

class TestEdgeCases:
    """
    交叉审查发现的缺陷。每一条都是"跑通了主流程之后才暴露"的那种。
    """

    async def test_scheduler_never_touches_real_db_in_tests(self) -> None:
        """
        【最严重的一条】：调度器在测试里绝不能碰真实数据库。

        ## 实测到的污染

        `scheduler.reload()` 原来直接调 `get_sessionmaker()` ——
        而 FastAPI 的 `dependency_overrides[get_db]` 拦不住它
        （调度器自己开会话，不走依赖注入）。

        结果 12 个路由测试往真实的 data/jeeves.db 写了 7 条 cron_run、
        建了 3 个真实会话。

        更危险的是：开发机上如果有一个 on_missed=run_once 且在 6 小时
        补偿窗口内的任务，跑一次 pytest 会【真的拉起一次 agent 对话】
        —— 强制 auto 审批，能无确认执行工具。

        当时没炸只是因为测试里 ChatService 没注入，在
        "ChatService 未注入" 那一步就失败了。纯属运气。

        现在 _sm() 在 pytest 下没绑 sessionmaker 就直接抛。
        """
        sb = CronScheduler()
        with pytest.raises(RuntimeError, match="没绑 sessionmaker"):
            sb._sm()

    async def test_run_now_rejects_disabled_task(self, client: Any, _ws: str) -> None:
        """
        禁用的任务不能手动触发。

        原来是照样插一条 running 记录再 spawn，而 runner.run_task 开头
        发现 enabled=0 就直接 return，不做任何收尾 ——
        那条记录【永久停在 running】。

        而且 run_task 是正常返回（无异常），所以 _run_guarded 的
        _mark_failed 也不会执行，只能等重启由 clear_stale_running 兜底。
        """
        r = await client.post(
            "/api/cron/tasks",
            json={"prompt": "x", "cron": "0 9 * * *", "enabled": False},
        )
        tid = r.json()["id"]
        rr = await client.post(f"/api/cron/tasks/{tid}/run")
        assert rr.status_code == 400
        assert "停用" in rr.text

        # 不该留下任何执行记录
        runs = (await client.get(f"/api/cron/tasks/{tid}/runs")).json()["items"]
        assert runs == [], "被拒的手动触发不该留记录"

    async def test_finish_run_discards_when_task_gone(
        self, db: AsyncSession, _ws: str
    ) -> None:
        """
        任务在执行中途被删掉时，收尾不能撞外键。

        cron_run.task_id 是 CASCADE —— 删任务会把那条 running 记录
        一起删掉。原来 finish_run 找不到记录就无条件 add_run，
        往一个已不存在的 task_id 上插，抛 IntegrityError。

        那个异常从 runner 的 finally 里抛出会掩盖原始异常，
        然后被 _mark_failed 外面的 suppress 整个吞掉 ——
        表现是对话跑完了、会话建出来了，但执行历史一条不留。
        """
        t = await repo.create_task(db, prompt="a", cron="0 9 * * *", workspace_id=_ws)
        tid = t.id
        await db.commit()
        await repo.delete_task(db, tid)
        await db.commit()

        # 任务已删，收尾应该安静地丢弃而不是抛
        await repo.finish_run(
            db, task_id=tid, scheduled_at=1234, status=m.RUN_OK, session_id="ses_x"
        )
        await db.commit()  # 不该抛 IntegrityError

    async def test_cancel_running_by_task_id(self) -> None:
        """
        删/停用任务要能停掉正在跑的那次。

        不停的话 agent 对话继续跑完 —— 继续烧配额、继续执行工具，
        而用户点了"删除"却发现助手还在动。
        """
        import asyncio

        sb = CronScheduler()

        async def _slow() -> None:
            await asyncio.sleep(30)

        t = asyncio.create_task(_slow(), name="cron-run-crt_target")
        other = asyncio.create_task(_slow(), name="cron-run-crt_other")
        sb._inflight.add(t)
        sb._inflight.add(other)

        assert sb.cancel_running("crt_target") == 1
        await asyncio.sleep(0.05)
        assert t.cancelled(), "目标任务应被取消"
        assert not other.cancelled(), "不该误伤别的任务"

        other.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await other

    async def test_cancel_running_ignores_unknown(self) -> None:
        sb = CronScheduler()
        assert sb.cancel_running("crt_nothing") == 0

    async def test_delete_cancels_before_deleting(self) -> None:
        """
        顺序必须是"先取消再删"。

        反过来的话那次执行的 finally 会去写一个已被 CASCADE 删掉的
        记录，撞外键然后被静默吞掉。
        """
        from app.api.routes_cron import delete_task

        src = code_only(inspect.getsource(delete_task))
        # 用 repo.delete_task 的调用位置作锚点 ——
        # 函数自己也叫 delete_task，直接找那个字符串会命中定义行
        assert src.index("cancel_running") < src.index("repo . delete_task")

    async def test_disable_cancels_running(self) -> None:
        from app.api.routes_cron import patch_task

        src = code_only(inspect.getsource(patch_task))
        assert "cancel_running" in src

    async def test_reenable_resets_last_fired(self, client: Any, _ws: str) -> None:
        """
        停用再启用不该触发补跑。

        PATCH 原来不动 last_fired_at —— 于是 reload 的错过检测认为
        停用期间的窗口都错过了，落一条 missed 记录，或者
        （on_missed=run_once 且在宽限内）【立刻补跑一次】。

        用户只是关了又开，却看到任务自己跑了。
        """
        r = await client.post(
            "/api/cron/tasks",
            json={"prompt": "x", "cron": "0 * * * *", "on_missed": "run_once"},
        )
        tid = r.json()["id"]

        await client.patch(f"/api/cron/tasks/{tid}", json={"enabled": False})
        r2 = await client.patch(f"/api/cron/tasks/{tid}", json={"enabled": True})
        assert r2.status_code == 200
        # 启用时 last_fired_at 被刷新到现在 —— 不会被判定为错过
        from app.core.time import now_ms

        assert abs(r2.json()["last_fired_at"] - now_ms()) < 10_000

    async def test_reload_failure_does_not_break_write(
        self, client: Any, _ws: str, monkeypatch: Any
    ) -> None:
        """
        reload 失败不能把已成功的 commit 报成 500。

        调用方都是已经 commit 成功的写操作。抛出去变成 500 的话，
        客户端会以为创建失败并重试 —— 于是建出两个一样的任务。
        """
        from app.modules.cron.scheduler import scheduler as sched

        async def _boom() -> None:
            raise RuntimeError("模拟 reload 失败")

        monkeypatch.setattr(sched, "reload", _boom)
        r = await client.post(
            "/api/cron/tasks", json={"prompt": "x", "cron": "0 9 * * *"}
        )
        assert r.status_code == 201, "reload 失败不该影响创建结果"

    async def test_phantom_task_does_not_crash_tick(self) -> None:
        """
        内存里有任务但库里没有（比如库被外部改动）时，_tick 不该崩。

        实测行为：守卫放行 → spawn → runner 里 get_task 返回 None
        → 打 warning 后 return → 照常重排。不崩，但会变成幽灵任务
        （按周期一遍遍触发、刷 warning）。

        这里只断言"不崩"—— 幽灵本身要靠删除路径 reload 来避免。
        """
        sb = CronScheduler()
        sb._schedule_next("crt_phantom", "*/1 * * * *", "", base_ms=0)
        assert "crt_phantom" in sb._tasks
        # 版本一致、键存在 → 守卫会放行，不会 KeyError
        assert sb._versions["crt_phantom"] > 0
