"""
追踪落库的测试。

## 常见实现在这块的状况

| | 落库式追踪 | 脱敏 | TTL | 成本归集 |
| --- | --- | --- | --- | --- |
| | 塞进 messages.info JSON 列 | **无** | **无** | 前端内存累加 |
| | **零持久化**（只有日志） | **无** | **无** | 未实现 |
| 同类实现 | 设计写了两份文档，**代码一行未实现** | **无** | **无** | 展示层重建 |

所以这块几乎全是自己设计。测试重点在两条铁律：

1. **追踪写入永不影响主流程**（反例：写失败 raise HTTPException，
   整段对话被打断）
2. **密钥必须脱敏**（常见实现没做，甚至明文过 IPC）
"""

import asyncio
from typing import Any

import pytest
import pytest_asyncio
from app.modules.trace.recorder import compute_cost
from app.modules.trace.redact import ATTR_WHITELIST, redact, redact_attrs
from app.modules.trace.writer import (
    PREVIEW_MAX_BYTES,
    PREVIEW_MAX_LINES,
    QUEUE_MAX,
    RunRecord,
    SpanRecord,
    TraceWriter,
    make_preview,
)
from sqlalchemy.ext.asyncio import AsyncSession


@pytest_asyncio.fixture
async def trace_session(db: AsyncSession) -> str:
    """
    一个真实会话的 id。

    run/span 有 ON DELETE CASCADE 外键指向 session，所以不能再用
    编造的 session_id —— 会被外键拒绝。

    这个约束是有意加的：追踪记录跟着会话走，删会话时一起清掉。
    之前没有外键，删掉的会话留下一堆孤儿 span 永远占着磁盘。
    """
    from app.modules.session import repo as srepo

    ws = await srepo.ensure_default_workspace(db, "/tmp/trace-test")
    s = await srepo.create_session(db, workspace_id=ws.id, title="追踪测试")
    return s.id


class TestRedaction:
    """常见实现没有脱敏。span 会捕获工具参数，泄漏是必然而非可能。"""

    @pytest.mark.parametrize(
        "raw",
        [
            "sk-abc123def456ghi789",
            "sk-proj-abcdefgh12345678",
            "sk-ant-api03-xxxxxxxxxxxx",
            "AIzaSyABCDEFGHIJKLMNOP",
            "ghp_abcdefghijklmnopqrst",
            "AKIAIOSFODNN7EXAMPLE",
        ],
    )
    def test_api_keys_masked(self, raw: str) -> None:
        out = redact(f"用的 key 是 {raw} 请注意")
        assert raw not in out, f"{raw} 没被脱敏"
        assert "***" in out

    def test_prefix_kept_for_identification(self) -> None:
        """
        保留前缀，好让人判断"是哪一类 key"而不暴露它。

        全遮掉的话排查时无法确认"用的是不是预期的那把钥匙"。
        """
        out = redact("sk-abc123def456ghi789")
        assert out.startswith("sk-")
        assert "abc123def456" not in out

    def test_bearer_token(self) -> None:
        out = redact("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6")
        assert "eyJhbGci" not in out

    def test_key_value_forms(self) -> None:
        cases = [
            "api_key=supersecret123",
            "API_KEY: supersecret123",
            'password="supersecret123"',
            "token=supersecret123",
            "access_key = supersecret123",
        ]
        for c in cases:
            out = redact(c)
            assert "supersecret123" not in out, f"没脱敏：{c}"

    def test_json_form(self) -> None:
        out = redact('{"api_key": "sk-live-abcdefgh", "model": "gpt-4"}')
        assert "sk-live-abcdefgh" not in out
        # 非敏感字段要保留 —— 全遮掉的话追踪就没用了
        assert "gpt-4" in out

    def test_db_connection_string(self) -> None:
        out = redact("postgresql://admin:hunter2pass@db.host:5432/mydb")
        assert "hunter2pass" not in out
        # 主机名保留，它是排查连接问题需要的
        assert "db.host" in out

    def test_normal_text_untouched(self) -> None:
        """
        脱敏不能过度。把正常内容也遮掉的话追踪就失去意义了。
        """
        text = "读取 config.py 第 42 行，发现 timeout=30 设置得太小"
        assert redact(text) == text

    def test_empty_and_none_safe(self) -> None:
        assert redact("") == ""

    def test_attrs_whitelist(self) -> None:
        """
        attributes 走白名单而非黑名单 —— 新增字段默认不存，
        比默认存了才发现泄漏好。
        """
        out = redact_attrs(
            {
                "tool_name": "run_shell",
                "api_key": "sk-leaked123456",
                "random_field": "whatever",
            }
        )
        assert "tool_name" in out
        assert "api_key" not in out
        assert "random_field" not in out

    def test_whitelist_has_no_secret_fields(self) -> None:
        for k in ATTR_WHITELIST:
            assert not any(
                bad in k.lower() for bad in ("key", "secret", "password", "token")
            ), f"白名单里有可疑字段：{k}"


class TestPreview:
    def test_redact_before_truncate(self) -> None:
        """
        必须【先脱敏再截断】。

        顺序反了的话，截断点可能把密钥切成两半，前半段仍是明文的一部分，
        而正则再也匹配不到它。
        """
        # 把 key 放在接近截断边界的位置
        pad = "x" * (PREVIEW_MAX_BYTES - 10)
        text = pad + "sk-abcdefgh12345678"
        prev, truncated, _ = make_preview(text)
        assert truncated is True
        # 截断后残留的部分不能包含 key 的明文片段
        assert "abcdefgh" not in prev

    def test_byte_limit(self) -> None:
        prev, truncated, raw = make_preview("a" * (PREVIEW_MAX_BYTES + 500))
        assert truncated is True
        assert len(prev.encode("utf-8")) <= PREVIEW_MAX_BYTES
        assert raw == PREVIEW_MAX_BYTES + 500

    def test_line_limit(self) -> None:
        """双阈值：字节和行数，先到先算。"""
        prev, truncated, _ = make_preview("\n".join(["line"] * (PREVIEW_MAX_LINES + 50)))
        assert truncated is True
        assert prev.count("\n") < PREVIEW_MAX_LINES + 50

    def test_original_size_recorded(self) -> None:
        """
        记原始字节数。只存截断后内容的话，读的人无法判断
        "这就是全部"还是"还有更多"。
        """
        _prev, _t, raw = make_preview("中文内容")
        assert raw == len("中文内容".encode())

    def test_no_broken_utf8(self) -> None:
        cn = "中" * (PREVIEW_MAX_BYTES // 3 + 100)
        prev, _t, _r = make_preview(cn)
        prev.encode("utf-8").decode("utf-8")  # 不抛就是没切坏

    def test_short_text_not_truncated(self) -> None:
        prev, truncated, raw = make_preview("短内容")
        assert prev == "短内容"
        assert truncated is False


class TestCost:
    def test_basic(self) -> None:
        c = compute_cost(
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            price_in_per_1m=0.5,
            price_out_per_1m=1.5,
        )
        assert c == pytest.approx(2.0)

    def test_missing_price_returns_zero(self) -> None:
        """
        单价 NULL 表示"没配价"，不是免费。

        返回 0 是无奈之选，但单价必须一起存进 span —— 报表能靠
        price 字段是否为 NULL 区分"零成本"和"没配价"。
        """
        assert compute_cost(
            input_tokens=1000,
            output_tokens=1000,
            price_in_per_1m=None,
            price_out_per_1m=None,
        ) == 0.0

    def test_partial_price(self) -> None:
        c = compute_cost(
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            price_in_per_1m=1.0,
            price_out_per_1m=None,
        )
        assert c == pytest.approx(1.0)

    def test_zero_tokens(self) -> None:
        assert compute_cost(
            input_tokens=0, output_tokens=0, price_in_per_1m=1.0, price_out_per_1m=1.0
        ) == 0.0


class _FakeSession:
    """可控失败的假 session。"""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.added: list[Any] = []
        self.committed = 0

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def execute(self, *a: Any, **kw: Any) -> Any:
        if self.fail:
            raise RuntimeError("数据库挂了")

        class _R:
            def scalar_one_or_none(self) -> None:
                return None

        return _R()

    async def commit(self) -> None:
        if self.fail:
            raise RuntimeError("commit 失败")
        self.committed += 1


class TestWriterNeverBreaksMainFlow:
    """
    第一原则：追踪写入永不影响主流程。

    一条值得记住的原则：`Observability must never affect 同类实现 execution.`
    反例：写失败 raise HTTPException，
    整段对话被打断 —— 因为记日志失败。
    """

    def _mk(self, fail: bool = False) -> tuple[TraceWriter, list[_FakeSession]]:
        made: list[_FakeSession] = []

        def sm() -> _FakeSession:
            s = _FakeSession(fail=fail)
            made.append(s)
            return s

        return TraceWriter(sm), made  # type: ignore[arg-type]

    def _span(self, session_id: str, sid: str = "sp1") -> SpanRecord:
        return SpanRecord(
            id=sid,
            run_id="r1",
            session_id=session_id,
            kind="tool",
            name="read_file",
            started_at=1,
        )

    async def test_submit_is_sync_and_nonblocking(self) -> None:
        """
        submit 不是 async。调用方不需要 await，也就不可能因为追踪而变慢。
        """
        w, _ = self._mk()
        # 不 await 也能调用
        w.submit(self._span(trace_session))
        assert w._q.qsize() == 1

    async def test_db_failure_does_not_raise(self) -> None:
        """写库失败只记 warning，绝不上抛。"""
        w, _ = self._mk(fail=True)
        w.start()
        w.submit(self._span(trace_session))
        await asyncio.sleep(0.05)
        assert w.stats.failed >= 1
        assert w.stats.written == 0
        # consumer 还活着 —— 一次失败不该让整个写入器停摆
        assert w._task is not None and not w._task.done()
        await w.stop()

    async def test_recovers_after_failure(self) -> None:
        """一条写失败后，后续的还要能写成功。"""
        made: list[_FakeSession] = []
        state = {"fail": True}

        def sm() -> _FakeSession:
            s = _FakeSession(fail=state["fail"])
            made.append(s)
            return s

        w = TraceWriter(sm)  # type: ignore[arg-type]
        w.start()
        w.submit(self._span(trace_session, "bad"))
        await asyncio.sleep(0.05)
        state["fail"] = False
        w.submit(self._span(trace_session, "good"))
        await asyncio.sleep(0.05)
        assert w.stats.failed >= 1
        assert w.stats.written >= 1
        await w.stop()

    async def test_queue_full_drops_oldest_not_raises(self) -> None:
        """
        队列满时丢最老的并计数，不阻塞、不抛错。

        新的 span 比旧的有用 —— 排查问题时看的是最近发生的。
        """
        w, _ = self._mk()
        # 不 start consumer，让队列填满
        for i in range(QUEUE_MAX + 20):
            w.submit(self._span(f"sp{i}"))
        assert w.stats.dropped >= 20
        assert w._q.qsize() <= QUEUE_MAX

    async def test_uses_independent_session(self) -> None:
        """
        用独立 session。复用请求级 session 的话，追踪写入失败会让业务
        事务一起回滚 —— 那就成了"记日志失败导致用户消息丢了"。
        """
        w, made = self._mk()
        w.start()
        w.submit(self._span(trace_session, "a"))
        w.submit(self._span(trace_session, "b"))
        await asyncio.sleep(0.05)
        # 每条一个新 session
        assert len(made) >= 2
        await w.stop()

    async def test_stop_flushes_pending(self) -> None:
        """
        停止前排空队列。不排空的话最后几条 span 会丢，
        而那几条往往正是排查崩溃时最需要的。
        """
        w, _ = self._mk()
        w.start()
        for i in range(5):
            w.submit(self._span(f"sp{i}"))
        await w.stop()
        assert w.stats.written == 5

    async def test_module_level_submit_without_init_is_safe(self) -> None:
        """
        writer 未初始化时静默丢弃 —— 测试和脚本不该被迫初始化追踪。
        """
        from app.modules.trace import writer as mod

        saved = mod._writer
        mod._writer = None
        try:
            mod.submit(self._span(trace_session))  # 不抛就对了
        finally:
            mod._writer = saved


class TestSpanTreeAndCleanup:
    """走真实库，验证树还原和清理。"""

    async def _add_span(
        self,
        db: Any,
        sid: str,
        *,
        session_id: str,
        run_id: str = "r1",
        parent: str | None = None,
        depth: int = 0,
        kind: str = "tool",
        started: int = 1000,
        created: int | None = None,
    ) -> None:
        from app.modules.trace.models import Span

        db.add(
            Span(
                id=sid,
                run_id=run_id,
                session_id=session_id,
                parent_span_id=parent,
                depth=depth,
                kind=kind,
                name=sid,
                started_at=started,
                created_at=created if created is not None else 9_000_000_000_000,
                updated_at=created if created is not None else 9_000_000_000_000,
            )
        )

    async def test_tree_reconstruction(self, db: Any, trace_session: str) -> None:
        from app.modules.trace import service

        # agent(root) → llm, tool → 子 tool
        await self._add_span(db, "root", session_id=trace_session, kind="agent", started=1)
        await self._add_span(db, "llm1", session_id=trace_session, parent="root", depth=1, kind="llm", started=2)
        await self._add_span(db, "tool1", session_id=trace_session, parent="root", depth=1, started=3)
        await self._add_span(db, "sub1", session_id=trace_session, parent="tool1", depth=2, started=4)
        await db.commit()

        roots = await service.get_span_tree(db, "r1")
        assert len(roots) == 1
        assert roots[0].span.id == "root"
        assert {c.span.id for c in roots[0].children} == {"llm1", "tool1"}
        tool_node = next(c for c in roots[0].children if c.span.id == "tool1")
        assert [c.span.id for c in tool_node.children] == ["sub1"]

    async def test_orphan_span_attached_to_root_not_dropped(
        self, db: Any, trace_session: str
    ) -> None:
        """
        父 span 不在本 run 里时挂到根上，不丢掉。

        丢掉会让树缺一整枝，看起来像那部分没执行过 ——
        而实际上它执行了，只是父引用跨了 run。
        """
        from app.modules.trace import service

        await self._add_span(db, "orphan", session_id=trace_session, parent="nonexistent", depth=3)
        await db.commit()
        roots = await service.get_span_tree(db, "r1")
        assert len(roots) == 1
        assert roots[0].span.id == "orphan"

    async def test_tree_to_dict_exposes_truncation(
        self, db: Any, trace_session: str
    ) -> None:
        """
        截断事实要出现在 API 响应里 —— 否则前端无法提示"内容不全"。
        """
        from app.modules.trace import service
        from app.modules.trace.models import Span

        db.add(
            Span(
                id="sp1",
                run_id="r1",
                session_id=trace_session,
                kind="tool",
                name="x",
                started_at=1,
                output_preview="截断后的",
                output_truncated=True,
                output_bytes=99999,
            )
        )
        await db.commit()
        tree = await service.get_span_tree(db, "r1")
        d = service.tree_to_dict(tree)
        assert d[0]["output_truncated"] is True
        assert d[0]["output_bytes"] == 99999

    async def test_has_price_distinguishes_zero_from_unpriced(
        self, db: Any, trace_session: str
    ) -> None:
        """
        has_price 让前端能区分"零成本"和"没配价"。

        只存 total_tokens，成本永远算不出 —— 这个错误不可逆，
        因为原始数据没留下。
        """
        from app.modules.trace import service
        from app.modules.trace.models import Span

        db.add(
            Span(
                id="priced",
                run_id="r1",
                session_id=trace_session,
                kind="llm",
                name="m",
                started_at=1,
                price_in_per_1m=0.5,
            )
        )
        db.add(
            Span(
                id="unpriced",
                run_id="r1",
                session_id=trace_session,
                kind="llm",
                name="m",
                started_at=2,
            )
        )
        await db.commit()
        d = service.tree_to_dict(await service.get_span_tree(db, "r1"))
        by_id = {x["span_id"]: x for x in d}
        assert by_id["priced"]["has_price"] is True
        assert by_id["unpriced"]["has_price"] is False

    async def test_no_orphan_spans_from_subagent(self) -> None:
        """
        子代理的 agent span 必须落库，否则它下面的 span 全是孤儿。

        这是真实验证抓到的 bug：subagent 工具最初用 `new_span`（只传上下文，
        不写表），于是子代理内部那几条 span 的 parent_span_id 指向一个
        【库里不存在的 id】。

        后果是执行树里子代理的 span 变成和 agent:main 平级的孤儿：

            agent:main
              llm
              tool:subagent
            llm          ← 应该在 tool:subagent 下面
            tool:list_dir  ←
            llm            ←

        看起来像"委派和主流程是两件独立的事"，完全看不出嵌套。

        孤儿 span 不报错、不丢数据，只是树画错了 —— 这类问题只能靠盯着
        树的形状发现，所以必须锁住。
        """
        import inspect

        from app.modules.agent.tools import subagent as sa

        src = inspect.getsource(sa.SubAgentTool.run)
        assert "record_span(" in src, "agent span 没落库，会产生孤儿 span"
        assert "new_span(" not in src, "又用回 new_span 了，它不写表"

    async def test_cleanup_removes_old_only(
        self, db: Any, trace_session: str
    ) -> None:
        """
        TTL 清理。常见实现没做 —— 落库了就必须管清理，
        否则这张表会无声长到几百 MB（span 增速是消息表的 5~10 倍）。
        """
        from app.core.time import now_ms
        from app.modules.trace import service
        from app.modules.trace.models import Run

        now = now_ms()
        old = now - 30 * 86_400_000
        await self._add_span(db, "old_span", session_id=trace_session, created=old)
        await self._add_span(db, "new_span", session_id=trace_session, created=now)
        db.add(
            Run(id="old_run", session_id=trace_session, started_at=old, created_at=old, updated_at=old)
        )
        db.add(
            Run(id="new_run", session_id=trace_session, started_at=now, created_at=now, updated_at=now)
        )
        await db.commit()

        res = await service.cleanup(db, retain_days=14)
        assert res["spans"] == 1
        assert res["runs"] == 1

        remaining = await service.get_span_tree(db, "r1")
        assert [n.span.id for n in remaining] == ["new_span"]

    async def test_stats(
        self, db: Any, trace_session: str
    ) -> None:
        from app.modules.trace import service
        from app.modules.trace.models import Run

        db.add(
            Run(
                id="r1",
                session_id=trace_session,
                started_at=1,
                total_tokens=500,
                cost_usd=0.002,
            )
        )
        await self._add_span(db, "sp1", session_id=trace_session)
        await db.commit()
        s = await service.stats(db)
        assert s["runs"] == 1
        assert s["spans"] == 1
        assert s["total_tokens"] == 500


class TestRunRollup:
    async def test_run_record_shape(self, trace_session: str) -> None:
        """run 记录带 parent_run_id，用于成本上卷。"""
        r = RunRecord(
            id="r1", session_id=trace_session, started_at=1, parent_run_id="r0", total_tokens=100
        )
        assert r.parent_run_id == "r0"
        assert r.total_tokens == 100

    async def test_span_totals_include_subagent(
        self, db: Any, trace_session: str
    ) -> None:
        """
        从 span 汇总时必须包含子代理的量，并按智能体拆开。

        子代理与父代理共享 run_id（同一次用户输入触发），所以库里没有独立
        的子 run 行 —— run.rollup_total_tokens 永远等于 total_tokens，
        字段名承诺"含子代理"但实际没有。真实数据在 span 表里。

        "委派花了多少"是判断委派值不值的唯一依据，所以必须能拆出来。
        """
        from app.modules.trace import service
        from app.modules.trace.models import Span

        def sp(sid: str, agent: str, tok: int, kind: str = "llm") -> Span:
            return Span(
                id=sid,
                run_id="r1",
                session_id=trace_session,
                kind=kind,
                name="m",
                agent_name=agent,
                started_at=1,
                total_tokens=tok,
                cost_usd=tok / 1_000_000,
            )

        db.add(sp("a1", "", 3000))  # 主代理
        db.add(sp("a2", "", 800))
        db.add(sp("b1", "researcher", 1500))  # 子代理
        # agent 类 span 上也挂 token（记的是子 loop 累计），不能重复计
        db.add(sp("agent1", "researcher", 1500, kind="agent"))
        await db.commit()

        t = await service.span_token_totals(db, "r1")
        # 3000 + 800 + 1500，不含 agent 类那 1500
        assert t["total_tokens"] == 5300
        by = {a["agent_name"]: a for a in t["by_agent"]}
        assert by["main"]["total_tokens"] == 3800
        assert by["researcher"]["total_tokens"] == 1500
        assert by["main"]["llm_calls"] == 2

    async def test_child_tokens_rollup_to_parent(
        self, db: Any, trace_session: str
    ) -> None:
        """
        子 run 的 token 上卷到父 run。

        常见实现在这里全部不合格：在前端内存累加、
        有实现在展示层按 parentSessionPath 重建、没实现。
        共同结果是【后端无法回答"这次任务总共花了多少钱"】。
        """
        from app.modules.trace.models import Run
        from app.modules.trace.writer import TraceWriter

        class _Ctx:
            def __init__(self, s: Any) -> None:
                self.s = s

            async def __aenter__(self) -> Any:
                return self.s

            async def __aexit__(self, *a: Any) -> None:
                return None

        w = TraceWriter(lambda: _Ctx(db))  # type: ignore[arg-type]

        # 父 run 先落库
        await w._write(
            RunRecord(id="parent", session_id=trace_session, started_at=1, total_tokens=100, cost_usd=0.001)
        )
        # 两个子 run 结束
        await w._write(
            RunRecord(
                id="c1",
                session_id=trace_session,
                started_at=2,
                parent_run_id="parent",
                status="done",
                total_tokens=500,
                cost_usd=0.005,
            )
        )
        await w._write(
            RunRecord(
                id="c2",
                session_id=trace_session,
                started_at=3,
                parent_run_id="parent",
                status="done",
                total_tokens=300,
                cost_usd=0.003,
            )
        )

        from sqlalchemy import select

        parent = (
            await db.execute(select(Run).where(Run.id == "parent"))
        ).scalar_one()
        # 自身 100 + 子 500 + 子 300
        assert parent.rollup_total_tokens == 900
        assert parent.rollup_cost_usd == pytest.approx(0.009)
        # 自身的 total 不被污染 —— 要能分开看"我自己花了多少"和"总共花了多少"
        assert parent.total_tokens == 100

    async def test_rollup_skips_running_child(
        self, db: Any, trace_session: str
    ) -> None:
        """还在跑的子 run 不上卷，否则会重复计。"""
        from app.modules.trace.models import Run
        from app.modules.trace.writer import TraceWriter
        from sqlalchemy import select

        class _Ctx:
            def __init__(self, s: Any) -> None:
                self.s = s

            async def __aenter__(self) -> Any:
                return self.s

            async def __aexit__(self, *a: Any) -> None:
                return None

        w = TraceWriter(lambda: _Ctx(db))  # type: ignore[arg-type]
        await w._write(RunRecord(id="p", session_id=trace_session, started_at=1))
        await w._write(
            RunRecord(
                id="c",
                session_id=trace_session,
                started_at=2,
                parent_run_id="p",
                status="running",
                total_tokens=999,
            )
        )
        p = (await db.execute(select(Run).where(Run.id == "p"))).scalar_one()
        assert p.rollup_total_tokens == 0

    async def test_rollup_missing_parent_is_safe(
        self, db: Any, trace_session: str
    ) -> None:
        """
        父 run 还没落库时（子先结束的竞态）丢掉这次上卷，不抛异常。

        追踪数据不值得为准确性引入重试和占位行的复杂度。
        """
        from app.modules.trace.writer import TraceWriter

        class _Ctx:
            def __init__(self, s: Any) -> None:
                self.s = s

            async def __aenter__(self) -> Any:
                return self.s

            async def __aexit__(self, *a: Any) -> None:
                return None

        w = TraceWriter(lambda: _Ctx(db))  # type: ignore[arg-type]
        # 父不存在
        await w._write(
            RunRecord(
                id="orphan",
                session_id=trace_session,
                started_at=1,
                parent_run_id="ghost",
                status="done",
                total_tokens=50,
            )
        )  # 不抛就对了

    def test_reasoning_not_double_counted(self) -> None:
        """
        reasoning 是 output 的子集，不能重复加进 total。 同类实现。加了就重复计费。
        """
        from app.core.trace_context import SpanInfo
        from app.modules.trace.recorder import SpanSink

        sink = SpanSink(
            info=SpanInfo(
                span_id="x", parent_span_id=None, depth=0, kind="llm", name="m"
            )
        )
        sink.set_usage(input_tokens=100, output_tokens=50, reasoning=30)
        # total 应该是 150，不是 180
        assert sink.usage["total_tokens"] == 150

    def test_unknown_usage_is_none_not_zero(self) -> None:
        """
        供应商不报的字段用 None 而非 0 —— 要能区分"未知"和"确实是零"。
        """
        from app.core.trace_context import SpanInfo
        from app.modules.trace.recorder import SpanSink

        sink = SpanSink(
            info=SpanInfo(
                span_id="x", parent_span_id=None, depth=0, kind="llm", name="m"
            )
        )
        sink.set_usage(input_tokens=100)
        assert sink.usage["cache_read_tokens"] is None
        assert sink.usage["output_tokens"] is None
