"""
远程访问鉴权：密码哈希、会话、登录/登出/改密、用户管理、中间件门禁、CSRF、限流。

## 测试策略

服务层直接打函数（不经过 HTTP）；API 层用 ASGITransport + 自建内存库。
鉴权默认关闭（与生产默认一致），本文件里显式开启并保证测试后还原。
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from app.core.config import settings
from app.infra.db.base import Base
from app.modules.auth import service as auth_svc
from app.modules.auth.models import AuthSession, User
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# ─────────────────────────── 服务层 ───────────────────────────


class TestPasswordHash:
    def test_roundtrip(self) -> None:
        h = auth_svc.hash_password("correct horse battery staple")
        assert h.startswith("pbkdf2_sha256$600000$")
        assert auth_svc.verify_password("correct horse battery staple", h)

    def test_wrong_password_fails(self) -> None:
        h = auth_svc.hash_password("right-password")
        assert not auth_svc.verify_password("wrong-password", h)

    def test_salt_makes_hashes_differ(self) -> None:
        assert auth_svc.hash_password("same-pass") != auth_svc.hash_password("same-pass")

    def test_malformed_stored_hash(self) -> None:
        assert not auth_svc.verify_password("x", "garbage")
        assert not auth_svc.verify_password("x", "")
        assert not auth_svc.verify_password("x", "md5$1000$c2FsdA==$aGVsbG8=")

    def test_short_password_rejected(self) -> None:
        with pytest.raises(ValueError):
            auth_svc.hash_password("short")


@pytest.mark.asyncio
class TestSessionService:
    async def test_create_and_validate(self, db: AsyncSession) -> None:
        u = await auth_svc.create_user(db, "alice", "password123")
        raw = await auth_svc.create_session(db, u.id)
        found = await auth_svc.get_user_by_session(db, raw)
        assert found is not None and found.id == u.id

    async def test_unknown_token(self, db: AsyncSession) -> None:
        assert await auth_svc.get_user_by_session(db, "no-such-token") is None

    async def test_revoke(self, db: AsyncSession) -> None:
        u = await auth_svc.create_user(db, "bob", "password123")
        raw = await auth_svc.create_session(db, u.id)
        await auth_svc.revoke_session(db, raw)
        assert await auth_svc.get_user_by_session(db, raw) is None

    async def test_disabled_user_cannot_login(self, db: AsyncSession) -> None:
        u = await auth_svc.create_user(db, "carol", "password123", enabled=False)
        raw = await auth_svc.create_session(db, u.id)
        assert await auth_svc.get_user_by_session(db, raw) is None

    async def test_token_hash_only_stored(self, db: AsyncSession) -> None:
        """库里绝不能存原始 token。"""
        u = await auth_svc.create_user(db, "dave", "password123")
        raw = await auth_svc.create_session(db, u.id)
        rows = (await db.execute(select(AuthSession))).scalars().all()
        assert len(rows) == 1
        assert raw not in rows[0].token_hash
        assert auth_svc._hash_token(raw) == rows[0].token_hash

    async def test_revoke_user_sessions_keeps_current(self, db: AsyncSession) -> None:
        u = await auth_svc.create_user(db, "erin", "password123")
        raw1 = await auth_svc.create_session(db, u.id)
        raw2 = await auth_svc.create_session(db, u.id)
        await auth_svc.revoke_user_sessions(db, u.id, keep=raw1)
        assert await auth_svc.get_user_by_session(db, raw1) is not None
        assert await auth_svc.get_user_by_session(db, raw2) is None


class TestRateLimiter:
    def test_blocks_after_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        auth_svc._failures.clear()
        monkeypatch.setattr(settings.security, "login_rate_limit", 3)
        try:
            assert not auth_svc.login_blocked("1.2.3.4")
            auth_svc.record_login_failure("1.2.3.4")
            auth_svc.record_login_failure("1.2.3.4")
            auth_svc.record_login_failure("1.2.3.4")
            assert auth_svc.login_blocked("1.2.3.4")
            # 别的 IP 不受影响
            assert not auth_svc.login_blocked("5.6.7.8")
        finally:
            auth_svc._failures.clear()

    def test_reset(self) -> None:
        auth_svc._failures.clear()
        auth_svc.record_login_failure("9.9.9.9")
        auth_svc.reset_login_failures("9.9.9.9")
        assert not auth_svc.login_blocked("9.9.9.9")


@pytest.mark.asyncio
class TestBootstrapAdmin:
    async def test_creates_when_enabled_and_empty(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings.security, "auth_enabled", True)
        monkeypatch.setattr(settings.security, "admin_username", "root")
        monkeypatch.setattr(settings.security, "admin_password", "bootstrap-pass")
        pw = await auth_svc.ensure_bootstrap_admin(db)
        assert pw is None  # 配了密码不返回
        u = await auth_svc.get_user_by_name(db, "root")
        assert u is not None and u.is_admin

    async def test_skips_when_users_exist(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings.security, "auth_enabled", True)
        await auth_svc.create_user(db, "existing", "password123")
        assert await auth_svc.ensure_bootstrap_admin(db) is None
        assert (await db.execute(select(User))).scalars().all().__len__() == 1

    async def test_skips_when_disabled(self, db: AsyncSession) -> None:
        assert await auth_svc.ensure_bootstrap_admin(db) is None
        assert (await db.execute(select(User))).scalars().all() == []


# ─────────────────────────── API 层 ───────────────────────────


def _pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


@pytest_asyncio.fixture
async def auth_env() -> AsyncIterator[tuple[AsyncClient, async_sessionmaker[AsyncSession]]]:
    """
    自建内存库 + 开启鉴权 + 预置 admin 用户的应用。
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    event.listen(engine.sync_engine, "connect", _pragmas)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    from app.api import deps
    from app.infra.db.session import get_db
    from app.main import create_app
    from app.modules.agent.tools.base import ToolRegistry

    async with maker() as db:
        await auth_svc.create_user(db, "admin", "admin-pass-123", is_admin=True)
        session = db

    old = settings.security.auth_enabled
    settings.security.auth_enabled = True
    try:
        app = create_app()
        app.state.db_sessionmaker = maker
        app.dependency_overrides[get_db] = lambda: session
        app.dependency_overrides[deps.get_registry] = lambda: ToolRegistry()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c, maker
    finally:
        settings.security.auth_enabled = old
        app.dependency_overrides.clear()
        auth_svc._failures.clear()
        await engine.dispose()


async def _login(client: AsyncClient, username: str = "admin", password: str = "admin-pass-123") -> AsyncClient:
    """返回带会话 cookie 的客户端。

    【不要手动 client.cookies.set】—— httpx 的 jar 会自动处理响应里的
    Set-Cookie（替换同名 cookie）；手动 set 是【追加】，会造成同名多值、
    CookieConflict，而请求时旧 cookie 还跟着走。
    """
    r = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return client


class TestLoginAPI:
    async def test_login_success_sets_cookie(self, auth_env: Any) -> None:
        client, _ = auth_env
        r = await client.post(
            "/api/auth/login", json={"username": "admin", "password": "admin-pass-123"}
        )
        assert r.status_code == 200
        assert r.json()["authenticated"] is True
        set_cookie = r.headers.get("set-cookie", "")
        assert "jeeves_session=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=strict" in set_cookie

    async def test_login_wrong_password(self, auth_env: Any) -> None:
        client, _ = auth_env
        r = await client.post(
            "/api/auth/login", json={"username": "admin", "password": "wrong"}
        )
        assert r.status_code == 401

    async def test_login_unknown_user(self, auth_env: Any) -> None:
        client, _ = auth_env
        r = await client.post(
            "/api/auth/login", json={"username": "ghost", "password": "whatever"}
        )
        assert r.status_code == 401

    async def test_login_rate_limited(
        self, auth_env: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = auth_env
        monkeypatch.setattr(settings.security, "login_rate_limit", 2)
        auth_svc._failures.clear()
        for _ in range(2):
            r = await client.post(
                "/api/auth/login", json={"username": "xx", "password": "yy"}
            )
            assert r.status_code == 401
        r = await client.post(
            "/api/auth/login", json={"username": "admin", "password": "admin-pass-123"}
        )
        assert r.status_code == 429
        assert r.json()["detail"]["code"] == "rate_limited"


class TestAuthMiddleware:
    async def test_api_requires_login(self, auth_env: Any) -> None:
        client, _ = auth_env
        r = await client.get("/api/sessions")
        assert r.status_code == 401

    async def test_api_allows_with_cookie(self, auth_env: Any) -> None:
        client, _ = auth_env
        await _login(client)
        r = await client.get("/api/sessions")
        assert r.status_code == 200

    async def test_api_allows_with_bearer(self, auth_env: Any) -> None:
        client, _ = auth_env
        r = await client.post(
            "/api/auth/login", json={"username": "admin", "password": "admin-pass-123"}
        )
        token = r.json()["token"]
        assert token
        r2 = await client.get(
            "/api/sessions", headers={"Authorization": f"Bearer {token}"}
        )
        assert r2.status_code == 200

    async def test_health_is_public(self, auth_env: Any) -> None:
        client, _ = auth_env
        r = await client.get("/api/health")
        assert r.status_code in (200, 404)  # 无论路由是否存在都不能 401

    async def test_me_is_public_and_reflects_state(self, auth_env: Any) -> None:
        client, _ = auth_env
        r = await client.get("/api/auth/me")
        assert r.status_code == 200
        assert r.json() == {
            "auth_enabled": True,
            "authenticated": False,
            "username": "",
            "is_admin": False,
            "session_ttl_days": 30,
        }
        await _login(client)
        r2 = await client.get("/api/auth/me")
        assert r2.json()["authenticated"] is True
        assert r2.json()["username"] == "admin"

    async def test_static_paths_are_public(self, auth_env: Any) -> None:
        """登录页本身必须能加载 —— 前端静态资源不鉴权。"""
        client, _ = auth_env
        r = await client.get("/")
        assert r.status_code in (200, 404)  # 没构建 dist 时 404，但绝不 401

    async def test_csrf_rejects_cross_origin(self, auth_env: Any) -> None:
        client, _ = auth_env
        await _login(client)
        r = await client.post(
            "/api/sessions",
            json={},
            headers={"origin": "http://evil.example"},
        )
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "csrf_rejected"

    async def test_csrf_allows_same_origin(self, auth_env: Any) -> None:
        client, _ = auth_env
        await _login(client)
        r = await client.post(
            "/api/sessions",
            json={},
            headers={"origin": "http://test"},
        )
        assert r.status_code != 403

    async def test_security_headers_present(self, auth_env: Any) -> None:
        client, _ = auth_env
        r = await client.get("/api/auth/me")
        assert r.headers.get("x-content-type-options") == "nosniff"
        assert r.headers.get("x-frame-options") == "DENY"
        assert "default-src 'self'" in r.headers.get("content-security-policy", "")


class TestPasswordAndUsersAPI:
    async def test_change_password_flow(self, auth_env: Any) -> None:
        client, _ = auth_env
        await _login(client)
        r = await client.post(
            "/api/auth/password",
            json={"old_password": "admin-pass-123", "new_password": "new-pass-456"},
        )
        assert r.status_code == 200
        # 旧密码失效
        r = await client.post(
            "/api/auth/login", json={"username": "admin", "password": "admin-pass-123"}
        )
        assert r.status_code == 401
        r = await client.post(
            "/api/auth/login", json={"username": "admin", "password": "new-pass-456"}
        )
        assert r.status_code == 200

    async def test_change_password_wrong_old(self, auth_env: Any) -> None:
        client, _ = auth_env
        await _login(client)
        r = await client.post(
            "/api/auth/password",
            json={"old_password": "nope", "new_password": "new-pass-456"},
        )
        assert r.status_code == 401

    async def test_list_create_delete_users(self, auth_env: Any) -> None:
        client, _ = auth_env
        await _login(client)
        r = await client.get("/api/auth/users")
        assert r.status_code == 200
        assert len(r.json()) == 1

        r = await client.post(
            "/api/auth/users",
            json={"username": "friend", "password": "friend-pass-1", "is_admin": False},
        )
        assert r.status_code == 201
        uid = r.json()["id"]

        # 非管理员账号看不到用户列表
        await client.post(
            "/api/auth/login", json={"username": "friend", "password": "friend-pass-1"}
        )
        r = await client.get("/api/auth/users")
        assert r.status_code == 403

        # 回到 admin 删除
        await _login(client)
        r = await client.delete(f"/api/auth/users/{uid}")
        assert r.status_code == 200
        r = await client.get("/api/auth/users")
        assert len(r.json()) == 1

    async def test_cannot_delete_self_or_last_admin(self, auth_env: Any) -> None:
        client, _ = auth_env
        await _login(client)
        users = (await client.get("/api/auth/users")).json()
        me = users[0]["id"]
        r = await client.delete(f"/api/auth/users/{me}")
        assert r.status_code == 400

    async def test_users_api_disabled_without_auth(self, db: AsyncSession) -> None:
        """鉴权未开启时用户管理接口明确报错，而不是 500。"""
        from app.api import deps
        from app.main import create_app
        from app.modules.agent.tools.base import ToolRegistry

        old = settings.security.auth_enabled
        settings.security.auth_enabled = False
        try:
            app = create_app()
            app.dependency_overrides[deps.get_registry] = lambda: ToolRegistry()
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as c:
                r = await c.get("/api/auth/users")
                assert r.status_code == 400
                assert r.json()["detail"]["code"] == "auth_disabled"
        finally:
            settings.security.auth_enabled = old
            app.dependency_overrides.clear()


class TestAuthDisabledMode:
    async def test_everything_open(self, db: AsyncSession) -> None:
        """默认模式（无鉴权）行为与之前完全一致。"""
        from app.api import deps
        from app.main import create_app
        from app.modules.agent.tools.base import ToolRegistry

        old = settings.security.auth_enabled
        settings.security.auth_enabled = False
        try:
            app = create_app()
            app.dependency_overrides[deps.get_registry] = lambda: ToolRegistry()
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as c:
                r = await c.get("/api/sessions")
                assert r.status_code == 200
                r = await c.get("/api/auth/me")
                assert r.status_code == 200
                assert r.json()["auth_enabled"] is False
        finally:
            settings.security.auth_enabled = old
            app.dependency_overrides.clear()


class TestCheckConfig:
    def test_refuses_non_localhost_without_auth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.main import check_config

        old_host = settings.app.host
        old_auth = settings.security.auth_enabled
        try:
            settings.app.host = "0.0.0.0"
            settings.security.auth_enabled = False
            with pytest.raises(SystemExit, match="未开启鉴权"):
                check_config()
        finally:
            settings.app.host = old_host
            settings.security.auth_enabled = old_auth

    def test_allows_non_localhost_with_auth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.main import check_config

        old_host = settings.app.host
        old_auth = settings.security.auth_enabled
        try:
            settings.app.host = "0.0.0.0"
            settings.security.auth_enabled = True
            check_config()  # 不抛
        finally:
            settings.app.host = old_host
            settings.security.auth_enabled = old_auth
