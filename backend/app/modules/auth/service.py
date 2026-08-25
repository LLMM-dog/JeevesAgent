
"""
鉴权核心逻辑：密码哈希、会话签发/校验/吊销、登录限流、管理员引导。

所有函数不依赖 FastAPI —— 路由和中间件都只调这里，
测试可以直接打服务层，不需要起 HTTP。
"""

import base64
import hashlib
import hmac
import secrets
from collections import defaultdict, deque
from time import monotonic

import structlog
from fastapi import Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.ids import path_id
from app.core.time import now_ms
from app.modules.auth.models import AuthSession, User

log = structlog.get_logger(__name__)

# PBKDF2 参数。60 万次迭代：OpenSSL 实现下约 0.1~0.3s/次，
# 足够拖慢离线爆破，又不至于让每次登录都卡顿。
_ITERATIONS = 600_000
_ALGO = "pbkdf2_sha256"

MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 128
USERNAME_RE = "^[a-zA-Z0-9_-]{2,32}$"


# ─────────────────────────── 密码 ───────────────────────────


def hash_password(password: str) -> str:
    """加盐 PBKDF2 哈希。输出自描述格式，未来换算法可平滑迁移。"""
    if not (MIN_PASSWORD_LEN <= len(password) <= MAX_PASSWORD_LEN):
        raise ValueError(
            f"密码长度需在 {MIN_PASSWORD_LEN}~{MAX_PASSWORD_LEN} 个字符之间"
        )
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _ITERATIONS
    )
    return f"{_ALGO}${_ITERATIONS}$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    """常数时间比较，防时序侧信道。格式不符一律 False。"""
    try:
        algo, iters_s, salt_b64, hash_b64 = stored.split("$")
        iterations = int(iters_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    if algo != _ALGO:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(digest, expected)


# ─────────────────────────── 会话 ───────────────────────────


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_session(
    db: AsyncSession,
    user_id: str,
    *,
    ip: str = "",
    user_agent: str = "",
) -> str:
    """签发会话，返回【原始 token】—— 只此一次，调用方负责写进 cookie。"""
    raw = secrets.token_urlsafe(32)
    ttl_ms = settings.security.session_ttl_days * 86_400_000
    db.add(
        AuthSession(
            id=path_id(),
            user_id=user_id,
            token_hash=_hash_token(raw),
            expires_at=now_ms() + ttl_ms,
            created_ip=ip[:64],
            user_agent=user_agent[:500],
        )
    )
    await db.commit()
    return raw


async def get_user_by_session(db: AsyncSession, raw_token: str) -> User | None:
    """按 cookie token 取用户。过期 / 已停用 / 被吊销一律返回 None。"""
    if not raw_token:
        return None
    row = (
        await db.execute(
            select(AuthSession).where(AuthSession.token_hash == _hash_token(raw_token))
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.expires_at <= now_ms():
        await db.delete(row)
        await db.commit()
        return None
    user = await db.get(User, row.user_id)
    if user is None or not user.enabled:
        return None
    return user


async def revoke_session(db: AsyncSession, raw_token: str) -> None:
    if not raw_token:
        return
    await db.execute(
        delete(AuthSession).where(AuthSession.token_hash == _hash_token(raw_token))
    )
    await db.commit()


async def revoke_user_sessions(db: AsyncSession, user_id: str, keep: str = "") -> None:
    """吊销某用户全部会话（改密码时用）。keep 为要保留的原始 token。"""
    stmt = delete(AuthSession).where(AuthSession.user_id == user_id)
    if keep:
        stmt = stmt.where(AuthSession.token_hash != _hash_token(keep))
    await db.execute(stmt)
    await db.commit()


async def cleanup_expired_sessions(db: AsyncSession) -> int:
    res = await db.execute(
        delete(AuthSession).where(AuthSession.expires_at <= now_ms())
    )
    await db.commit()
    return res.rowcount or 0


# ─────────────────────────── 用户 ───────────────────────────


async def get_user_by_name(db: AsyncSession, username: str) -> User | None:
    return (
        await db.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()


async def create_user(
    db: AsyncSession,
    username: str,
    password: str,
    *,
    is_admin: bool = True,
    enabled: bool = True,
) -> User:
    user = User(
        id=path_id(),
        username=username,
        password_hash=hash_password(password),
        is_admin=1 if is_admin else 0,
        enabled=1 if enabled else 0,
    )
    db.add(user)
    await db.commit()
    log.info("auth_user_created", username=username, is_admin=is_admin)
    return user


async def ensure_bootstrap_admin(db: AsyncSession) -> str | None:
    """
    鉴权开启且用户表为空时，自动创建管理员。

    返回值为自动生成的初始密码（只有生成时返回一次，用于打印到启动日志）。
    env 里配了 admin_password 则用配置值，不返回。
    """
    if not settings.security.auth_enabled:
        return None
    existing = (await db.execute(select(User).limit(1))).scalar_one_or_none()
    if existing is not None:
        return None

    username = settings.security.admin_username or "admin"
    password = settings.security.admin_password
    generated = not password
    if generated:
        password = secrets.token_urlsafe(12)
    await create_user(db, username, password, is_admin=True, enabled=True)
    log.warning(
        "auth_bootstrap_admin_created",
        username=username,
        password_generated=generated,
        hint="首次启动已创建管理员账号，请立即登录并修改密码"
        if generated
        else "首次启动已创建管理员账号（密码来自 JEEVES_SECURITY__ADMIN_PASSWORD）",
    )
    return password if generated else None


# ─────────────────────────── 登录限流 ───────────────────────────

# 进程内滑动窗口。单机个人应用，不需要 Redis ——
# 重启即清零（攻击者不会因为重启就更强）。
# key 是 client IP，value 是失败时间戳队列。
_failures: dict[str, deque[float]] = defaultdict(deque)


def _prune(ip: str) -> None:
    window = settings.security.login_rate_window
    q = _failures[ip]
    cutoff = monotonic() - window
    while q and q[0] < cutoff:
        q.popleft()
    if not q:
        _failures.pop(ip, None)


def login_blocked(ip: str) -> bool:
    """该 IP 是否已触发限流（窗口内失败次数达到上限）。"""
    _prune(ip)
    return len(_failures.get(ip, ())) >= settings.security.login_rate_limit


def record_login_failure(ip: str) -> None:
    _failures[ip].append(monotonic())


def reset_login_failures(ip: str) -> None:
    _failures.pop(ip, None)


def client_ip(request: Request) -> str:
    """取客户端 IP。信任 X-Forwarded-For 的第一个值 ——
    反代（Caddy/nginx）部署时这是真实客户端；直连时头为空不生效。"""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]

