"""
切会话时正在运行的 run 不该死锁。

## 这个 bug 的现场

用户在 auto 模式下对话（模型连续调多个工具），然后切到别的会话。回来后：
界面没有新输出、发消息报"连接中断：该会话已有正在进行的对话"、
永远无法继续 —— 只有重启进程才能恢复。

## 链条

  1. 前端切会话时 abort fetch（只停本地读取，服务端 run 继续 ——
     这是有意的，用户要的就是"让它自己跑"）
  2. Starlette 取消 SSE 响应任务，消费端不再调 bus.get()
  3. 队列填满（512 槽位，auto 模式一条长回复的 delta 就够）
  4. 下一个结构类事件（tool_start / tool_end / approval_required）
     执行 `await queue.put()` —— 永久阻塞
  5. produce() 的 finally 永不执行 → run_registry.unregister 不执行
     → task 永远不 done → active_run_of() 永远返回它 → 恒 409
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.core.events import Ev, EventBus

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / "frontend" / "src"
APP = ROOT / "backend" / "app"


class TestBusDetach:
    async def test_push_blocks_when_full_without_detach(self) -> None:
        """
        先证明这个阻塞是真的存在 —— 不然后面的修复无从谈起。
        """
        bus = EventBus(maxsize=2)
        await bus.push(Ev.TOOL_START, {"i": 1})
        await bus.push(Ev.TOOL_START, {"i": 2})

        # 第三个会阻塞。用极短超时确认它确实卡住了。
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                bus.push(Ev.TOOL_START, {"i": 3}), timeout=0.15
            )

    async def test_detach_makes_push_noop(self) -> None:
        bus = EventBus(maxsize=2)
        await bus.push(Ev.TOOL_START, {"i": 1})
        await bus.push(Ev.TOOL_START, {"i": 2})

        bus.detach()

        # 满队列 + detached，push 必须立刻返回而不是阻塞
        await asyncio.wait_for(bus.push(Ev.TOOL_START, {"i": 3}), timeout=0.5)
        await asyncio.wait_for(bus.push(Ev.TOOL_END, {"i": 4}), timeout=0.5)
        assert bus.detached is True

    async def test_detach_unblocks_already_waiting_producer(self) -> None:
        """
        【只置标志位是不够的】。

        已经卡在 `await put()` 上的协程不会因为标志位变化而醒来 ——
        它在等一个永远不会腾出的槽位。所以 detach 必须清空队列。
        """
        bus = EventBus(maxsize=1)
        await bus.push(Ev.TOOL_START, {"i": 1})

        waiting = asyncio.create_task(bus.push(Ev.TOOL_END, {"i": 2}))
        await asyncio.sleep(0.05)
        assert not waiting.done(), "前提不成立：它应该正卡着"

        bus.detach()
        # 不清空队列的话这里会超时
        await asyncio.wait_for(waiting, timeout=1.0)

    async def test_detach_is_idempotent(self) -> None:
        """SSE 生成器的 finally 和异常路径都可能调它。"""
        bus = EventBus(maxsize=2)
        bus.detach()
        bus.detach()
        await asyncio.wait_for(bus.push(Ev.TOOL_START, {}), timeout=0.5)

    async def test_bounded_put_prevents_deadlock_without_detach(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        兜底：即使 detach 因为某个异常路径没被调到，也不能永久卡住。

        无限等的代价是整个 run 死锁 + 会话锁死 + DB 连接泄漏（要重启进程）；
        丢一个事件的代价是前端某个工具卡片停在"执行中"（刷新即好）。
        """
        from app.core.config import settings

        monkeypatch.setattr(settings.agent, "event_put_timeout", 0.2)

        bus = EventBus(maxsize=1)
        await bus.push(Ev.TOOL_START, {"i": 1})

        # 没有 detach，但有超时兜底 —— 必须返回而不是永久阻塞
        await asyncio.wait_for(bus.push(Ev.TOOL_END, {"i": 2}), timeout=2.0)
        assert bus.dropped >= 1, "丢弃应该被计数，否则问题不可观测"

    async def test_delta_events_still_dropped_not_blocked(self) -> None:
        """
        增量事件（thinking / message）原本就是丢弃策略，这条锁住它没被改坏。
        丢几个字符用户几乎无感，而阻塞会拖垮整个 run。
        """
        bus = EventBus(maxsize=1)
        await bus.push(Ev.MESSAGE, {"delta": "a"})
        await asyncio.wait_for(bus.push(Ev.MESSAGE, {"delta": "b"}), timeout=0.5)
        assert bus.dropped == 1

    async def test_closed_bus_ignores_push(self) -> None:
        bus = EventBus(maxsize=4)
        await bus.close()
        await bus.push(Ev.TOOL_START, {})
        # close 放了哨兵 None，队列里只该有它
        assert await bus.get() is None


class TestSseGeneratorDetaches:
    def test_finally_calls_detach(self) -> None:
        """
        锁住"SSE 生成器退出时必须 detach"这件事。

        少了这一行，客户端断开后 run 会在队列填满时永久死锁 ——
        而那个症状（切回会话没输出、发消息恒 409）完全不指向
        "少调了一个 detach"。
        """
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "modules"
            / "agent"
            / "chat_service.py"
        ).read_text(encoding="utf-8")
        i = src.index("status = \"done\"")
        body = src[i : i + 2200]
        assert "bus.detach()" in body, "SSE 生成器退出时没有 detach"
        # detach 必须在 yield DONE 之前 —— GeneratorExit 下那个 yield
        # 可能抛 RuntimeError，之后的语句不保证执行
        assert body.index("bus.detach()") < body.index("Ev.DONE")


class TestFrontendGuardsAgainstStaleSession:
    """
    切走之后，旧会话的回调不能再动当前界面。

    这些回调是闭包，捕获的 sessionId 是发消息那一刻的值。用户切到别的
    会话后它们仍会触发（流还在读、或者刚断开）。
    """

    def _store(self) -> str:
        return (FRONT / "store" / "chat.ts").read_text(encoding="utf-8")

    def test_send_callbacks_check_session(self) -> None:
        src = self._store()
        i = src.index("async send(content, images, refs)")
        body = src[i : i + 3400]
        assert "isStillCurrent" in body, "回调没有校验会话"
        # 三个回调都要校验
        assert body.count("isStillCurrent()") >= 3

    def test_done_refetch_checks_session(self) -> None:
        """
        listMessages 的往返有几十到几百毫秒，用户完全可能在这期间切走。
        不校验的话旧会话的消息列表会覆盖新会话的。
        """
        src = self._store()
        i = src.index('case "done"')
        body = src[i : i + 1200]
        assert "get().sessionId !== sessionId" in body

    def test_open_session_discards_stale_response(self) -> None:
        """
        快速连点侧栏（A→B→C）时三个 openSession 并发飞行，
        Promise.all 的完成顺序不保证与发起顺序一致。
        """
        src = self._store()
        i = src.index("async openSession(sessionId)")
        body = src[i : i + 2600]
        assert "if (get().sessionId !== sessionId) return;" in body

    def test_open_session_suppresses_stale_banner(self) -> None:
        """过期的失败也不该弹 banner —— 用户已经在别的会话了。"""
        src = self._store()
        i = src.index("async openSession(sessionId)")
        body = src[i : i + 3400]
        j = body.index("catch (err)")
        assert "get().sessionId !== sessionId" in body[j : j + 300]


class TestBackgroundRunRecovery:
    """
    切回来要知道"还在后台跑"。

    不检测的话现象是：看到的是切走那一刻的历史，之后再没有新内容 ——
    而后台其实一直在写库。用户以为卡死了，发消息还会撞 409。
    """

    def test_endpoint_exists(self) -> None:
        src = (APP / "api" / "routes_chat.py").read_text(encoding="utf-8")
        assert "active-run" in src
        assert "run_registry.active_run_of" in src

    def test_store_watches_on_open(self) -> None:
        src = (FRONT / "store" / "chat.ts").read_text(encoding="utf-8")
        i = src.index("async openSession(sessionId)")
        body = src[i : i + 3000]
        assert "watchBackgroundRun" in body

    def test_watch_locks_input(self) -> None:
        """
        后台在跑时输入框要锁上 —— 不锁的话用户能输入，
        一发就 409，而那个错误信息说的是"连接中断"。
        """
        src = (FRONT / "store" / "chat.ts").read_text(encoding="utf-8")
        i = src.index("async watchBackgroundRun")
        body = src[i : i + 2600]
        assert "pending: true" in body

    def test_watch_unlocks_when_done(self) -> None:
        src = (FRONT / "store" / "chat.ts").read_text(encoding="utf-8")
        i = src.index("async watchBackgroundRun")
        body = src[i : i + 3000]
        assert "pending: false" in body

    def test_watch_stops_if_user_switches_away(self) -> None:
        """轮询循环必须在用户切走时退出，否则它会一直打后端。"""
        src = (FRONT / "store" / "chat.ts").read_text(encoding="utf-8")
        i = src.index("async watchBackgroundRun")
        body = src[i : i + 3000]
        assert "while (get().sessionId === sessionId)" in body


class TestConflictNotReportedAsNetworkError:
    """
    409 报成"连接中断"会让用户完全找不到真因。
    """

    def test_sse_separates_api_error(self) -> None:
        src = (FRONT / "lib" / "sse.ts").read_text(encoding="utf-8")
        assert "onApiError" in src
        assert "err instanceof ApiError" in src

    def test_store_removes_optimistic_message_on_conflict(self) -> None:
        """
        409 发生在后端落库【之前】，所以那条消息在库里不存在。
        留着它的话用户以为发出去了，而刷新之后它会凭空消失。
        """
        src = (FRONT / "store" / "chat.ts").read_text(encoding="utf-8")
        i = src.index("onApiError:")
        body = src[i : i + 900]
        assert "m.id !== tempId" in body

    def test_store_starts_watching_on_conflict(self) -> None:
        src = (FRONT / "store" / "chat.ts").read_text(encoding="utf-8")
        i = src.index("onApiError:")
        body = src[i : i + 900]
        assert "run_in_progress" in body
        assert "watchBackgroundRun" in body


class TestSkillIsADirectory:
    """
    技能不是单个 md 文件，它是一个目录。

    ## 真实的技能长什么样

    我自己的技能目录（~/.claude/skills）里：SKILL.md 是必须的，
    但一份像样的技能还带 references/*.md，也可能有 scripts/、assets/。
    frontmatter 里除了 name/description 还有 allowed-tools 之类。

    manage_asset 只能写 SKILL.md，所以光有它建不出完整技能。
    """

    def test_skills_dir_is_writable_whitelist(self) -> None:
        """
        skills/ 要在可写白名单里，
        这样模型能用已经熟练的 write_file / edit_file 组织附件 ——
        不需要为"写技能附件"再造一套 API。
        """
        src = (APP / "main.py").read_text(encoding="utf-8")
        assert "settings.skills_dir.resolve()" in src

    def test_builtins_upsert_not_all_or_nothing(self) -> None:
        """
        【必须逐条 upsert】。

        原来是"表为空才插"，那样新增内置项时已经在用的用户永远拿不到 ——
        他们表里有数据，整个分支被跳过。症状是"文档说能写 skills/，
        我这儿报路径不在白名单内"。
        """
        src = (APP / "main.py").read_text(encoding="utf-8")
        assert "existing_paths" in src
        assert "if str(p) in existing_paths" in src

    def test_hard_deny_still_wins(self) -> None:
        """
        白名单放开 skills/ 不等于放开敏感文件 ——
        硬拒止清单（.env / *.pem / credentials）优先级更高。
        """
        src = (APP / "modules" / "agent" / "pathguard.py").read_text(encoding="utf-8")
        i = src.index("def check(")
        body = src[i : i + 1200]
        assert body.index("_check_hard_deny") < body.index("_match_allowed")

    def test_loader_collects_nested_files(self) -> None:
        """
        加载器本来就支持多文件 —— 只是写不进去。
        这条锁住"reload 之后附件会进白名单"。
        """
        import tempfile

        from app.modules.skill.loader import load_index

        d = Path(tempfile.mkdtemp())
        sk = d / "多文件"
        (sk / "references").mkdir(parents=True)
        (sk / "scripts").mkdir()
        (sk / "SKILL.md").write_text(
            "---\nname: 多文件\ndescription: 当用户测试时使用\n---\n\n# 正文\n",
            encoding="utf-8",
        )
        (sk / "references" / "detail.md").write_text("x", encoding="utf-8")
        (sk / "scripts" / "run.py").write_text("print(1)", encoding="utf-8")

        idx = load_index(d)
        m = idx.skills["多文件"]
        assert "references/detail.md" in m.files
        assert "scripts/run.py" in m.files

    def test_tool_has_reload_action(self) -> None:
        """
        用 write_file 加了附件之后必须 reload，否则那个文件不在
        meta.files 白名单里，load_skill_file 会拒绝读它 ——
        而那个错误完全不指向"你需要 reload"。
        """
        from app.modules.agent.tools.asset import ManageAssetTool

        params = ManageAssetTool().parameters()
        assert "reload" in params["properties"]["action"]["enum"]

    def test_tool_description_explains_multifile(self) -> None:
        from app.modules.agent.tools.asset import ManageAssetTool

        d = ManageAssetTool().description
        assert "目录" in d
        assert "reload" in d

    async def test_reload_reports_diagnostics(self) -> None:
        """
        缺 description 的条目会被静默跳过。reload 必须把诊断报出来 ——
        不报的话模型以为建好了，而它根本没被加载。
        """
        import tempfile

        import app.core.config as cfg
        from app.modules.agent.tools.asset import ManageAssetTool
        from app.modules.agent.tools.base import ToolContext

        d = Path(tempfile.mkdtemp())
        (d / "skills").mkdir()
        bad = d / "skills" / "坏技能"
        bad.mkdir()
        (bad / "SKILL.md").write_text("---\nname: 坏技能\n---\n正文\n", encoding="utf-8")

        old = cfg.PROJECT_ROOT
        cfg.PROJECT_ROOT = d
        try:
            ctx = ToolContext(
                session_id="s", run_id="r", workspace=d,
                db=None, llm=None,  # type: ignore[arg-type]
            )
            r = await ManageAssetTool().run(ctx, action="reload", kind="skill")
            assert "description" in r.content or "有问题" in r.content
        finally:
            cfg.PROJECT_ROOT = old


class TestAssetToolGivesAbsolutePath:
    """
    【实测抓到的 bug】。

    模型建完 SKILL.md 之后想加 references/detail.md，用的是相对路径
    "skills/xxx/references/detail.md"。而 write_file 的相对路径基准是
    【工作区】—— 文件写到了 workspace/skills/xxx/ 去。

    最糟的部分是它【不报错】：workspace 本来就可写，写入成功、
    模型回复"已完成"，而 reload 之后附件不在 files 白名单里。
    用户看到的是"技能建好了但读不到附件"，而日志里一切正常。

    真实验证时的现场：模型返回"已创建"后，实际落盘却在
        workspace/skills/多文件技能-1074/references/detail.md
        而 skills/多文件技能-1074/ 里只有 SKILL.md。
    """

    async def _make(self, tmp: Path) -> tuple[object, object]:
        import app.core.config as cfg
        from app.modules.agent.tools.asset import ManageAssetTool
        from app.modules.agent.tools.base import ToolContext

        (tmp / "skills").mkdir(parents=True, exist_ok=True)
        cfg.PROJECT_ROOT = tmp
        ctx = ToolContext(
            session_id="s", run_id="r", workspace=tmp / "workspace",
            db=None, llm=None,  # type: ignore[arg-type]
        )
        return ManageAssetTool(), ctx

    async def test_create_returns_absolute_dir(self) -> None:
        import tempfile

        import app.core.config as cfg

        old = cfg.PROJECT_ROOT
        tmp = Path(tempfile.mkdtemp())
        try:
            tool, ctx = await self._make(tmp)
            r = await tool.run(  # type: ignore[attr-defined]
                ctx, action="create", kind="skill", name="绝对路径",
                description="当用户测试时使用", body="# 正文",
            )
            d = r.display.get("dir")
            assert d, "没有返回目录"
            assert Path(d).is_absolute(), f"不是绝对路径：{d}"
            # 正文里也要有 —— 模型读的是 content 不是 display
            assert str(d) in r.content
        finally:
            cfg.PROJECT_ROOT = old

    async def test_create_warns_about_relative_path(self) -> None:
        """
        光给路径不够，要说清"别用相对路径"——
        模型的默认习惯就是相对路径，而那条路不报错。
        """
        import tempfile

        import app.core.config as cfg

        old = cfg.PROJECT_ROOT
        tmp = Path(tempfile.mkdtemp())
        try:
            tool, ctx = await self._make(tmp)
            r = await tool.run(  # type: ignore[attr-defined]
                ctx, action="create", kind="skill", name="提醒",
                description="当用户测试时使用", body="# 正文",
            )
            assert "相对路径" in r.content
            assert "reload" in r.content
        finally:
            cfg.PROJECT_ROOT = old

    async def test_read_lists_existing_attachments(self) -> None:
        """模型要能看到"这个技能现在有哪些附件"。"""
        import tempfile

        import app.core.config as cfg

        old = cfg.PROJECT_ROOT
        tmp = Path(tempfile.mkdtemp())
        try:
            tool, ctx = await self._make(tmp)
            r = await tool.run(  # type: ignore[attr-defined]
                ctx, action="create", kind="skill", name="带附件",
                description="当用户测试时使用", body="# 正文",
            )
            sk = Path(r.display["dir"])
            (sk / "references").mkdir(parents=True, exist_ok=True)
            (sk / "references" / "detail.md").write_text("x", encoding="utf-8")
            await tool.run(ctx, action="reload", kind="skill")  # type: ignore[attr-defined]

            r2 = await tool.run(  # type: ignore[attr-defined]
                ctx, action="read", kind="skill", name="带附件"
            )
            assert "references/detail.md" in r2.content
            assert r2.display["files"] == ["references/detail.md"]
        finally:
            cfg.PROJECT_ROOT = old

    def test_description_states_the_rule(self) -> None:
        from app.modules.agent.tools.asset import ManageAssetTool

        d = ManageAssetTool().description
        assert "绝对路径" in d
        assert "工作区" in d, "没说清相对路径会落到哪"

    def test_list_extra_files_skips_junk(self) -> None:
        """技能目录里放了依赖时不该刷一屏。"""
        import tempfile

        import app.core.config as cfg
        from app.modules.skill import authoring

        old = cfg.PROJECT_ROOT
        tmp = Path(tempfile.mkdtemp())
        try:
            cfg.PROJECT_ROOT = tmp
            sk = tmp / "skills" / "有垃圾"
            (sk / "node_modules" / "pkg").mkdir(parents=True)
            (sk / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")
            (sk / "references").mkdir()
            (sk / "references" / "ok.md").write_text("y", encoding="utf-8")
            (sk / "SKILL.md").write_text(
                "---\nname: 有垃圾\ndescription: d\n---\n正文\n", encoding="utf-8"
            )
            got = authoring.list_extra_files(kind="skill", name="有垃圾")
            assert "references/ok.md" in got
            assert not any("node_modules" in g for g in got)
        finally:
            cfg.PROJECT_ROOT = old
