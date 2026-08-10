"""
运行注册表测试。
"""

from __future__ import annotations

import asyncio

import pytest
from app.modules.agent.run_registry import (
    _active_by_session,
    _runs,
    active_run_of,
    cancel,
    cancel_all,
    register,
    unregister,
)


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    """每个测试前清空全局注册表，避免测试间污染。"""
    _runs.clear()
    _active_by_session.clear()
    yield
    _runs.clear()
    _active_by_session.clear()


class TestRegisterAndGet:
    async def test_register_new_run(self) -> None:
        async def hang(event: asyncio.Event) -> None:
            await event.wait()

        evt = asyncio.Event()
        task = asyncio.create_task(hang(evt))
        register("run_001", "ses_001", task)
        assert active_run_of("ses_001") == "run_001"
        cancel("run_001")
        await asyncio.sleep(0)
        assert task.cancelled() or task.done()

    async def test_register_overwrites_session(self) -> None:
        async def hang(event: asyncio.Event) -> None:
            await event.wait()

        e1 = asyncio.Event()
        e2 = asyncio.Event()
        t1 = asyncio.create_task(hang(e1))
        t2 = asyncio.create_task(hang(e2))
        register("run_001", "ses_001", t1)
        register("run_002", "ses_001", t2)
        assert active_run_of("ses_001") == "run_002"
        cancel("run_001")
        cancel("run_002")

    async def test_active_run_of_unknown_session(self) -> None:
        assert active_run_of("ses_unknown") is None

    async def test_active_run_of_after_cancel(self) -> None:
        async def hang(event: asyncio.Event) -> None:
            await event.wait()

        evt = asyncio.Event()
        task = asyncio.create_task(hang(evt))
        register("run_001", "ses_001", task)
        cancel("run_001")
        await asyncio.sleep(0)
        assert active_run_of("ses_001") is None

    async def test_active_run_of_after_unregister(self) -> None:
        async def hang(event: asyncio.Event) -> None:
            await event.wait()

        evt = asyncio.Event()
        task = asyncio.create_task(hang(evt))
        register("run_001", "ses_001", task)
        unregister("run_001")
        assert active_run_of("ses_001") is None
        # 清理
        if not task.done():
            task.cancel()


class TestCancel:
    async def test_cancel_returns_true_for_existing(self) -> None:
        async def hang(event: asyncio.Event) -> None:
            await event.wait()

        evt = asyncio.Event()
        task = asyncio.create_task(hang(evt))
        register("run_001", "ses_001", task)
        assert cancel("run_001") is True

    async def test_cancel_returns_false_for_unknown(self) -> None:
        assert cancel("run_nonexistent") is False

    async def test_cancel_returns_false_on_second_call(self) -> None:
        async def hang(event: asyncio.Event) -> None:
            await event.wait()

        evt = asyncio.Event()
        task = asyncio.create_task(hang(evt))
        register("run_001", "ses_001", task)
        assert cancel("run_001") is True
        await asyncio.sleep(0)
        assert cancel("run_001") is False

    async def test_cancel_actually_cancels_task(self) -> None:
        async def hang(event: asyncio.Event) -> None:
            await event.wait()

        evt = asyncio.Event()
        task = asyncio.create_task(hang(evt))
        register("run_001", "ses_001", task)
        cancel("run_001")
        await asyncio.sleep(0)
        assert task.done()

    async def test_cancel_two_different_sessions(self) -> None:
        async def hang(event: asyncio.Event) -> None:
            await event.wait()

        e1 = asyncio.Event()
        e2 = asyncio.Event()
        t1 = asyncio.create_task(hang(e1))
        t2 = asyncio.create_task(hang(e2))
        register("run_001", "ses_001", t1)
        register("run_002", "ses_002", t2)
        cancel("run_001")
        cancel("run_002")
        await asyncio.sleep(0)
        assert active_run_of("ses_001") is None
        assert active_run_of("ses_002") is None


class TestCancelAll:
    async def test_cancel_all_clears_all(self) -> None:
        async def hang(event: asyncio.Event) -> None:
            try:
                await event.wait()
            except asyncio.CancelledError:
                pass

        e1 = asyncio.Event()
        e2 = asyncio.Event()
        t1 = asyncio.create_task(hang(e1))
        t2 = asyncio.create_task(hang(e2))
        await asyncio.sleep(0)
        register("run_a", "ses_a", t1)
        register("run_b", "ses_b", t2)
        await cancel_all()
        assert active_run_of("ses_a") is None
        assert active_run_of("ses_b") is None

    async def test_cancel_all_idempotent(self) -> None:
        await cancel_all()
        await cancel_all()  # 不应崩溃


class TestActiveRunOfGarbageCollection:
    async def test_done_task_cleaned_by_active_run_of(self) -> None:
        """如果 task 已完成但注册表未清，active_run_of 应清理并返回 None。"""
        async def quick() -> None:
            pass

        task = asyncio.create_task(quick())
        await task  # 等待完成
        register("run_x", "ses_x", task)
        assert active_run_of("ses_x") is None

    async def test_register_overwrites_old_session_mapping(self) -> None:
        """当新 run 替换同一 session 时，旧 session mapping 被覆盖。"""
        async def hang(event: asyncio.Event) -> None:
            await event.wait()

        e1 = asyncio.Event()
        e2 = asyncio.Event()
        t1 = asyncio.create_task(hang(e1))
        t2 = asyncio.create_task(hang(e2))
        register("old_run", "common_ses", t1)
        register("new_run", "common_ses", t2)
        assert active_run_of("common_ses") == "new_run"
        cancel("old_run")
        cancel("new_run")
