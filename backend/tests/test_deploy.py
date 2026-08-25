"""
部署路由测试：状态端点 + Tailscale 管理（用假 CLI 输出）。
"""

from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


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


class TestDeployStatus:
    async def test_status_fields(self, client: AsyncClient) -> None:
        r = await client.get("/api/deploy/status")
        assert r.status_code == 200
        d = r.json()
        assert d["is_localhost"] is True
        assert d["auth_enabled"] is False
        assert d["port"] > 0
        assert "host" in d


class TestTailscale:
    async def test_not_installed(self, client: AsyncClient, monkeypatch: Any) -> None:
        from app.modules.deploy import tailscale as ts

        monkeypatch.setattr(ts, "_bin", lambda: None)
        r = await client.get("/api/deploy/tailscale")
        assert r.status_code == 200
        assert r.json()["installed"] is False

    async def test_status_parses_json(self, client: AsyncClient, monkeypatch: Any) -> None:
        from app.modules.deploy import tailscale as ts

        monkeypatch.setattr(ts, "_bin", lambda: "/fake/tailscale")
        fake = {
            "status --json": '{"BackendState":"Running","Self":{"DNSName":"laptop.my-tailnet.ts.net.","TailscaleIPs":["100.64.0.1"]}}',
            "serve status --json": '{"TCP":{"443":{"HTTPS":true,"Handlers":{"/":{"Proxy":"http://127.0.0.1:9000"}}}}}',
            "funnel status --json": '{"TCP":{"443":{"TailscaleFunnel":{"Enabled":false}}}}',
        }

        async def fake_run(args, timeout_s=8.0):
            key = " ".join(args)
            return (0, fake.get(key, ""))

        monkeypatch.setattr(ts, "_run", fake_run)
        r = await client.get("/api/deploy/tailscale")
        assert r.status_code == 200
        d = r.json()
        assert d["installed"] is True
        assert d["logged_in"] is True
        assert d["device_name"] == "laptop.my-tailnet.ts.net"
        assert d["serve"]["serve_on"] is True
        assert d["serve"]["funnel_on"] is False

    async def test_start_serve(self, client: AsyncClient, monkeypatch: Any) -> None:
        from app.modules.deploy import tailscale as ts

        calls: list[list[str]] = []

        async def fake_run(args, timeout_s=8.0):
            calls.append(args)
            if args[:2] == ["status", "--json"]:
                return (0, '{"BackendState":"Running"}')
            if args[:2] == ["serve", "--bg"]:
                return (0, "ok")
            return (0, "{}")

        monkeypatch.setattr(ts, "_bin", lambda: "/fake/tailscale")
        monkeypatch.setattr(ts, "_run", fake_run)
        r = await client.post("/api/deploy/tailscale/serve", json={"port": 9000})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert ["serve", "--bg", "9000"] in calls

    async def test_start_serve_failure(self, client: AsyncClient, monkeypatch: Any) -> None:
        from app.modules.deploy import tailscale as ts

        async def fake_run(args, timeout_s=8.0):
            return (1, "not logged in")

        monkeypatch.setattr(ts, "_bin", lambda: "/fake/tailscale")
        monkeypatch.setattr(ts, "_run", fake_run)
        r = await client.post("/api/deploy/tailscale/serve", json={"port": 9000})
        assert r.status_code == 200
        assert r.json()["ok"] is False
        assert "not logged in" in r.json()["detail"]

    async def test_stop_funnel(self, client: AsyncClient, monkeypatch: Any) -> None:
        from app.modules.deploy import tailscale as ts

        calls: list[list[str]] = []

        async def fake_run(args, timeout_s=8.0):
            calls.append(args)
            if args[:2] == ["status", "--json"]:
                return (0, '{"BackendState":"Running"}')
            return (0, "{}")

        monkeypatch.setattr(ts, "_bin", lambda: "/fake/tailscale")
        monkeypatch.setattr(ts, "_run", fake_run)
        r = await client.post("/api/deploy/tailscale/funnel/stop")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert ["funnel", "off"] in calls


class TestDeploySettings:
    async def test_list_deploy_settings(self, client: AsyncClient) -> None:
        r = await client.get("/api/deploy/settings")
        assert r.status_code == 200
        keys = [i["key"] for i in r.json()["items"]]
        assert "security.auth_enabled" in keys
        assert "app.host" in keys
        assert "app.port" in keys
        assert "memory.enabled" not in keys

    async def test_update_auth_enabled_hot(self, client: AsyncClient) -> None:
        from app.core.config import settings

        old = settings.security.auth_enabled
        try:
            r = await client.put("/api/deploy/settings", json={"values": {"security.auth_enabled": True}})
            assert r.status_code == 200
            assert settings.security.auth_enabled is True
            item = next(i for i in r.json()["items"] if i["key"] == "security.auth_enabled")
            assert item["value"] is True
        finally:
            settings.security.auth_enabled = old

    async def test_reject_unknown_key(self, client: AsyncClient) -> None:
        r = await client.put("/api/deploy/settings", json={"values": {"security.encryption_key": "x"}})
        assert r.status_code == 400

    async def test_port_requires_restart_flag(self, client: AsyncClient) -> None:
        r = await client.get("/api/deploy/settings")
        item = next(i for i in r.json()["items"] if i["key"] == "app.port")
        assert item["restart"] is True


@pytest_asyncio.fixture
async def auth_client() -> Any:
    """带 app.state.db_sessionmaker 的客户端 —— 鉴权中间件会校验会话。"""
    from app.api import deps
    from app.infra.db.base import Base
    from app.infra.db.session import get_db
    from app.main import create_app
    from app.modules.agent.tools.base import ToolRegistry
    from sqlalchemy import event

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    def _pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    event.listen(engine.sync_engine, "connect", _pragmas)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker() as session:
        app = create_app()
        app.state.db_sessionmaker = maker
        app.dependency_overrides[get_db] = lambda: session
        app.dependency_overrides[deps.get_registry] = lambda: ToolRegistry()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c
    app.dependency_overrides.clear()
    await engine.dispose()


class TestEnableAuth:
    async def test_enable_auth_creates_admin_and_logs_in(self, auth_client: AsyncClient) -> None:
        from app.core.config import settings

        old = settings.security.auth_enabled
        try:
            settings.security.auth_enabled = False
            r = await auth_client.post("/api/deploy/enable-auth", json={"username": "boss", "password": "boss-pass-123"})
            assert r.status_code == 200
            assert r.json()["username"] == "boss"
            assert r.json()["token"]
            assert settings.security.auth_enabled is True
            assert "jeeves_session" in r.headers.get("set-cookie", "")
        finally:
            settings.security.auth_enabled = old

    async def test_enable_auth_twice_conflicts(self, auth_client: AsyncClient) -> None:
        from app.core.config import settings

        old = settings.security.auth_enabled
        try:
            settings.security.auth_enabled = False
            await auth_client.post("/api/deploy/enable-auth", json={"username": "boss", "password": "boss-pass-123"})
            r = await auth_client.post("/api/deploy/enable-auth", json={"username": "boss2", "password": "boss-pass-456"})
            assert r.status_code == 409
        finally:
            settings.security.auth_enabled = old

    async def test_short_password_rejected(self, auth_client: AsyncClient) -> None:
        r = await auth_client.post("/api/deploy/enable-auth", json={"username": "boss", "password": "short"})
        assert r.status_code == 422


class TestTailscaleInstallLogin:
    async def test_install(self, client: AsyncClient, monkeypatch: Any) -> None:
        from app.modules.deploy import tailscale as ts

        monkeypatch.setattr(ts, "install", _fake(True, "安装完成"))
        monkeypatch.setattr(ts, "get_status", _fake_status)
        r = await client.post("/api/deploy/tailscale/install")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert "安装完成" in r.json()["detail"]

    async def test_login_returns_url(self, client: AsyncClient, monkeypatch: Any) -> None:
        from app.modules.deploy import tailscale as ts

        monkeypatch.setattr(ts, "start_login", _fake(True, "https://login.tailscale.com/a/abc"))
        monkeypatch.setattr(ts, "get_status", _fake_status)
        r = await client.post("/api/deploy/tailscale/login")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert "login.tailscale.com" in r.json()["detail"]


def _fake(ok: bool, detail: Any):
    async def _inner(*_a: Any, **_k: Any):
        return ok, detail

    return _inner


async def _fake_status() -> dict[str, Any]:
    return {}


class TestPortableTailscale:
    def test_extract_tgz(self, tmp_path: Any, monkeypatch: Any) -> None:
        import io
        import tarfile

        from app.modules.deploy import tailscale as ts

        tar_path = tmp_path / "x.tgz"
        with tarfile.open(tar_path, "w:gz") as tf:
            for name in ("tailscale", "tailscaled"):
                data = b"#!/bin/sh\necho ok\n"
                info = tarfile.TarInfo(name=f"tailscale_1.0_amd64/{name}")
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        bin_dir = tmp_path / "bin"
        monkeypatch.setattr(ts, "_BIN_DIR", bin_dir)
        ok, err = ts._extract_tgz(tar_path)
        assert ok, err
        assert (bin_dir / ts._exe("tailscale")).exists()
        assert (bin_dir / ts._exe("tailscaled")).exists()

    def test_socket_args_when_bundled(self, tmp_path: Any, monkeypatch: Any) -> None:
        from app.modules.deploy import tailscale as ts

        import sys as _sys

        monkeypatch.setattr(ts, "_system_bin", lambda: None)
        monkeypatch.setattr(ts, "_bundled_bin", lambda: tmp_path / "bin" / "tailscale")
        assert ts._is_bundled() is True
        if _sys.platform == "win32":
            # Windows 用默认命名管道，不带 --socket
            assert ts._socket_args() == []
        else:
            assert "--socket" in ts._socket_args()

    async def test_ensure_bundled_uses_system(self, monkeypatch: Any) -> None:
        from app.modules.deploy import tailscale as ts

        monkeypatch.setattr(ts, "_system_bin", lambda: "/usr/bin/tailscale")
        ok, detail = await ts.ensure_bundled()
        assert ok
        assert "系统" in detail

    async def test_ensure_bundled_downloads(self, tmp_path: Any, monkeypatch: Any) -> None:
        import io
        import tarfile

        from app.modules.deploy import tailscale as ts

        bundled = tmp_path / ".tailscale"
        monkeypatch.setattr(ts, "_system_bin", lambda: None)
        monkeypatch.setattr(ts, "_BUNDLED_DIR", bundled)
        monkeypatch.setattr(ts, "_BIN_DIR", bundled / "bin")
        monkeypatch.setattr(ts, "_DL_DIR", bundled / "download")

        async def fake_asset() -> tuple[str, str]:
            return "http://example.com/tailscale_1.0_amd64.tgz", "tgz"

        async def fake_download(url: str, dest: Any) -> tuple[bool, str]:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(dest, "w:gz") as tf:
                for name in ("tailscale", "tailscaled"):
                    data = b"x"
                    info = tarfile.TarInfo(name=f"d/{name}")
                    info.size = len(data)
                    tf.addfile(info, io.BytesIO(data))
            return True, ""

        monkeypatch.setattr(ts, "_asset_url", fake_asset)
        monkeypatch.setattr(ts, "_download", fake_download)
        ok, detail = await ts.ensure_bundled()
        assert ok, detail
        assert (bundled / "bin" / ts._exe("tailscale")).exists()
