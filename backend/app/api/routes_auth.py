"""
鉴权路由：登录 / 登出 / 当前用户 / 改密码 / 用户管理。

登录成功下发 HttpOnly + SameSite=Strict 的会话 cookie；
命令行客户端可用 Authorization: Bearer <token>（登录响应里会返回 token）。
"""

from typing import cast

import structlog
from app.api.schemas import (
    AuthMeResponse,
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    UserCreateRequest,
    UserOut,
    UserPatchRequest,
)
from app.core.config import settings
from app.core.exceptions import AppError, BadRequestError, ConflictError, NotFoundError
from app.core.time import now_ms
from app.infra.db.session import get_db
from app.modules.auth import service as auth_svc
from app.modules.auth.middleware import COOKIE_NAME
from app.modules.auth.models import User
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


def _require_admin(request: Request) -> User:
    """用户管理接口的管理员校验。鉴权未开启时这些接口无意义。"""
    if not settings.security.auth_enabled:
        raise BadRequestError("鉴权未开启，用户管理不可用", code="auth_disabled")
    user = cast(User | None, getattr(request.state, "user", None))
    if user is None or not user.is_admin:
        raise AppError("需要管理员权限", code="forbidden", status_code=403)
    return user


# ─────────────────────────── 登录 / 登出 ───────────────────────────


@router.post("/login", response_model=LoginResponse, summary="登录")
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    用户名 + 密码登录。带进程内限流（默认 15 分钟 10 次失败）。
    成功时下发 HttpOnly cookie，响应体里同时返回 token 供命令行使用。
    """
    ip = auth_svc.client_ip(request)
    if not settings.security.auth_enabled:
        raise BadRequestError("鉴权未开启，无需登录", code="auth_disabled")
    if auth_svc.login_blocked(ip):
        log.warning("login_rate_limited", ip=ip)
        raise BadRequestError(
            "登录尝试过于频繁，请稍后再试",
            code="rate_limited",
            status_code=429,
        )

    user = await auth_svc.get_user_by_name(db, body.username)
    if user is None or not user.enabled or not auth_svc.verify_password(
        body.password, user.password_hash
    ):
        auth_svc.record_login_failure(ip)
        log.info("login_failed", username=body.username, ip=ip)
        raise BadRequestError("用户名或密码错误", code="invalid_credentials", status_code=401)

    auth_svc.reset_login_failures(ip)
    user.last_login_at = now_ms()
    await db.commit()

    raw = await auth_svc.create_session(
        db,
        user.id,
        ip=ip,
        user_agent=request.headers.get("user-agent", ""),
    )
    ttl = settings.security.session_ttl_days * 86_400
    log.info("login_ok", username=user.username, ip=ip)

    resp = JSONResponse(
        LoginResponse(
            username=user.username, is_admin=bool(user.is_admin), token=raw
        ).model_dump()
    )
    # 只有 HTTPS 请求（或经反代转发的 https）才标记 Secure。
    secure = request.url.scheme == "https"
    resp.set_cookie(
        key=COOKIE_NAME,
        value=raw,
        max_age=ttl,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )
    return resp


@router.post("/logout", summary="登出")
async def logout(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    raw = request.cookies.get(COOKIE_NAME) or ""
    auth_header = request.headers.get("authorization", "")
    if not raw and auth_header.lower().startswith("bearer "):
        raw = auth_header[7:].strip()
    if raw:
        await auth_svc.revoke_session(db, raw)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@router.get("/me", response_model=AuthMeResponse, summary="当前鉴权状态")
async def me(request: Request) -> AuthMeResponse:
    """前端启动时调用：判断要不要显示登录页。公开接口（无 cookie 也返回 200）。"""
    if not settings.security.auth_enabled:
        return AuthMeResponse(auth_enabled=False, authenticated=False)
    user = getattr(request.state, "user", None)
    if user is None:
        return AuthMeResponse(
            auth_enabled=True,
            authenticated=False,
            session_ttl_days=settings.security.session_ttl_days,
        )
    return AuthMeResponse(
        auth_enabled=True,
        authenticated=True,
        username=user.username,
        is_admin=bool(user.is_admin),
        session_ttl_days=settings.security.session_ttl_days,
    )


# ─────────────────────────── 密码 / 用户管理 ───────────────────────────


@router.post("/password", summary="修改自己的密码")
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    state_user = getattr(request.state, "user", None)
    if state_user is None:
        raise BadRequestError("未登录", code="unauthorized", status_code=401)
    # 从【本路由的 db 会话】重新加载用户 —— request.state.user 来自
    # 中间件自己的会话，直接改它再 commit 不会生效（两个会话两个 identity map）。
    user = await db.get(User, state_user.id)
    if user is None:
        raise BadRequestError("用户不存在", code="not_found", status_code=404)
    if not auth_svc.verify_password(body.old_password, user.password_hash):
        raise BadRequestError("原密码错误", code="invalid_credentials", status_code=401)
    user.password_hash = auth_svc.hash_password(body.new_password)
    # 改密后吊销其他设备上的会话（保留当前这个）。
    raw = request.state.auth_token or ""
    await auth_svc.revoke_user_sessions(db, user.id, keep=raw)
    await db.commit()
    log.info("password_changed", username=user.username)
    return JSONResponse({"ok": True})


@router.get("/users", response_model=list[UserOut], summary="用户列表（管理员）")
async def list_users(
    request: Request, db: AsyncSession = Depends(get_db)
) -> list[UserOut]:
    _require_admin(request)
    rows = (await db.execute(select(User).order_by(User.created_at))).scalars()
    return [
        UserOut(
            id=u.id,
            username=u.username,
            is_admin=bool(u.is_admin),
            enabled=bool(u.enabled),
            created_at=u.created_at,
            last_login_at=u.last_login_at,
        )
        for u in rows
    ]


@router.post("/users", response_model=UserOut, status_code=201, summary="创建用户（管理员）")
async def create_user(
    body: UserCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    _require_admin(request)
    if await auth_svc.get_user_by_name(db, body.username) is not None:
        raise ConflictError(f"用户名 {body.username} 已存在", code="username_taken")
    u = await auth_svc.create_user(
        db, body.username, body.password, is_admin=body.is_admin
    )
    return UserOut(
        id=u.id,
        username=u.username,
        is_admin=bool(u.is_admin),
        enabled=bool(u.enabled),
        created_at=u.created_at,
        last_login_at=u.last_login_at,
    )


@router.patch("/users/{user_id}", response_model=UserOut, summary="修改用户（管理员）")
async def patch_user(
    user_id: str,
    body: UserPatchRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    admin = _require_admin(request)
    u = await db.get(User, user_id)
    if u is None:
        raise NotFoundError("用户不存在")
    if body.is_admin is not None:
        if u.id == admin.id and not body.is_admin:
            raise BadRequestError("不能取消自己的管理员权限")
        u.is_admin = 1 if body.is_admin else 0
    if body.enabled is not None:
        if u.id == admin.id and not body.enabled:
            raise BadRequestError("不能停用自己的账号")
        u.enabled = 1 if body.enabled else 0
    if body.password:
        u.password_hash = auth_svc.hash_password(body.password)
        # 重置密码后吊销该用户所有会话（强制重新登录）。
        await auth_svc.revoke_user_sessions(db, u.id)
    await db.commit()
    log.info("user_updated", username=u.username, by=admin.username)
    return UserOut(
        id=u.id,
        username=u.username,
        is_admin=bool(u.is_admin),
        enabled=bool(u.enabled),
        created_at=u.created_at,
        last_login_at=u.last_login_at,
    )


@router.delete("/users/{user_id}", summary="删除用户（管理员）")
async def delete_user(
    user_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    admin = _require_admin(request)
    u = await db.get(User, user_id)
    if u is None:
        raise NotFoundError("用户不存在")
    if u.id == admin.id:
        raise BadRequestError("不能删除自己")
    if u.is_admin and not await _admin_count_ok(db, keep=u.id):
        raise BadRequestError("不能删除最后一名管理员")
    await auth_svc.revoke_user_sessions(db, u.id)
    await db.delete(u)
    await db.commit()
    log.info("user_deleted", username=u.username, by=admin.username)
    return JSONResponse({"ok": True})


async def _admin_count_ok(db: AsyncSession, keep: str | None) -> bool:
    """删除/降级后是否还剩至少一名管理员。keep 为将要保留的用户 id。"""
    rows = (await db.execute(select(User).where(User.is_admin == 1))).scalars()
    admins = [u for u in rows if u.id != keep]
    return len(admins) >= 1
