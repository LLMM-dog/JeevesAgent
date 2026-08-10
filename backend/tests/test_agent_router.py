"""
智能体 HTTP API 端点测试。
"""

from __future__ import annotations

from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


@pytest_asyncio.fixture
async def client(db: AsyncSession, workspace_id: str) -> Any:
    from app.api import deps
    from app.infra.db.session import get_db
    from app.main import create_app
    from app.modules.agent import agent_service
    from app.modules.agent.tools.base import ToolRegistry

    await agent_service.ensure_default(db)

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[deps.get_registry] = lambda: ToolRegistry()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    app.dependency_overrides.clear()


class TestAgentListAPI:
    async def test_list_empty_returns_default(self, client: AsyncClient) -> None:
        r = await client.get("/api/agents")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        assert data[0]["name"] == "默认助手"

    async def test_list_filters_hidden(self, client: AsyncClient) -> None:
        r1 = await client.post("/api/agents", json={"name": "隐藏智能体"})
        aid = r1.json()["id"]
        await client.patch(
            f"/api/agents/{aid}",
            json={"hidden": True},
        )
        r2 = await client.get("/api/agents?hidden=false")
        assert all(a["id"] != aid for a in r2.json())

    async def test_list_filters_by_skill(self, client: AsyncClient) -> None:
        r = await client.post(
            "/api/agents",
            json={"name": "研究员", "skill_names": ["code-review"]},
        )
        aid = r.json()["id"]
        r2 = await client.get("/api/agents?using_skill=code-review")
        data = r2.json()
        assert len(data) == 1
        assert data[0]["id"] == aid

    async def test_list_filters_by_mcp(self, client: AsyncClient) -> None:
        r = await client.post(
            "/api/agents",
            json={"name": "Github 助手", "mcp_servers": ["github"]},
        )
        aid = r.json()["id"]
        r2 = await client.get("/api/agents?using_mcp=github")
        data = r2.json()
        assert len(data) == 1
        assert data[0]["id"] == aid


class TestAgentCreateAPI:
    async def test_create_basic(self, client: AsyncClient) -> None:
        r = await client.post(
            "/api/agents",
            json={"name": "测试智能体", "description": "用于测试"},
        )
        assert r.status_code == 201
        data = r.json()
        assert data["name"] == "测试智能体"
        assert data["description"] == "用于测试"
        assert data["id"].startswith("adf_")

    async def test_create_with_all_fields(self, client: AsyncClient) -> None:
        r = await client.post(
            "/api/agents",
            json={
                "name": "全能智能体",
                "description": "拥有全部权限",
                "system_prompt": "你是一个全能的助手。",
                "permission_read": True,
                "permission_write": True,
                "permission_shell": True,
                "permission_network": True,
                "permission_subagent": True,
                "verification_enabled": True,
                "strict_mode": True,
                "skill_names": ["code-review"],
                "mcp_servers": ["github"],
            },
        )
        assert r.status_code == 201
        data = r.json()
        assert data["permission_write"] is True
        assert data["permission_shell"] is True
        assert data["verification_enabled"] is True
        assert data["strict_mode"] is True
        assert "code-review" in data["skill_names"]
        assert "github" in data["mcp_servers"]

    async def test_create_rejects_empty_name(self, client: AsyncClient) -> None:
        r = await client.post("/api/agents", json={"name": ""})
        assert r.status_code == 422

    async def test_create_rejects_missing_name(self, client: AsyncClient) -> None:
        r = await client.post("/api/agents", json={})
        assert r.status_code == 422

    async def test_create_defaults_permissions_zero(self, client: AsyncClient) -> None:
        r = await client.post("/api/agents", json={"name": "只读"})
        assert r.status_code == 201
        data = r.json()
        assert data["permission_write"] is False
        assert data["permission_shell"] is False
        assert data["permission_network"] is False
        assert data["permission_subagent"] is False

    async def test_create_with_model_id(self, client: AsyncClient) -> None:
        r = await client.post(
            "/api/agents",
            json={"name": "绑定模型", "model_id": "mdl_test001"},
        )
        assert r.status_code == 201
        data = r.json()
        assert data["model_id"] == "mdl_test001"


class TestAgentGetAPI:
    async def test_get_existing(self, client: AsyncClient) -> None:
        r = await client.post(
            "/api/agents",
            json={"name": "查找我", "description": "用于 GET 测试"},
        )
        aid = r.json()["id"]
        r2 = await client.get(f"/api/agents/{aid}")
        assert r2.status_code == 200
        data = r2.json()
        assert data["name"] == "查找我"
        assert data["description"] == "用于 GET 测试"

    async def test_get_nonexistent_returns_404(self, client: AsyncClient) -> None:
        r = await client.get("/api/agents/adf_nonexistent")
        assert r.status_code == 404

    async def test_get_default_agent(self, client: AsyncClient) -> None:
        r2 = await client.get("/api/agents/adf_default")
        assert r2.status_code == 200
        assert r2.json()["name"] == "默认助手"


class TestAgentUpdateAPI:
    async def test_update_name(self, client: AsyncClient) -> None:
        r = await client.post("/api/agents", json={"name": "旧名字"})
        aid = r.json()["id"]
        r2 = await client.patch(f"/api/agents/{aid}", json={"name": "新名字"})
        assert r2.status_code == 200
        assert r2.json()["name"] == "新名字"

    async def test_update_partial(self, client: AsyncClient) -> None:
        r = await client.post(
            "/api/agents",
            json={"name": "原版", "description": "原始描述", "system_prompt": "旧的提示词"},
        )
        aid = r.json()["id"]
        r2 = await client.patch(
            f"/api/agents/{aid}",
            json={"description": "新描述"},
        )
        assert r2.status_code == 200
        data = r2.json()
        assert data["name"] == "原版"
        assert data["description"] == "新描述"
        assert data["system_prompt"] == "旧的提示词"

    async def test_update_permissions(self, client: AsyncClient) -> None:
        r = await client.post("/api/agents", json={"name": "无权限"})
        aid = r.json()["id"]
        r2 = await client.patch(
            f"/api/agents/{aid}",
            json={"permission_write": True, "permission_shell": True},
        )
        assert r2.status_code == 200
        data = r2.json()
        assert data["permission_write"] is True
        assert data["permission_shell"] is True
        assert data["permission_read"] is True  # 未改的保持不变

    async def test_update_hidden(self, client: AsyncClient) -> None:
        r = await client.post("/api/agents", json={"name": "即将隐藏"})
        aid = r.json()["id"]
        r2 = await client.patch(f"/api/agents/{aid}", json={"hidden": True})
        assert r2.status_code == 200
        assert r2.json()["hidden"] is True
        r3 = await client.get("/api/agents?hidden=false")
        assert aid not in [a["id"] for a in r3.json()]

    async def test_update_verification(self, client: AsyncClient) -> None:
        r = await client.post("/api/agents", json={"name": "验证测试"})
        aid = r.json()["id"]
        r2 = await client.patch(
            f"/api/agents/{aid}",
            json={"verification_enabled": True, "strict_mode": True},
        )
        assert r2.status_code == 200
        data = r2.json()
        assert data["verification_enabled"] is True
        assert data["strict_mode"] is True

    async def test_update_nonexistent_returns_404(self, client: AsyncClient) -> None:
        r = await client.patch(
            "/api/agents/adf_nonexistent",
            json={"name": "不存在"},
        )
        assert r.status_code == 404

    async def test_update_blank_name_rejected(self, client: AsyncClient) -> None:
        r = await client.post("/api/agents", json={"name": "有名字"})
        aid = r.json()["id"]
        r2 = await client.patch(f"/api/agents/{aid}", json={"name": ""})
        assert r2.status_code == 422


class TestAgentDeleteAPI:
    async def test_delete_existing(self, client: AsyncClient) -> None:
        r = await client.post("/api/agents", json={"name": "待删除"})
        aid = r.json()["id"]
        r2 = await client.delete(f"/api/agents/{aid}")
        assert r2.status_code == 204
        r3 = await client.get(f"/api/agents/{aid}")
        assert r3.status_code == 404

    async def test_delete_default_agent_rejected(self, client: AsyncClient) -> None:
        r = await client.delete("/api/agents/adf_default")
        assert r.status_code == 403

    async def test_delete_nonexistent_returns_404(self, client: AsyncClient) -> None:
        r = await client.delete("/api/agents/adf_nonexistent")
        assert r.status_code == 404

    async def test_delete_then_recreate_works(self, client: AsyncClient) -> None:
        r = await client.post("/api/agents", json={"name": "将删重建"})
        aid = r.json()["id"]
        await client.delete(f"/api/agents/{aid}")
        r2 = await client.post("/api/agents", json={"name": "将删重建"})
        assert r2.status_code == 201
        assert r2.json()["id"] != aid

    async def test_deleted_not_in_list(self, client: AsyncClient) -> None:
        r = await client.post("/api/agents", json={"name": "隐身"})
        aid = r.json()["id"]
        await client.delete(f"/api/agents/{aid}")
        r2 = await client.get("/api/agents")
        assert aid not in [a["id"] for a in r2.json()]
