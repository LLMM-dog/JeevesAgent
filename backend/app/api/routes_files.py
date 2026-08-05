"""
路径白名单与目录浏览。

## 为什么单独一个文件

白名单是【会话级】的（见 PathWhitelist.session_id），而 routes_config.py
放的是全局配置。混在一起会让"这个设置影响一个会话还是全部会话"变得
不清楚 —— 那正是这次改动要解决的问题。

## 为什么需要目录浏览接口

用户要为对话指定工作目录。没有浏览接口的话只能手打绝对路径，
而 Windows 路径又长又容易打错（反斜杠、盘符大小写、空格），
打错了得到的是"目录不存在"，试几次就放弃了。
"""

from __future__ import annotations

import os
import string
from pathlib import Path

import structlog
from app.api.schemas import (
    BrowseEntry,
    BrowseResult,
    WhitelistCreate,
    WhitelistItem,
    WhitelistPatch,
)
from app.core.config import PROJECT_ROOT
from app.core.exceptions import BadRequestError, ConflictError, NotFoundError
from app.core.ids import path_id
from app.infra.db.session import get_db
from app.modules.provider.models import PathWhitelist
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)
router = APIRouter(tags=["文件访问"])


def _to_item(r: PathWhitelist) -> WhitelistItem:
    # exists 每次都实算。缓存的话目录被移走后界面还显示"正常"，
    # 而用户正在纳闷为什么 agent 读不到文件。
    try:
        ok = Path(r.path).exists()
    except OSError:
        ok = False
    return WhitelistItem(
        id=r.id,
        path=r.path,
        can_write=bool(r.can_write),
        note=r.note,
        builtin=bool(r.builtin),
        session_id=r.session_id,
        exists=ok,
    )


@router.get("/whitelist", response_model=dict, summary="白名单列表")
async def list_whitelist(
    session_id: str | None = Query(
        None, description="给定则返回该会话的条目 + 全局条目；不给只返回全局"
    ),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """
    列出路径白名单。

    带 session_id 时返回【该会话生效的全部条目】—— 会话级的加上全局的。
    这正是 agent 实际使用的集合，界面显示的必须和它一致，
    否则用户看着白名单里有某个目录，却不明白为什么工具还是被拒。
    """
    stmt = select(PathWhitelist)
    if session_id:
        stmt = stmt.where(
            (PathWhitelist.session_id == session_id)
            | (PathWhitelist.session_id.is_(None))
        )
    else:
        stmt = stmt.where(PathWhitelist.session_id.is_(None))
    rows = list((await db.execute(stmt)).scalars())
    # 全局条目排在后面：会话级的是用户刚加的，更关心
    rows.sort(key=lambda r: (r.session_id is None, r.path.lower()))
    return {"items": [_to_item(r) for r in rows]}


@router.post("/whitelist", response_model=WhitelistItem, status_code=201, summary="加白名单")
async def add_whitelist(
    body: WhitelistCreate,
    session_id: str | None = Query(None, description="不给则加为全局条目"),
    db: AsyncSession = Depends(get_db),
) -> WhitelistItem:
    raw = body.path.strip()
    if not raw:
        raise BadRequestError("路径不能为空", code="empty_path")
    try:
        p = Path(raw).expanduser().resolve()
    except (OSError, ValueError) as e:
        raise BadRequestError(f"路径无法解析：{raw}", code="bad_path") from e

    # 不存在也允许加 —— 用户可能先授权再创建目录。
    # 但要在返回值里标出来（exists=False），让界面能提示。
    dup = (
        await db.execute(
            select(PathWhitelist).where(
                PathWhitelist.path == str(p),
                PathWhitelist.session_id == session_id,
            )
        )
    ).scalars().first()
    if dup is not None:
        raise ConflictError("这个路径已经在白名单里了", code="path_exists")

    row = PathWhitelist(
        id=path_id(),
        session_id=session_id,
        path=str(p),
        can_write=1 if body.can_write else 0,
        note=body.note,
        builtin=0,
    )
    db.add(row)
    await db.commit()
    log.info("whitelist_added", path=str(p), session_id=session_id, write=body.can_write)
    return _to_item(row)


@router.patch("/whitelist/{item_id}", response_model=WhitelistItem, summary="改白名单")
async def patch_whitelist(
    item_id: str, body: WhitelistPatch, db: AsyncSession = Depends(get_db)
) -> WhitelistItem:
    row = (
        await db.execute(select(PathWhitelist).where(PathWhitelist.id == item_id))
    ).scalars().first()
    if row is None:
        raise NotFoundError("白名单条目不存在", code="whitelist_not_found")

    data = body.model_dump(exclude_unset=True)
    if "can_write" in data and data["can_write"] is not None:
        # 内置条目的读写权限不能改。
        #
        # 上传目录是【只读】的：模型能读用户传的图，但不该改它们 ——
        # 改了之后用户看到的图和自己传的不一样，而且没有任何提示。
        if row.builtin:
            raise BadRequestError("内置条目的权限不可修改", code="builtin_readonly")
        row.can_write = 1 if data["can_write"] else 0
    if "note" in data and data["note"] is not None:
        row.note = data["note"]
    await db.commit()
    return _to_item(row)


# 删除成功返回 {"ok": true}，不用 204。
#
# 项目里其它删除接口（会话、供应商）都是这个形态，前端统一按
# 有响应体处理。混用 204 会让前端多一条分支。
@router.delete("/whitelist/{item_id}", summary="删白名单")
async def delete_whitelist(
    item_id: str, db: AsyncSession = Depends(get_db)
) -> dict[str, bool]:
    row = (
        await db.execute(select(PathWhitelist).where(PathWhitelist.id == item_id))
    ).scalars().first()
    if row is None:
        raise NotFoundError("白名单条目不存在", code="whitelist_not_found")
    # 内置项不可删 —— 删了 agent 就完全不能读写文件，
    # 而用户不容易想到是这个原因。
    if row.builtin:
        raise BadRequestError("内置条目不可删除", code="builtin_undeletable")
    await db.delete(row)
    await db.commit()
    log.info("whitelist_removed", path=row.path)
    return {"ok": True}


# ── 目录浏览 ──

_MAX_ENTRIES = 500


def _roots() -> list[BrowseEntry]:
    """
    常用起点。让用户不用手打绝对路径。
    """
    out: list[BrowseEntry] = []
    home = Path.home()
    out.append(BrowseEntry(name="主目录", path=str(home), is_dir=True))
    out.append(
        BrowseEntry(name="项目目录", path=str(PROJECT_ROOT), is_dir=True)
    )
    if os.name == "nt":
        # 只列真实存在的盘符。全列 A-Z 的话界面上一半是点不开的死项
        for letter in string.ascii_uppercase:
            d = Path(f"{letter}:\\")
            try:
                if d.exists():
                    out.append(BrowseEntry(name=f"{letter}:", path=str(d), is_dir=True))
            except OSError:
                continue
    else:
        out.append(BrowseEntry(name="/", path="/", is_dir=True))
    return out


@router.get("/browse", response_model=BrowseResult, summary="浏览目录")
async def browse(
    path: str = Query("", description="留空返回常用起点"),
    dirs_only: bool = Query(True, description="只列目录"),
) -> BrowseResult:
    """
    列目录内容，供工作目录选择器用。

    ## 这个接口【不受】白名单限制

    它的用途正是"选一个还没被授权的目录"，受限的话就自相矛盾了。

    暴露的信息只有目录名和文件名 —— 不读任何文件内容。
    而服务默认只绑 127.0.0.1，能调这个接口的人本来就能用文件管理器
    看同样的东西。

    真正需要挡的是【路径穿越】，但这里根本没有"根目录"的概念，
    用户本来就可以浏览任意位置，所以没有可穿越的边界。
    """
    raw = (path or "").strip()
    if not raw:
        return BrowseResult(path="", parent=None, entries=[], roots=_roots())

    try:
        p = Path(raw).expanduser().resolve()
    except (OSError, ValueError) as e:
        raise BadRequestError(f"路径无法解析：{raw}", code="bad_path") from e

    if not p.exists():
        raise NotFoundError(f"目录不存在：{p}", code="dir_missing")
    if not p.is_dir():
        raise BadRequestError(f"不是目录：{p}", code="not_a_dir")

    entries: list[BrowseEntry] = []
    try:
        it = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except PermissionError as e:
        raise BadRequestError(f"没有权限访问：{p}", code="perm_denied") from e
    except OSError as e:
        raise BadRequestError(f"无法列出目录：{p}", code="list_failed") from e

    for child in it:
        if len(entries) >= _MAX_ENTRIES:
            break
        # 隐藏项不列。node_modules、.git 这类会把列表淹掉，
        # 而它们几乎不会被选作工作目录
        if child.name.startswith("."):
            continue
        try:
            is_dir = child.is_dir()
        except OSError:
            continue
        if dirs_only and not is_dir:
            continue
        entries.append(BrowseEntry(name=child.name, path=str(child), is_dir=is_dir))

    parent = str(p.parent) if p.parent != p else None
    return BrowseResult(path=str(p), parent=parent, entries=entries, roots=_roots())
