"""
技能状态管理测试。
"""

from __future__ import annotations

from app.modules.skill.state import (
    disabled_names,
    filter_l1,
    set_enabled,
)
from sqlalchemy.ext.asyncio import AsyncSession


class TestDisabledNames:
    async def test_empty_db_returns_empty_set(self, db: AsyncSession) -> None:
        result = await disabled_names(db)
        assert result == set()

    async def test_disabled_skill_is_returned(self, db: AsyncSession) -> None:
        await set_enabled(db, "code-review", False)
        result = await disabled_names(db)
        assert "code-review" in result

    async def test_enabled_skill_is_not_returned(self, db: AsyncSession) -> None:
        await set_enabled(db, "security-audit", True)
        result = await disabled_names(db)
        assert "security-audit" not in result

    async def test_not_in_table_is_enabled(self, db: AsyncSession) -> None:
        result = await disabled_names(db)
        assert "never-set-skill" not in result

    async def test_multiple_skills(self, db: AsyncSession) -> None:
        await set_enabled(db, "skill-a", False)
        await set_enabled(db, "skill-b", False)
        await set_enabled(db, "skill-c", True)
        result = await disabled_names(db)
        assert "skill-a" in result
        assert "skill-b" in result
        assert "skill-c" not in result


class TestSetEnabled:
    async def test_disable_then_list(self, db: AsyncSession) -> None:
        await set_enabled(db, "my-skill", False)
        result = await disabled_names(db)
        assert "my-skill" in result

    async def test_enable_after_disable(self, db: AsyncSession) -> None:
        await set_enabled(db, "toggle-skill", False)
        await set_enabled(db, "toggle-skill", True)
        result = await disabled_names(db)
        assert "toggle-skill" not in result

    async def test_disable_twice_idempotent(self, db: AsyncSession) -> None:
        await set_enabled(db, "idem-skill", False)
        await set_enabled(db, "idem-skill", False)
        result = await disabled_names(db)
        assert "idem-skill" in result

    async def test_enable_twice_idempotent(self, db: AsyncSession) -> None:
        await set_enabled(db, "idem2-skill", True)
        await set_enabled(db, "idem2-skill", True)
        result = await disabled_names(db)
        assert "idem2-skill" not in result

    async def test_enable_without_prior_record(self, db: AsyncSession) -> None:
        await set_enabled(db, "new-skill", True)
        result = await disabled_names(db)
        assert "new-skill" not in result

    async def test_disabled_in_valid_format(self, db: AsyncSession) -> None:
        """验证 disabled 条目确实是 enabled=0 的行。"""
        from app.modules.skill.models import SkillState
        from sqlalchemy import select

        await set_enabled(db, "check-format", False)
        row = (await db.execute(
            select(SkillState).where(SkillState.name == "check-format")
        )).scalar_one()
        assert row.enabled == 0

    async def test_enabled_record_is_row_with_enabled_1(self, db: AsyncSession) -> None:
        from app.modules.skill.models import SkillState
        from sqlalchemy import select

        await set_enabled(db, "check-enabled-1", True)
        row = (await db.execute(
            select(SkillState).where(SkillState.name == "check-enabled-1")
        )).scalar_one()
        assert row.enabled == 1


class TestFilterL1:
    async def test_empty_pairs_returns_empty(self, db: AsyncSession) -> None:
        result = await filter_l1(db, [])
        assert result == []

    async def test_all_enabled_passed_through(self, db: AsyncSession) -> None:
        pairs = [("skill-a", "A desc"), ("skill-b", "B desc"), ("skill-c", "C desc")]
        result = await filter_l1(db, pairs)
        assert result == pairs

    async def test_disabled_filtered_out(self, db: AsyncSession) -> None:
        await set_enabled(db, "blocked-skill", False)
        pairs = [("blocked-skill", "blocked"), ("ok-skill", "ok")]
        result = await filter_l1(db, pairs)
        assert result == [("ok-skill", "ok")]

    async def test_mix_of_disabled_and_enabled(self, db: AsyncSession) -> None:
        await set_enabled(db, "off-a", False)
        await set_enabled(db, "off-b", False)
        pairs = [
            ("off-a", "a"),
            ("on-x", "x"),
            ("off-b", "b"),
            ("on-y", "y"),
        ]
        result = await filter_l1(db, pairs)
        assert result == [("on-x", "x"), ("on-y", "y")]

    async def test_all_disabled_returns_empty(self, db: AsyncSession) -> None:
        await set_enabled(db, "a", False)
        await set_enabled(db, "b", False)
        result = await filter_l1(db, [("a", ""), ("b", "")])
        assert result == []
