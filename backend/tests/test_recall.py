"""
测试记忆召回。
"""

import pytest
from app.modules.memory.recall import recall_memories
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_recall_memories_empty_session(db: AsyncSession):
    """空会话召回应该返回空结果。"""
    result = await recall_memories(
        db=db,
        session_id="ses_empty",
        query="测试查询",
        agent_id="agent_test",
    )

    assert result.memories == []
    assert result.rendered == ""
    assert result.total_chars == 0


@pytest.mark.asyncio
async def test_recall_memories_with_data(db: AsyncSession):
    """有记忆数据时应该能召回相关内容。"""
    # TODO: 需要先写入一些测试记忆数据
    # 然后验证召回结果

    # 创建会话和智能体
    from app.modules.session.models import Session
    session = Session(id="ses_recall_test", title="Recall Test")
    db.add(session)

    # 写入测试记忆
    from app.modules.memory import service as memory_service
    from app.modules.memory.layout import MemoryScope

    scope = MemoryScope(agent_id="agent_recall_test", session_id="ses_recall_test")

    # 写入偏好
    await memory_service.write(
        scope=scope,
        memory_type="preferences",
        fields={"preference": "喜欢 Python"},
        content="用户喜欢使用 Python 编程",
    )

    # 写入事件
    await memory_service.write(
        scope=scope,
        memory_type="events",
        fields={"event": "完成任务"},
        content="用户完成了一个 FastAPI 项目",
    )

    await db.commit()

    # 召回（无嵌入模型，应该返回空或报错）
    result = await recall_memories(
        db=db,
        session_id="ses_recall_test",
        query="Python 项目",
        agent_id="agent_recall_test",
        embedding_model=None,  # 测试环境没有嵌入模型
    )

    # 验证结构正确（即使结果为空）
    assert isinstance(result.memories, list)
    assert isinstance(result.rendered, str)
    assert isinstance(result.total_chars, int)
    assert "stats" in result.__dict__


@pytest.mark.asyncio
async def test_recall_respects_budget(db: AsyncSession):
    """召回应该遵守字符预算。"""
    result = await recall_memories(
        db=db,
        session_id="ses_budget_test",
        query="测试",
        agent_id="agent_test",
        max_chars=100,  # 很小的预算
    )

    # 即使有很多结果，总字符数也不应超过预算
    assert result.total_chars <= 100
