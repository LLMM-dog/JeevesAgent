"""
智能体管理 API。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.session import get_db
from app.modules.agent import agent_service

router = APIRouter(prefix="/api/agents", tags=["agents"])


# ── 请求/响应模型 ──


class AgentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    system_prompt: str = ""
    model_id: str | None = None
    skill_names: list[str] = []
    mcp_servers: list[str] = []
    permission_read: bool = True
    permission_write: bool = False
    permission_shell: bool = False
    permission_network: bool = False
    permission_subagent: bool = False
    extra_llm_params: str = ""


class AgentUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = None
    system_prompt: str | None = None
    model_id: str | None = None
    skill_names: list[str] | None = None
    mcp_servers: list[str] | None = None
    permission_read: bool | None = None
    permission_write: bool | None = None
    permission_shell: bool | None = None
    permission_network: bool | None = None
    permission_subagent: bool | None = None
    hidden: bool | None = None
    extra_llm_params: str | None = None


def _to_dict(agent: Any) -> dict[str, Any]:
    import json

    return {
        "id": agent.id,
        "name": agent.name,
        "description": agent.description,
        "avatar": agent.avatar,
        "system_prompt": agent.system_prompt,
        "model_id": agent.model_id,
        "skill_names": json.loads(agent.skill_names) if agent.skill_names else [],
        "mcp_servers": json.loads(agent.mcp_servers) if agent.mcp_servers else [],
        "permission_read": bool(agent.permission_read),
        "permission_write": bool(agent.permission_write),
        "permission_shell": bool(agent.permission_shell),
        "permission_network": bool(agent.permission_network),
        "permission_subagent": bool(agent.permission_subagent),
        "extra_llm_params": agent.extra_llm_params or "",
        "hidden": bool(agent.hidden),
        "is_default": agent.id == agent_service.DEFAULT_AGENT_ID,
        "created_at": agent.created_at,
        "updated_at": agent.updated_at,
    }


# ── 端点 ──


@router.get("")
async def list_agents(
    hidden: bool | None = Query(None, description="过滤可见性：true=只隐藏, false=只可见, 不传=全部"),
    using_skill: str | None = Query(None, description="查询使用指定 skill 的智能体"),
    using_mcp: str | None = Query(None, description="查询使用指定 MCP 的智能体"),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    agents = await agent_service.list_all(
        db, hidden=hidden, using_skill=using_skill, using_mcp=using_mcp
    )
    return [_to_dict(a) for a in agents]


@router.post("", status_code=201)
async def create_agent(
    body: AgentCreate, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    agent = await agent_service.create(
        db,
        name=body.name,
        description=body.description,
        system_prompt=body.system_prompt,
        model_id=body.model_id,
        skill_names=body.skill_names,
        mcp_servers=body.mcp_servers,
        permission_read=body.permission_read,
        permission_write=body.permission_write,
        permission_shell=body.permission_shell,
        permission_network=body.permission_network,
        permission_subagent=body.permission_subagent,
        extra_llm_params=body.extra_llm_params,
    )
    await db.commit()
    return _to_dict(agent)


@router.get("/{agent_id}")
async def get_agent(
    agent_id: str, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    agent = await agent_service.get(db, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="智能体不存在")
    return _to_dict(agent)


@router.patch("/{agent_id}")
async def update_agent(
    agent_id: str,
    body: AgentUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="没有提供要更新的字段")
    agent = await agent_service.update(db, agent_id, **updates)
    if agent is None:
        raise HTTPException(status_code=404, detail="智能体不存在")
    await db.commit()
    return _to_dict(agent)


@router.delete("/{agent_id}")
async def delete_agent(
    agent_id: str, db: AsyncSession = Depends(get_db)
) -> Response:
    if agent_id == agent_service.DEFAULT_AGENT_ID:
        raise HTTPException(status_code=403, detail="默认智能体不可删除")
    ok = await agent_service.delete(db, agent_id)
    if not ok:
        raise HTTPException(status_code=404, detail="智能体不存在")
    await db.commit()
    return Response(status_code=204)
