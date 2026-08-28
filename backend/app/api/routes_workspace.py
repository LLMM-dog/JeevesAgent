"""
工作区管理：CRUD + 容器执行环境 + 容器状态检测。

每个工作区可独立选择执行环境（本机 / Docker 容器）。选 Docker 时：
- 容器名唯一（应用层校验）
- 对话时自动检测/创建/复用容器
"""

import re
import shutil
from typing import Any

import structlog
from app.api.schemas import WorkspaceCreate, WorkspaceOut, WorkspacePatch
from app.core.exceptions import BadRequestError, ConflictError
from app.core.ids import workspace_id
from app.infra.db.session import get_db
from app.modules.session import repo
from app.modules.session.models import Session, Workspace
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/workspaces", tags=["工作区"])


def _clean_root(raw: str) -> str:
    """去掉用户复制路径时可能带上的首尾引号。"""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        raw = raw[1:-1].strip()
    return raw


# Docker 容器名规则：字母数字开头，后续可含 . _ -，最长 64。
CONTAINER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")


def _to_out(ws: Workspace, container_status: str = "") -> WorkspaceOut:
    return WorkspaceOut(
        id=ws.id,
        name=ws.name,
        root_path=ws.root_path,
        is_default=bool(ws.is_default),
        sandbox_backend=ws.sandbox_backend,
        docker_container=ws.docker_container,
        docker_image=ws.docker_image,
        docker_network=ws.docker_network,
        container_status=container_status,
        created_at=ws.created_at,
        updated_at=ws.updated_at,
    )


async def _docker_state(container: str) -> str:
    """查容器状态：running / stopped / not_found / ""（没装 docker 或没配容器）。"""
    if not container or shutil.which("docker") is None:
        return ""
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        "docker", "ps", "-a", "--filter", f"name={container}", "--format", "{{.State}}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
    state = out.decode("utf-8", errors="replace").strip()
    if not state:
        return "not_found"
    if "running" in state:
        return "running"
    return "stopped"


async def _validate_container(db: AsyncSession, name: str, *, exclude_id: str = "") -> None:
    """容器名合法且唯一（不能和别的工作区重名）。"""
    if name and not CONTAINER_RE.match(name):
        raise BadRequestError("容器名不合法：字母数字开头，只能含字母数字 _ . -，最长 64")
    if name:
        q = select(Workspace).where(Workspace.docker_container == name)
        if exclude_id:
            q = q.where(Workspace.id != exclude_id)
        dup = (await db.execute(q)).scalar_one_or_none()
        if dup is not None:
            raise ConflictError(f"容器名 {name} 已被工作区「{dup.name}」使用", code="container_name_taken")


@router.get("", response_model=list[WorkspaceOut], summary="工作区列表")
async def list_workspaces(db: AsyncSession = Depends(get_db)) -> list[WorkspaceOut]:
    rows = list((await db.execute(select(Workspace).order_by(Workspace.is_default.desc(), Workspace.created_at))).scalars())
    out = []
    for ws in rows:
        status = await _docker_state(ws.docker_container) if ws.sandbox_backend == "docker" else ""
        out.append(_to_out(ws, status))
    return out


@router.post("", response_model=WorkspaceOut, status_code=201, summary="创建工作区")
async def create_workspace(
    body: WorkspaceCreate, db: AsyncSession = Depends(get_db)
) -> WorkspaceOut:
    backend = (body.sandbox_backend or "local").strip().lower()
    if backend not in ("local", "docker"):
        raise BadRequestError("sandbox_backend 只能是 local 或 docker")
    container = body.docker_container.strip() if backend == "docker" else ""
    await _validate_container(db, container)

    # root_path 唯一；去掉复制路径时可能带入的首尾引号
    root = _clean_root(body.root_path)
    dup = (await db.execute(select(Workspace).where(Workspace.root_path == root))).scalar_one_or_none()
    if dup is not None:
        raise ConflictError(f"目录 {root} 已被工作区「{dup.name}」使用", code="workspace_path_taken")

    ws = Workspace(
        id=workspace_id(),
        name=body.name.strip(),
        root_path=root,
        is_default=0,
        sandbox_backend=backend,
        docker_container=container,
        docker_image=body.docker_image.strip() or "python:3.12-slim",
        docker_network=body.docker_network if body.docker_network in ("none", "bridge") else "none",
    )
    db.add(ws)
    await db.commit()
    log.info("workspace_created", id=ws.id, name=ws.name, backend=backend, container=container)
    return _to_out(ws)


@router.patch("/{workspace_id}", response_model=WorkspaceOut, summary="编辑工作区")
async def patch_workspace(
    workspace_id: str, body: WorkspacePatch, db: AsyncSession = Depends(get_db)
) -> WorkspaceOut:
    ws = await repo.get_workspace(db, workspace_id)
    if body.name is not None:
        ws.name = body.name.strip()
    if body.sandbox_backend is not None:
        backend = body.sandbox_backend.strip().lower()
        if backend not in ("local", "docker"):
            raise BadRequestError("sandbox_backend 只能是 local 或 docker")
        ws.sandbox_backend = backend
        if backend == "local":
            ws.docker_container = ""
    if body.docker_container is not None:
        container = body.docker_container.strip() if ws.sandbox_backend == "docker" else ""
        await _validate_container(db, container, exclude_id=ws.id)
        ws.docker_container = container
    if body.docker_image is not None:
        ws.docker_image = body.docker_image.strip() or "python:3.12-slim"
    if body.docker_network is not None:
        ws.docker_network = body.docker_network if body.docker_network in ("none", "bridge") else "none"
    await db.commit()
    status = await _docker_state(ws.docker_container) if ws.sandbox_backend == "docker" else ""
    return _to_out(ws, status)


@router.delete("/{workspace_id}", summary="删除工作区")
async def delete_workspace(
    workspace_id: str, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    ws = await repo.get_workspace(db, workspace_id)
    if ws.is_default:
        raise BadRequestError("默认工作区不能删除")

    # 有会话还引用这个工作区时，先把它们迁到默认工作区。
    # 否则 SQLite 外键约束会直接报 IntegrityError，用户只看到一堆堆栈。
    sessions = list(
        (
            await db.execute(
                select(Session).where(Session.workspace_id == ws.id)
            )
        ).scalars()
    )
    if sessions:
        default_ws = (
            await db.execute(select(Workspace).where(Workspace.is_default == 1))
        ).scalar_one_or_none()
        if default_ws is None:
            raise BadRequestError("没有默认工作区，无法迁移引用该工作区的会话")
        for s in sessions:
            s.workspace_id = default_ws.id
        await db.flush()

    await db.delete(ws)
    await db.commit()
    log.info("workspace_deleted", id=ws.id, name=ws.name)
    return {"ok": True}
