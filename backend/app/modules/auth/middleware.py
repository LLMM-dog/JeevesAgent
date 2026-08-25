"""
鉴权与安全响应头中间件。

两个职责：

1. 安全响应头（始终生效）
2. 鉴权（settings.security.auth_enabled=True 时生效）：
   - /api/* 除白名单（login/me/health）外全部要求有效会话
   - 会话来源：HttpOnly cookie（浏览器）或 Authorization: Bearer（命令行）
   - CSRF：cookie 鉴权的非安全方法，若带 Origin 头则必须与本站同源

## 为什么用中间件而不是 FastAPI 依赖

依赖只能保护显式声明它的路由，而 SSE 流、静态文件回退、未来新增的
路由都容易漏。中间件一刀切覆盖所有 /api/* —— 漏一个路由等于没鉴权。
"""

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings
from app.infra.db.session import get_sessionmaker
from app.modules.auth import service as auth_svc

log = structlog.get_logger(__name__)

COOKIE_NAME = "jeeves_session"

# 不需要鉴权的 API 路径（前缀匹配）。
# logout 也公开：会话已过期时浏览器仍需要能清掉 cookie。
PUBLIC_API_PATHS = ("/api/auth/login", "/api/auth/logout", "/api/auth/me", "/api/health")

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# 安全响应头。CSP 放开 style-src unsafe-inline：
# react-markdown / shiki 会往元素上写 style 属性，禁掉会破坏渲染。
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "media-src 'self' blob:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
}


def _json_error(status: int, code: str, message: str, hint: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"detail": {"code": code, "message": message, "hint": hint}},
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """所有响应都带上安全头。"""

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response


class AuthMiddleware(BaseHTTPMiddleware):
    """鉴权。auth_enabled=False 时直接放行（本机模式，行为不变）。"""

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        if not settings.security.auth_enabled:
            return await call_next(request)

        path = request.url.path

        # CORS 预检（OPTIONS）直接放行 —— 它不带凭证，且拦截会破坏开发模式跨域。
        if request.method == "OPTIONS":
            return await call_next(request)

        # 非 API 路径（静态前端、SPA 路由）放行 —— 登录页本身必须能加载。
        # API 里登录/元信息/健康检查放行。
        if not path.startswith("/api/"):
            return await call_next(request)

        # /api/auth/me 公开但【要反映当前会话】——
        # 带有效 cookie 时解析出用户供路由读取，解析失败也不 401。
        if path == "/api/auth/me":
            raw_token = request.cookies.get(COOKIE_NAME) or ""
            auth_header = request.headers.get("authorization", "")
            if not raw_token and auth_header.lower().startswith("bearer "):
                raw_token = auth_header[7:].strip()
            if raw_token:
                sessionmaker = getattr(request.app.state, "db_sessionmaker", None) or get_sessionmaker()
                async with sessionmaker() as db:
                    user = await auth_svc.get_user_by_session(db, raw_token)
                    if user is not None:
                        request.state.user = user
                        request.state.auth_token = raw_token
            return await call_next(request)

        if path.startswith(PUBLIC_API_PATHS):
            return await call_next(request)

        # 取会话 token：cookie 优先，Bearer 兜底（命令行/脚本）。
        raw_token = request.cookies.get(COOKIE_NAME) or ""
        auth_header = request.headers.get("authorization", "")
        if not raw_token and auth_header.lower().startswith("bearer "):
            raw_token = auth_header[7:].strip()

        if not raw_token:
            return _json_error(401, "unauthorized", "未登录或会话已过期", "请先登录")

        # 用 app.state 上的 sessionmaker（测试可替换成内存库），
        # 没有则回落全局默认 —— 生产由 lifespan 设置。
        sessionmaker = getattr(request.app.state, "db_sessionmaker", None) or get_sessionmaker()
        async with sessionmaker() as db:
            user = await auth_svc.get_user_by_session(db, raw_token)
            if user is None:
                return _json_error(401, "unauthorized", "未登录或会话已过期", "请重新登录")

        # CSRF 防护：cookie 鉴权的非安全方法，Origin 必须与本站同源。
        # 命令行（curl 等）不发 Origin，放行；浏览器跨站请求一定带 Origin，
        # 且 SameSite=Strict 的 cookie 也不会被带过去 —— 双保险。
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin:
                expected = f"{request.url.scheme}://{request.url.netloc}"
                if origin.rstrip("/") != expected.rstrip("/"):
                    log.warning("csrf_rejected", path=path, origin=origin[:200])
                    return _json_error(403, "csrf_rejected", "请求来源不合法")

        # 暴露给下游路由：request.state.user 让需要用户身份的路由直接取。
        request.state.user = user
        request.state.auth_token = raw_token
        return await call_next(request)
