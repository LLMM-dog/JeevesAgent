"""
截断重发与会话置顶。

## 为什么值得单独一个文件

`truncate_from` 之前【零测试覆盖】。它是唯一会成批删数据的接口，
删错的后果不可逆 —— 用户点"重发"丢掉的是真实对话记录。

三个必须保证的点：

1. **边界**：删「该条及其之后」，前面的一条都不能少。
2. **计数同步**：`message_count` / `last_message_at` 要跟着变，
   否则列表页显示的条数和实际点进去看到的不一致。
3. **跑着的 run 不能截断**：run 还在往库里写，截断后它写回来的消息
   会挂在已删除的历史后面，上下文出现空洞。
"""

from typing import Any

import pytest
import pytest_asyncio
from app.core.exceptions import NotFoundError
from app.core.time import now_ms
from app.modules.agent.messages import Msg, ToolCall
from app.modules.session import repo
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _ws(db: AsyncSession) -> None:
    """
    会话有 workspace 外键，建会话前必须先有默认工作区。

    设成 autouse 而不是让每个测试自己调 —— 漏一个就是一条
    "没有可用的工作区" 的 NotFoundError，而报错信息完全不指向
    "你忘了建工作区" 这个真因。
    """
    await repo.ensure_default_workspace(db, "/tmp/ws-truncate")


async def _mk(db: AsyncSession, title: str = "t") -> str:
    s = await repo.create_session(db, title=title)
    return s.id


async def _fill(db: AsyncSession, sid: str) -> list[str]:
    """造一轮完整对话：user → assistant(带 tool_call) → tool → assistant。"""
    ids = []
    ids.append(await repo.append_message(db, sid, Msg(role="user", content="第一问")))
    ids.append(await repo.append_message(db, sid, Msg(role="assistant", content="第一答")))
    ids.append(await repo.append_message(db, sid, Msg(role="user", content="第二问")))
    ids.append(
        await repo.append_message(
            db,
            sid,
            Msg(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="c1", name="read_file", arguments="{}")],
            ),
        )
    )
    ids.append(
        await repo.append_message(
            db, sid, Msg(role="tool", content="文件内容", tool_call_id="c1")
        )
    )
    ids.append(await repo.append_message(db, sid, Msg(role="assistant", content="第二答")))
    return ids


class TestTruncateBoundary:
    async def test_deletes_target_and_after(self, db: AsyncSession) -> None:
        sid = await _mk(db)
        ids = await _fill(db, sid)

        # 从「第二问」截断
        n = await repo.truncate_from(db, sid, ids[2])

        rows = await repo.load_messages(db, sid)
        contents = [r.content for r in rows]
        assert contents == ["第一问", "第一答"], "第二问及其之后应全部删除"
        assert n == 4, f"应删 4 条（第二问/assistant/tool/第二答），实际 {n}"

    async def test_keeps_everything_before(self, db: AsyncSession) -> None:
        """最容易写错成 seq > target.seq，把目标本身留下来。"""
        sid = await _mk(db)
        ids = await _fill(db, sid)

        await repo.truncate_from(db, sid, ids[-1])

        rows = await repo.load_messages(db, sid)
        assert len(rows) == 5, "只删最后一条"
        assert all(r.id != ids[-1] for r in rows), "目标自己必须被删掉"

    async def test_truncate_first_clears_all(self, db: AsyncSession) -> None:
        sid = await _mk(db)
        ids = await _fill(db, sid)

        await repo.truncate_from(db, sid, ids[0])

        assert await repo.load_messages(db, sid) == []

    async def test_tool_messages_counted_in_deleted(self, db: AsyncSession) -> None:
        """
        返回值是删除的【总条数】（含 tool），而 message_count 只减可见条数。

        两者不同是有意的：用户看到的是"删了 2 条对话"，
        而实际物理删除包含中间的 tool 消息。
        """
        sid = await _mk(db)
        ids = await _fill(db, sid)

        n = await repo.truncate_from(db, sid, ids[3])
        assert n == 3, "assistant + tool + assistant"

    async def test_unknown_id_raises_not_found(self, db: AsyncSession) -> None:
        sid = await _mk(db)
        await _fill(db, sid)
        with pytest.raises(NotFoundError):
            await repo.truncate_from(db, sid, "msg_nonexistent")

    async def test_other_session_id_is_not_found(self, db: AsyncSession) -> None:
        """
        用别的会话的 message_id 必须报 not found，不能跨会话删。

        少了 session_id 条件的话，传另一个会话的 message_id 会按它的 seq
        去删【本会话】里 seq 相同的消息 —— 删掉的是完全无关的内容。
        """
        a = await _mk(db)
        b = await _mk(db)
        ids_a = await _fill(db, a)
        await _fill(db, b)

        with pytest.raises(NotFoundError):
            await repo.truncate_from(db, b, ids_a[0])

        assert len(await repo.load_messages(db, b)) == 6, "b 的消息一条都不该动"


class TestCountsStayConsistent:
    async def test_message_count_decreases(self, db: AsyncSession) -> None:
        sid = await _mk(db)
        ids = await _fill(db, sid)

        before = (await repo.get_session(db, sid)).message_count
        await repo.truncate_from(db, sid, ids[2])
        after = (await repo.get_session(db, sid)).message_count

        assert after < before, "截断后计数必须下降"
        assert after == 2, "只剩第一问 + 第一答"

    async def test_message_count_never_negative(self, db: AsyncSession) -> None:
        """
        清空所有消息后计数必须是 0 而不是负数。

        `message_count - visible` 直接相减，若冗余字段本身偏小就会变负，
        列表页会显示 "-2 条消息"。
        """
        sid = await _mk(db)
        ids = await _fill(db, sid)
        await repo.truncate_from(db, sid, ids[0])

        s = await repo.get_session(db, sid)
        assert s.message_count >= 0, f"计数为负：{s.message_count}"

    async def test_last_message_at_rolls_back(self, db: AsyncSession) -> None:
        """
        截断后 last_message_at 要退回到剩余消息的时间。

        不退的话会话在列表里仍按"最新"排序，用户看到一个显示时间很新
        但内容已经被截断的会话排在最前面。
        """
        sid = await _mk(db)
        ids = await _fill(db, sid)

        await repo.truncate_from(db, sid, ids[2])
        s = await repo.get_session(db, sid)

        rows = await repo.load_messages(db, sid)
        assert s.last_message_at == max(r.created_at for r in rows)

    async def test_empty_session_last_message_at_zero(self, db: AsyncSession) -> None:
        sid = await _mk(db)
        ids = await _fill(db, sid)
        await repo.truncate_from(db, sid, ids[0])

        s = await repo.get_session(db, sid)
        assert s.last_message_at == 0, "没有消息了，时间应归零"


class TestNewSessionVisible:
    """
    新建的空会话必须能在列表里找到。

    真实验证抓到的 bug：`last_message_at` 默认 0，而列表按
    `pinned DESC, last_message_at DESC` 排序 —— 于是新建的空会话排到
    【最后】。实测在 100 个会话的库里，点"新对话"建出来的会话落在
    第 99 位。用户被导航进去，一旦离开就再也找不回来。

    这个 bug 只在会话数量多的时候才显现：库里只有两三个会话时，
    排最后和排最前看起来都"在列表里"。
    """

    async def test_new_session_has_nonzero_timestamp(self, db: AsyncSession) -> None:
        s = await repo.create_session(db, title="新的")
        assert s.last_message_at > 0, "新会话的 last_message_at 不能是 0"

    async def test_new_session_sorts_first(self, db: AsyncSession) -> None:
        """
        在一堆更早活跃的会话中，新建的空会话要排最前。

        ## 为什么要手动改时间戳

        `now_ms()` 只有毫秒精度，测试里连续建 6 个会话往往落在同一毫秒 ——
        那时 last_message_at 完全相同，谁排第一取决于兜底键（id），
        断言"新建的排第一"就成了掷硬币。

        这个测试要验的是【时间序】，所以把旧会话的时间显式往前推，
        让比较有意义。同一毫秒的情况由 test_same_ms_order_is_stable 覆盖。
        """
        base = now_ms()
        for i in range(5):
            old = await repo.create_session(db, title=f"旧{i}")
            await repo.append_message(db, old.id, Msg(role="user", content="x"))
            old.last_message_at = base - 10000 - i * 100
        await db.commit()

        fresh = await repo.create_session(db, title="刚建的")
        rows, _ = await repo.list_sessions(db)
        assert rows[0].id == fresh.id, "新建的空会话应该排在最前"

    async def test_new_session_not_last(self, db: AsyncSession) -> None:
        """回归保护：只要它不是最后一个，就说明没有退回 0 的行为。"""
        base = now_ms()
        for i in range(3):
            o = await repo.create_session(db, title=f"o{i}")
            await repo.append_message(db, o.id, Msg(role="user", content="y"))
            o.last_message_at = base - 5000 - i * 100
        await db.commit()
        fresh = await repo.create_session(db)

        rows, _ = await repo.list_sessions(db)
        assert rows[-1].id != fresh.id

    async def test_same_ms_order_is_stable(self, db: AsyncSession) -> None:
        """
        同一毫秒内建的会话，排序必须稳定。

        真实问题：`now_ms()` 是毫秒精度，同一毫秒内的会话
        last_message_at 相同。只按它排序的话 SQLite 返回顺序任意 ——

          - 分页会重复或漏记录（第 1 页和第 2 页拿到同一条）
          - 列表顺序在两次刷新之间无理由跳动

        所以 order_by 必须有兜底键。这里连查两次，顺序应完全一致。
        """
        ids = []
        for i in range(8):
            s = await repo.create_session(db, title=f"同毫秒{i}")
            ids.append(s.id)
        # 强制所有会话时间戳相同，模拟同一毫秒
        for sid_ in ids:
            row = await repo.get_session(db, sid_)
            row.last_message_at = 1700000000000
            row.created_at = 1700000000000
        await db.commit()

        first, _ = await repo.list_sessions(db, size=8)
        second, _ = await repo.list_sessions(db, size=8)
        assert [r.id for r in first] == [r.id for r in second], "同毫秒排序不稳定"

    async def test_pagination_no_overlap_same_ms(self, db: AsyncSession) -> None:
        """
        同毫秒时分页不能重复。

        没有兜底键的话，第 1 页和第 2 页可能返回同一条记录 ——
        用户看到重复的会话，而另一条永远看不到。
        """
        for i in range(6):
            s = await repo.create_session(db, title=f"页{i}")
            row = await repo.get_session(db, s.id)
            row.last_message_at = 1700000000000
        await db.commit()

        p1, total = await repo.list_sessions(db, page=1, size=3)
        p2, _ = await repo.list_sessions(db, page=2, size=3)
        ids1 = {r.id for r in p1}
        ids2 = {r.id for r in p2}
        assert not (ids1 & ids2), f"分页有重复：{ids1 & ids2}"
        assert len(ids1 | ids2) == 6, "两页合起来应覆盖全部 6 个"
        assert total == 6


class TestPinned:
    async def test_pinned_sorts_first(self, db: AsyncSession) -> None:
        """置顶的排在前面，即使它的最后消息时间更早。"""
        old = await repo.create_session(db, title="旧但置顶")
        await repo.append_message(db, old.id, Msg(role="user", content="a"))
        new = await repo.create_session(db, title="新但未置顶")
        await repo.append_message(db, new.id, Msg(role="user", content="b"))

        old.pinned = 1
        await db.commit()

        rows, _ = await repo.list_sessions(db)
        assert rows[0].id == old.id, "置顶的必须在最前"

    async def test_pinned_group_sorted_by_time(self, db: AsyncSession) -> None:
        """两个都置顶时，内部仍按时间倒序。"""
        first = await repo.create_session(db, title="先")
        await repo.append_message(db, first.id, Msg(role="user", content="a"))
        second = await repo.create_session(db, title="后")
        await repo.append_message(db, second.id, Msg(role="user", content="b"))

        first.pinned = 1
        second.pinned = 1
        await db.commit()

        rows, _ = await repo.list_sessions(db)
        assert [r.id for r in rows[:2]] == [second.id, first.id]

    async def test_unpin_returns_to_time_order(self, db: AsyncSession) -> None:
        old = await repo.create_session(db, title="旧")
        await repo.append_message(db, old.id, Msg(role="user", content="a"))
        new = await repo.create_session(db, title="新")
        await repo.append_message(db, new.id, Msg(role="user", content="b"))

        old.pinned = 1
        await db.commit()
        assert (await repo.list_sessions(db))[0][0].id == old.id

        old.pinned = 0
        await db.commit()
        rows, _ = await repo.list_sessions(db)
        assert rows[0].id == new.id, "取消置顶后回到时间序"


class TestActiveRunGuard:
    async def test_truncate_blocked_during_run(self, db: AsyncSession) -> None:
        """
        跑着的时候不能截断。

        run 还在往库里追加消息。先截断再让 run 写完，写回来的消息会接在
        被删掉的历史之后 —— 上下文中间缺一段，而模型不会说"我少看了东西"，
        它会照着残缺的上下文继续答，表现为答非所问。
        """
        import asyncio

        from app.api import routes_chat
        from app.core.exceptions import ConflictError
        from app.modules.agent import run_registry

        sid = await _mk(db)
        ids = await _fill(db, sid)

        async def _never() -> None:
            await asyncio.sleep(3600)

        task = asyncio.create_task(_never())
        run_registry.register("run_x", sid, task)
        try:
            with pytest.raises(ConflictError) as ei:
                await routes_chat.truncate_messages(sid, ids[0], db=db)
            assert ei.value.code == "run_in_progress"
            # 数据必须完好
            assert len(await repo.load_messages(db, sid)) == 6
        finally:
            run_registry.unregister("run_x")
            task.cancel()

    async def test_truncate_ok_after_run_ends(self, db: AsyncSession) -> None:
        from app.api import routes_chat

        sid = await _mk(db)
        ids = await _fill(db, sid)

        res: dict[str, Any] = await routes_chat.truncate_messages(sid, ids[2], db=db)
        assert res["deleted_count"] == 4
