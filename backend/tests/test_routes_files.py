"""
文件管理 API 测试。
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
    from app.modules.agent.tools.base import ToolRegistry

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[deps.get_registry] = lambda: ToolRegistry()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    app.dependency_overrides.clear()


class TestWhitelistAPI:
    async def test_list_empty(self, client: AsyncClient) -> None:
        r = await client.get("/api/whitelist")
        assert r.status_code == 200
        data = r.json()
        assert "items" in data or isinstance(data, list)

    async def test_list_with_session_filter(self, client: AsyncClient) -> None:
        r = await client.get("/api/whitelist?session_id=ses_nonexistent")
        assert r.status_code == 200

    async def test_add_then_list(self, client: AsyncClient) -> None:
        r = await client.post(
            "/api/whitelist",
            json={"path": "C:\\temp\\test-dir", "can_write": False},
        )
        assert r.status_code == 201
        data = r.json()
        assert data["path"] in ("/tmp/test-dir", "C:\\temp\\test-dir", "C:\\Temp\\test-dir")
        assert data["can_write"] is False

        r2 = await client.get("/api/whitelist")
        assert r2.status_code == 200

    async def test_add_write_then_delete(self, client: AsyncClient) -> None:
        r = await client.post(
            "/api/whitelist",
            json={"path": "C:\\temp\\write-dir", "can_write": True},
        )
        assert r.status_code == 201
        item_id = r.json()["id"]
        r2 = await client.delete(f"/api/whitelist/{item_id}")
        assert r2.status_code == 200

    async def test_delete_nonexistent(self, client: AsyncClient) -> None:
        r = await client.delete("/api/whitelist/pth_nonexistent")
        assert r.status_code in (200, 404)

    async def test_patch_mode(self, client: AsyncClient) -> None:
        r = await client.post(
            "/api/whitelist",
            json={"path": "C:\\temp\\mode-test", "can_write": False},
        )
        item_id = r.json()["id"]
        r2 = await client.patch(
            f"/api/whitelist/{item_id}",
            json={"can_write": True},
        )
        assert r2.status_code == 200
        data = r2.json()
        assert data["can_write"] is True

    async def test_add_empty_path_rejected(self, client: AsyncClient) -> None:
        r = await client.post(
            "/api/whitelist",
            json={"path": ""},
        )
        assert r.status_code == 422

    async def test_patch_nonexistent_returns_404(self, client: AsyncClient) -> None:
        r = await client.patch(
            "/api/whitelist/pth_nonexistent",
            json={"can_write": True},
        )
        assert r.status_code == 404


class TestBrowseAPI:
    async def test_browse_root(self, client: AsyncClient) -> None:
        r = await client.get("/api/browse")
        assert r.status_code == 200
        data = r.json()
        assert "entries" in data

    async def test_browse_with_path(self, client: AsyncClient) -> None:
        r = await client.get("/api/browse?path=C:\\")
        assert r.status_code == 200

    async def test_browse_dirs_only(self, client: AsyncClient) -> None:
        r = await client.get("/api/browse?dirs_only=true")
        assert r.status_code == 200

    async def test_browse_without_dirs_only(self, client: AsyncClient) -> None:
        r = await client.get("/api/browse?dirs_only=false")
        assert r.status_code == 200
