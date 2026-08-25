"""
工作区管理 + 容器执行环境。
"""

from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


@pytest_asyncio.fixture
async def client(db: AsyncSession, workspace_id: str) -> Any:
    from app.api import deps
    from app.infra.db.session import get_db
    from app.main import create_app
    from app.modules.agent.tools.base import ToolRegistry

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[deps.get_registry] = lambda: ToolRegistry()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    app.dependency_overrides.clear()


class TestWorkspaceCrud:
    async def test_list_has_default(self, client: AsyncClient) -> None:
        r = await client.get("/api/workspaces")
        assert r.status_code == 200
        items = r.json()
        assert len(items) >= 1
        assert items[0]["is_default"] is True
        assert items[0]["sandbox_backend"] == "local"

    async def test_create_local_workspace(self, client: AsyncClient) -> None:
        r = await client.post("/api/workspaces", json={"name": "项目A", "root_path": "D:/tmp/proj-a"})
        assert r.status_code == 201
        assert r.json()["sandbox_backend"] == "local"

    async def test_create_docker_workspace(self, client: AsyncClient) -> None:
        r = await client.post("/api/workspaces", json={"name": "隔离区", "root_path": "D:/tmp/iso", "sandbox_backend": "docker", "docker_container": "iso-box", "docker_image": "python:3.12-slim"})
        assert r.status_code == 201
        d = r.json()
        assert d["sandbox_backend"] == "docker"
        assert d["docker_container"] == "iso-box"

    async def test_container_name_duplicate_rejected(self, client: AsyncClient) -> None:
        await client.post("/api/workspaces", json={"name": "A", "root_path": "D:/tmp/a", "sandbox_backend": "docker", "docker_container": "box-1"})
        r = await client.post("/api/workspaces", json={"name": "B", "root_path": "D:/tmp/b", "sandbox_backend": "docker", "docker_container": "box-1"})
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "container_name_taken"

    async def test_container_name_invalid(self, client: AsyncClient) -> None:
        r = await client.post("/api/workspaces", json={"name": "X", "root_path": "D:/tmp/x", "sandbox_backend": "docker", "docker_container": "-bad-name"})
        assert r.status_code == 400

    async def test_patch_backend_and_container(self, client: AsyncClient) -> None:
        ws = (await client.post("/api/workspaces", json={"name": "P", "root_path": "D:/tmp/p"})).json()
        r = await client.patch(
            f"/api/workspaces/{ws["id"]}",
            json={"sandbox_backend": "docker", "docker_container": "p-box", "docker_network": "bridge"},
        )
        assert r.status_code == 200
        assert r.json()["sandbox_backend"] == "docker"
        assert r.json()["docker_container"] == "p-box"
        assert r.json()["docker_network"] == "bridge"

    async def test_delete_workspace(self, client: AsyncClient) -> None:
        ws = (await client.post("/api/workspaces", json={"name": "D", "root_path": "D:/tmp/d"})).json()
        r = await client.delete(f"/api/workspaces/{ws["id"]}")
        assert r.status_code == 200
        r = await client.get("/api/workspaces")
        assert all(i["id"] != ws["id"] for i in r.json())

    async def test_cannot_delete_default(self, client: AsyncClient) -> None:
        items = (await client.get("/api/workspaces")).json()
        default = next(i for i in items if i["is_default"])
        r = await client.delete(f"/api/workspaces/{default["id"]}")
        assert r.status_code == 400
