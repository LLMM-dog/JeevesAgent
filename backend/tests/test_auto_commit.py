"""
测试自动记忆提取。
"""

import pytest
from app.modules.memory.auto_commit import check_and_trigger
from app.modules.session.models import Session
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_auto_commit_triggers_on_token_threshold(db: AsyncSession, monkeypatch):
    """达到 token 阈值时自动触发记忆提取。"""
    # 设置较低的阈值便于测试
    from app.core import config
    monkeypatch.setattr(config.settings.memory, "auto_commit_pending_token_threshold", 100)

    # 创建会话
    session = Session(id="ses_test", title="Test")
    db.add(session)
    await db.commit()

    # 添加一些消息（模拟超过阈值）
    from app.modules.agent.messages import Msg
    from app.modules.session import repo

    for _i in range(10):
        await repo.append_message(
            db,
            "ses_test",
            Msg(role="user", content="a" * 50),  # 50 字符 ≈ 12 tokens
        )

    # 检查是否会触发
    # 注意：实际不会真正提取（因为缺少 LLM），但会记录日志
    triggered = await check_and_trigger(db, "ses_test")

    # 10 条消息 * 50 字符 / 4 = 125 tokens > 100 阈值
    # 应该触发
    assert triggered is True


@pytest.mark.asyncio
async def test_auto_commit_not_triggered_below_threshold(db: AsyncSession):
    """未达到阈值时不触发。"""
    # 创建会话
    session = Session(id="ses_test2", title="Test")
    db.add(session)
    await db.commit()

    # 只添加少量消息
    from app.modules.agent.messages import Msg
    from app.modules.session import repo

    await repo.append_message(
        db,
        "ses_test2",
        Msg(role="user", content="hello"),
    )

    triggered = await check_and_trigger(db, "ses_test2")

    # 未达到阈值，不应触发
    assert triggered is False
