"""
agent_service 测试。
"""

from app.modules.agent import agent_service
from sqlalchemy.ext.asyncio import AsyncSession


class TestAgentService:
    async def test_create_and_get(self, db: AsyncSession) -> None:
        agent = await agent_service.create(
            db,
            name="测试智能体",
            description="用于测试",
            system_prompt="你是测试助手",
            permission_read=True,
            permission_write=True,
        )
        assert agent.id.startswith("adf_")
        assert agent.name == "测试智能体"
        assert agent.permission_write == 1

        fetched = await agent_service.get(db, agent.id)
        assert fetched is not None
        assert fetched.name == "测试智能体"

    async def test_create_defaults(self, db: AsyncSession) -> None:
        agent = await agent_service.create(db, name="默认值测试")
        assert agent.permission_read == 1
        assert agent.permission_write == 0
        assert agent.permission_shell == 0
        assert agent.permission_network == 0
        assert agent.permission_subagent == 0
        assert agent.verification_enabled == 0
        assert agent.strict_mode == 0
        assert agent.hidden == 0
        assert agent.skill_names == "[]"

    async def test_list_only_shows_existing(self, db: AsyncSession) -> None:
        await agent_service.create(db, name="活跃智能体")  # noqa: F841
        a2 = await agent_service.create(db, name="待删除")
        await agent_service.delete(db, a2.id)

        agents = await agent_service.list_all(db)
        names = [a.name for a in agents]
        assert "活跃智能体" in names
        assert "待删除" not in names  # 硬删除后不再出现

    async def test_update_partial(self, db: AsyncSession) -> None:
        agent = await agent_service.create(db, name="原名", description="旧描述")

        updated = await agent_service.update(db, agent.id, name="新名")
        assert updated is not None
        assert updated.name == "新名"
        assert updated.description == "旧描述"  # 没传的不变

    async def test_update_permissions(self, db: AsyncSession) -> None:
        agent = await agent_service.create(db, name="权限测试")
        assert agent.permission_write == 0

        updated = await agent_service.update(db, agent.id, permission_write=True)
        assert updated is not None
        assert updated.permission_write == 1

    async def test_update_nonexistent(self, db: AsyncSession) -> None:
        result = await agent_service.update(db, "adf_nonexistent", name="x")
        assert result is None

    async def test_delete_removes_row(self, db: AsyncSession) -> None:
        agent = await agent_service.create(db, name="待删除")
        ok = await agent_service.delete(db, agent.id)
        assert ok
        # 硬删除后 get 返回 None
        fetched = await agent_service.get(db, agent.id)
        assert fetched is None

    async def test_delete_nonexistent(self, db: AsyncSession) -> None:
        ok = await agent_service.delete(db, "adf_nonexistent")
        assert not ok

    async def test_count(self, db: AsyncSession) -> None:
        assert await agent_service.count(db) == 0
        await agent_service.create(db, name="a")
        await agent_service.create(db, name="b")
        assert await agent_service.count(db) == 2

        # 删除一个，计数减一
        agents = await agent_service.list_all(db)
        await agent_service.delete(db, agents[0].id)
        assert await agent_service.count(db) == 1

    async def test_ensure_default_creates_once(self, db: AsyncSession) -> None:
        a1 = await agent_service.ensure_default(db)
        assert a1.id == agent_service.DEFAULT_AGENT_ID
        assert a1.name == "默认助手"

        # 第二次调用不创建新的
        a2 = await agent_service.ensure_default(db)
        assert a2.id == a1.id

        # 只有一个智能体
        assert await agent_service.count(db) == 1

    async def test_list_by_hidden(self, db: AsyncSession) -> None:
        await agent_service.create(db, name="可见智能体")
        await agent_service.create(db, name="隐藏智能体")
        agents = await agent_service.list_all(db)
        await agent_service.update(db, agents[1].id, hidden=True)

        visible = await agent_service.list_all(db, hidden=False)
        assert len(visible) == 1
        assert visible[0].name == "可见智能体"

        hidden_list = await agent_service.list_all(db, hidden=True)
        assert len(hidden_list) == 1
        assert hidden_list[0].name == "隐藏智能体"

        all_agents = await agent_service.list_all(db)
        assert len(all_agents) == 2

    async def test_list_by_skill(self, db: AsyncSession) -> None:
        await agent_service.create(db, name="有审查技能的智能体", skill_names=["code-review"])
        await agent_service.create(db, name="无此技能")

        agents = await agent_service.list_all(db, using_skill="code-review")
        assert len(agents) == 1
        assert agents[0].name == "有审查技能的智能体"

    async def test_list_by_mcp(self, db: AsyncSession) -> None:
        await agent_service.create(db, name="有MCP", mcp_servers=["github"])
        await agent_service.create(db, name="无MCP")

        agents = await agent_service.list_all(db, using_mcp="github")
        assert len(agents) == 1
        assert agents[0].name == "有MCP"
