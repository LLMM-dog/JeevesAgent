"""
对话种子导入：验证消息真的落进了 message 表，且走的是生产路径。

## 为什么要专门测种子导入

种子是所有记忆测试的前提。它悄悄坏掉（少几条、seq 重复、tool_calls 丢了）
会让上层断言以看起来无关的方式失败 —— 排查成本远高于在这里直接验一遍。
"""

from __future__ import annotations

import pytest
from app.modules.session.models import Message, Session
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.seed import load_conversation, load_seed, seed_session


@pytest.mark.asyncio
async def test_seed_lands_in_message_table(db: AsyncSession, workspace_id: str) -> None:
    """
    消息存数据库，不存文件。种子只是输入源。
    """
    sid = await seed_session(db, "ses_first_memory", workspace_id=workspace_id, agent_id="adf_demo")

    n = (
        await db.execute(select(func.count()).select_from(Message).where(Message.session_id == sid))
    ).scalar()
    assert n == len(load_seed("ses_first_memory"))


@pytest.mark.asyncio
async def test_seq_is_assigned_by_db_not_by_seed(db: AsyncSession, workspace_id: str) -> None:
    """
    seq 由 _next_seq 分配，让 ix_message_seq 唯一索引真正参与。

    种子里的 seq 只用于排序 —— 直接写库会绕过分配逻辑，那样
    "seq 分配坏了"这类 bug 在测试里永远发现不了。

    【从 1 开始】而不是 0：_next_seq 返回 max(seq)+1，空会话时 max 为 NULL
    → 0+1。这是生产行为，测试跟着它，不要反过来要求生产改。
    """
    sid = await seed_session(db, "ses_first_memory", workspace_id=workspace_id)

    rows = (
        await db.execute(select(Message).where(Message.session_id == sid).order_by(Message.seq))
    ).scalars().all()
    seqs = [r.seq for r in rows]
    assert seqs == list(range(1, len(seqs) + 1)), "seq 必须从 1 起严格连续"
    assert len(set(seqs)) == len(seqs), "seq 必须唯一"


@pytest.mark.asyncio
async def test_session_counters_are_updated(db: AsyncSession, workspace_id: str) -> None:
    """
    冗余计数字段必须被维护 —— 批量 INSERT 会绕过这段逻辑。

    ## message_count 只算 user/assistant

    `repo.append_message` 刻意不把 tool 结果计入（repo.py:256 的注释：
    用户看到的"24 条消息"不该包含工具结果）。所以断言要按这个语义算，
    而不是按种子总行数 —— 我第一版写成总行数，测试失败后才发现
    那是在要求生产改成我以为的样子。
    """
    seed = load_seed("ses_first_memory")
    visible = sum(1 for r in seed if r.get("role") in ("user", "assistant"))

    sid = await seed_session(db, "ses_first_memory", workspace_id=workspace_id)

    session = (await db.execute(select(Session).where(Session.id == sid))).scalars().one()
    assert session.message_count == visible
    assert session.message_count < len(seed), "工具结果不该计入 message_count"
    assert session.last_message_at > 0


@pytest.mark.asyncio
async def test_tool_calls_survive_the_round_trip(db: AsyncSession, workspace_id: str) -> None:
    """
    tool_calls 在种子里是 JSON 字符串（与 DB 列一致），且有两种形状
    （OpenAI 嵌套 / 扁平）。只认一种会让 tool_calls 静默变空。
    """
    sid = await seed_session(db, "ses_first_memory", workspace_id=workspace_id)

    msgs = await load_conversation(db, sid)
    called = [tc.name for m in msgs for tc in m.tool_calls]

    # 数字对着 fixture 数出来的，不是猜的：read_file×1、edit_file×3、run_shell×2
    assert called.count("edit_file") == 3
    assert called.count("run_shell") == 2
    assert called.count("read_file") == 1
    assert len(called) == 6
    # 参数要能解析回 dict，否则提取阶段拿不到工具的输入
    first_edit = next(tc for m in msgs for tc in m.tool_calls if tc.name == "edit_file")
    assert "path" in first_edit.parsed_args()


@pytest.mark.asyncio
async def test_error_flag_is_preserved(db: AsyncSession, workspace_id: str) -> None:
    """
    失败的工具结果要能被识别 —— 它是 experiences 的 Reflect 段的原料。
    """
    sid = await seed_session(db, "ses_accumulated", workspace_id=workspace_id)

    msgs = await load_conversation(db, sid)
    failures = [m for m in msgs if m.is_error]

    assert len(failures) == 1
    assert "FrozenInstanceError" in (failures[0].content or "")


@pytest.mark.asyncio
async def test_two_sessions_are_independent(db: AsyncSession, workspace_id: str) -> None:
    """两份种子导入同一个库时互不干扰。"""
    a = await seed_session(db, "ses_first_memory", workspace_id=workspace_id, agent_id="adf_demo")
    b = await seed_session(db, "ses_accumulated", workspace_id=workspace_id, agent_id="adf_demo")

    assert a != b
    assert len(await load_conversation(db, a)) == len(load_seed("ses_first_memory"))
    assert len(await load_conversation(db, b)) == len(load_seed("ses_accumulated"))


@pytest.mark.asyncio
async def test_deleting_session_cascades_to_messages(db: AsyncSession, workspace_id: str) -> None:
    """
    ON DELETE CASCADE 是"消息留在 SQL"的三个理由之一。
    文件形态下要手工遍历删除，漏一次就留孤儿数据。
    """
    from app.modules.session import repo

    sid = await seed_session(db, "ses_first_memory", workspace_id=workspace_id)
    await repo.delete_session(db, sid)

    n = (
        await db.execute(select(func.count()).select_from(Message).where(Message.session_id == sid))
    ).scalar()
    assert n == 0


def test_missing_seed_gives_actionable_error() -> None:
    """
    种子缺失最可能的原因是 gitignore 吞了它。错误信息要指向那里，
    否则下一个人要重新查一遍。
    """
    with pytest.raises(FileNotFoundError, match="gitignore"):
        load_seed("ses_does_not_exist")
