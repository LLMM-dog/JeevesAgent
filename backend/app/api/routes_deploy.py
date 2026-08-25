"""
部署相关路由：远程访问状态、部署配置（复用 settings 体系）、
cpolar（公网主推）+ Tailscale（私密备选）隧道管理。

这些端点控制网络暴露面，鉴权开启时（/api/* 一律要求登录）
只有已登录用户能操作；本机无鉴权模式下信任模型与之前一致。
"""

from typing import Any

import structlog
from app.api.schemas import (
    CpolarActionResponse,
    CpolarAuthtokenRequest,
    CpolarRequest,
    DeploySettingsUpdate,
    DeployStatusResponse,
    EnableAuthRequest,
    EnableAuthResponse,
    TailscaleActionResponse,
    TailscaleRequest,
)
from app.core.config import settings
from app.infra.db.session import get_db
from app.modules.auth import service as auth_svc
from app.modules.auth.middleware import COOKIE_NAME
from app.modules.auth.models import User
from app.modules.deploy import cpolar
from app.modules.deploy import tailscale as ts
from app.modules.settings import service as settings_svc
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/deploy", tags=["deploy"])

DEPLOY_SECTIONS = ("security", "app")


@router.get("/status", response_model=DeployStatusResponse, summary="远程访问状态")
async def deploy_status(request: Request) -> DeployStatusResponse:
    """前端部署 tab 的顶部状态卡。"""
    return DeployStatusResponse(
        host=settings.app.host,
        port=settings.app.port,
        is_localhost=settings.is_localhost,
        auth_enabled=settings.security.auth_enabled,
        https=request.url.scheme == "https",
    )


# ─────────────────────────── 部署配置（复用 settings 体系） ───────────────────────────


@router.get("/settings", summary="部署相关的可调设置")
async def deploy_settings(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """直接复用 settings_service.describe()，只返回 security/app 段。"""
    await settings_svc.reload(db)
    items = [i for i in settings_svc.describe() if i["section"] in DEPLOY_SECTIONS]
    return {"items": items}


@router.put("/settings", summary="修改部署设置（鉴权类立即生效）")
async def update_deploy_settings(
    payload: DeploySettingsUpdate, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """复用 settings_service.set_many；host/port 需要重启。"""
    try:
        applied = await settings_svc.set_many(db, payload.values)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    items = [i for i in settings_svc.describe() if i["section"] in DEPLOY_SECTIONS]
    return {"applied": applied, "items": items}


# ─────────────────────────── 一键开启鉴权 ───────────────────────────


@router.post("/enable-auth", response_model=EnableAuthResponse, summary="开启鉴权并创建首个管理员")
async def enable_auth(
    body: EnableAuthRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    原子地完成「开鉴权 + 建首个管理员 + 自动登录」。

    只在用户表为空时可用 —— 之后改密码走 /api/auth/password，
    建用户走 /api/auth/users。避免「开了鉴权却进不去」的死角。
    """
    count = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    if count > 0:
        raise HTTPException(status_code=409, detail="已存在用户，请在登录后管理账户")

    username = body.username.strip()
    # 长度校验由 EnableAuthRequest 的 pydantic 约束完成（422）
    await auth_svc.create_user(db, username, body.password, is_admin=True)
    await settings_svc.set_many(db, {"security.auth_enabled": True})

    u = await auth_svc.get_user_by_name(db, username)
    assert u is not None
    raw = await auth_svc.create_session(db, u.id, ip=auth_svc.client_ip(request))
    resp = JSONResponse(EnableAuthResponse(username=username, token=raw).model_dump())
    resp.set_cookie(
        key=COOKIE_NAME,
        value=raw,
        max_age=settings.security.session_ttl_days * 86_400,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )
    log.info("deploy_auth_enabled", username=username)
    return resp


# ─────────────────────────── cpolar（公网主推） ───────────────────────────


@router.get("/cpolar", summary="cpolar 状态")
async def cpolar_status() -> dict[str, Any]:
    """安装 / token / 隧道 / URL。"""
    return await cpolar.status()


@router.post("/cpolar/install", response_model=CpolarActionResponse, summary="下载 cpolar 客户端")
async def cpolar_install() -> CpolarActionResponse:
    """下载到项目 .cpolar/，随项目走。"""
    ok, detail = await cpolar.install()
    return CpolarActionResponse(ok=ok, detail=detail, status=await cpolar.status())


@router.post("/cpolar/authtoken", response_model=CpolarActionResponse, summary="配置 cpolar token")
async def cpolar_authtoken(body: CpolarAuthtokenRequest) -> CpolarActionResponse:
    """cpolar.com 注册后控制台拿到的 authtoken（只需填一次）。"""
    ok, detail = await cpolar.set_authtoken(body.token)
    return CpolarActionResponse(ok=ok, detail=detail, status=await cpolar.status())


@router.post("/cpolar/start", response_model=CpolarActionResponse, summary="开启公网隧道")
async def cpolar_start(body: CpolarRequest) -> CpolarActionResponse:
    """后台 cpolar http <port>，返回公网 URL。"""
    ok, detail = await cpolar.start_http(body.port)
    return CpolarActionResponse(ok=ok, detail=detail, status=await cpolar.status())


@router.post("/cpolar/stop", response_model=CpolarActionResponse, summary="停止公网隧道")
async def cpolar_stop() -> CpolarActionResponse:
    ok, detail = await cpolar.stop()
    return CpolarActionResponse(ok=ok, detail=detail, status=await cpolar.status())


# ─────────────────────────── Tailscale（私密备选） ───────────────────────────


@router.get("/tailscale", summary="Tailscale 状态")
async def tailscale_status() -> dict[str, Any]:
    """安装 / 登录 / serve / funnel 聚合状态。"""
    return await ts.get_status()


@router.post("/tailscale/install", response_model=TailscaleActionResponse, summary="安装 Tailscale")
async def tailscale_install() -> TailscaleActionResponse:
    """系统已有直接用；否则 Windows 装官方版、其它平台便携版。"""
    ok, detail = await ts.install()
    return TailscaleActionResponse(ok=ok, detail=detail, status=await ts.get_status())


@router.post("/tailscale/daemon", response_model=TailscaleActionResponse, summary="启动 tailscaled 守护进程")
async def tailscale_daemon() -> TailscaleActionResponse:
    """守护进程启动失败后手动重试（Windows 会弹一次 UAC）。"""
    ok, detail = await ts.ensure_daemon()
    return TailscaleActionResponse(ok=ok, detail=detail, status=await ts.get_status())


@router.post("/tailscale/login", response_model=TailscaleActionResponse, summary="发起 Tailscale 登录")
async def tailscale_login() -> TailscaleActionResponse:
    """后台 tailscale login，返回授权 URL。"""
    ok, detail = await ts.start_login()
    return TailscaleActionResponse(ok=ok, detail=detail, status=await ts.get_status())


@router.post("/tailscale/serve", response_model=TailscaleActionResponse, summary="开启 serve")
async def tailscale_serve_start(body: TailscaleRequest) -> TailscaleActionResponse:
    """tailnet 内 https 访问。"""
    ok, detail = await ts.start_serve(body.port)
    return TailscaleActionResponse(ok=ok, detail=detail, status=await ts.get_status())


@router.post("/tailscale/serve/stop", response_model=TailscaleActionResponse, summary="关闭 serve")
async def tailscale_serve_stop() -> TailscaleActionResponse:
    ok, detail = await ts.stop_serve()
    return TailscaleActionResponse(ok=ok, detail=detail, status=await ts.get_status())


@router.post("/tailscale/funnel", response_model=TailscaleActionResponse, summary="开启 funnel")
async def tailscale_funnel_start(body: TailscaleRequest) -> TailscaleActionResponse:
    """公网 https 访问（大陆直连可能不稳）。"""
    ok, detail = await ts.start_funnel(body.port)
    return TailscaleActionResponse(ok=ok, detail=detail, status=await ts.get_status())


@router.post("/tailscale/funnel/stop", response_model=TailscaleActionResponse, summary="关闭 funnel")
async def tailscale_funnel_stop() -> TailscaleActionResponse:
    ok, detail = await ts.stop_funnel()
    return TailscaleActionResponse(ok=ok, detail=detail, status=await ts.get_status())
