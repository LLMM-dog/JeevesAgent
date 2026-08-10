"""
智能体定义 CRUD。

默认智能体在首次启动时自动创建，不可删除。
"""

from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id
from app.core.time import now_ms
from app.modules.agent.models import AgentDefinition

PREFIX = "adf"


async def create(
    db: AsyncSession,
    *,
    name: str,
    description: str = "",
    system_prompt: str = "",
    model_id: str | None = None,
    skill_names: list[str] | None = None,
    mcp_servers: list[str] | None = None,
    permission_read: bool = True,
    permission_write: bool = False,
    permission_shell: bool = False,
    permission_network: bool = False,
    permission_subagent: bool = False,
    verification_enabled: bool = False,
    strict_mode: bool = False,
    hidden: bool = False,
    max_turns: int | None = None,
) -> AgentDefinition:
    ts = now_ms()
    agent = AgentDefinition(
        id=new_id(PREFIX),
        name=name,
        description=description,
        system_prompt=system_prompt,
        model_id=model_id,
        skill_names=json.dumps(skill_names or [], ensure_ascii=False),
        mcp_servers=json.dumps(mcp_servers or [], ensure_ascii=False),
        permission_read=int(permission_read),
        permission_write=int(permission_write),
        permission_shell=int(permission_shell),
        permission_network=int(permission_network),
        permission_subagent=int(permission_subagent),
        verification_enabled=int(verification_enabled),
        strict_mode=int(strict_mode),
        hidden=int(hidden),
        max_turns=max_turns,
        created_at=ts,
        updated_at=ts,
    )
    db.add(agent)
    await db.flush()
    return agent


async def get(db: AsyncSession, agent_id: str) -> AgentDefinition | None:
    return await db.get(AgentDefinition, agent_id)


async def list_all(
    db: AsyncSession,
    *,
    hidden: bool | None = None,
    using_skill: str | None = None,
    using_mcp: str | None = None,
) -> list[AgentDefinition]:
    stmt = select(AgentDefinition)
    if hidden is True:
        stmt = stmt.where(AgentDefinition.hidden == 1)
    elif hidden is False:
        stmt = stmt.where(AgentDefinition.hidden == 0)
    if using_skill:
        # skill_names 是 JSON 数组字符串，如 '["code-review", "security-audit"]'
        # 用 LIKE 匹配是否包含该 skill 名
        stmt = stmt.where(AgentDefinition.skill_names.like(f'%"{using_skill}"%'))
    if using_mcp:
        stmt = stmt.where(AgentDefinition.mcp_servers.like(f'%"{using_mcp}"%'))
    stmt = stmt.order_by(AgentDefinition.name)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def count(db: AsyncSession) -> int:
    stmt = select(func.count()).select_from(AgentDefinition)
    result = await db.execute(stmt)
    return result.scalar_one()


async def update(
    db: AsyncSession,
    agent_id: str,
    **kwargs: object,
) -> AgentDefinition | None:
    agent = await get(db, agent_id)
    if agent is None:
        return None

    for key, value in kwargs.items():
        if key in ("skill_names", "mcp_servers") and isinstance(value, list):
            value = json.dumps(value, ensure_ascii=False)
        if key.startswith("permission_") or key in ("verification_enabled", "strict_mode", "hidden"):
            value = int(value)  # type: ignore[arg-type]
        if hasattr(agent, key):
            setattr(agent, key, value)

    agent.updated_at = now_ms()
    await db.flush()
    return agent


async def delete(db: AsyncSession, agent_id: str) -> bool:
    agent = await get(db, agent_id)
    if agent is None:
        return False
    await db.delete(agent)
    await db.flush()
    return True


# ── 启动时种子 ──


DEFAULT_AGENT_ID = "adf_default"


async def ensure_default(db: AsyncSession) -> AgentDefinition:
    """应用启动时调用。没有智能体则创建默认智能体。"""
    existing = await get(db, DEFAULT_AGENT_ID)
    if existing is not None:
        return existing

    ts = now_ms()
    agent = AgentDefinition(
        id=DEFAULT_AGENT_ID,
        name="默认助手",
        description="通用任务执行",
        system_prompt="",
        skill_names="[]",
        mcp_servers="[]",
        permission_read=1,
        permission_write=1,
        permission_shell=1,
        permission_network=1,
        permission_subagent=1,
        created_at=ts,
        updated_at=ts,
    )
    db.add(agent)
    await db.flush()
    return agent
