"""
应用装配与启动自检。

启动日志必须打出【实际加载的 .env 路径和数据库路径】——
这两个值是本项目最隐蔽一类问题的根源（相对路径按进程 cwd 解析，
从不同目录启动会读到不同的配置和数据库，表现为"配置没生效"或"会话全没了"）。
"""

import asyncio
import contextlib
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

# 【必须用 starlette 的 HTTPException】——StaticFiles 抛的是它。
# fastapi.HTTPException 是它的【子类】，
# except 子类抓不到父类实例，而表现和没写这段代码一样。
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import routes_chat, routes_config, routes_cron, routes_files, routes_models
from app.core.config import PROJECT_ROOT, settings
from app.core.exceptions import AppError
from app.core.ids import path_id
from app.core.logging import setup_logging
from app.core.time import now_ms
from app.infra.db.session import dispose_engine, get_sessionmaker
from app.infra.llm.openai_compat import close_llm
from app.modules.agent import run_registry
from app.modules.agent.chat_service import ChatService
from app.modules.agent.pathguard import AllowedPath, set_allowed
from app.modules.agent.tools.asset import ManageAssetTool
from app.modules.agent.tools.base import ToolRegistry
from app.modules.agent.tools.context import CompactContextTool
from app.modules.agent.tools.exec import RunPythonTool, RunShellTool
from app.modules.agent.tools.file import (
    EditFileTool,
    GlobTool,
    GrepTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from app.modules.agent.tools.memory import (
    ForgetMemoryTool,
    RecallTool,
    RememberTool,
    UpdateMemoryTool,
)
from app.modules.agent.tools.skill import LoadSkillFileTool, LoadSkillTool
from app.modules.agent.tools.subagent import SubAgentTool
from app.modules.agent.tools.todo import TodoReadTool, TodoWriteTool
from app.modules.provider.models import PathWhitelist  # noqa: F401
from app.modules.session import repo
from app.modules.todo.models import Todo  # noqa: F401
from app.modules.trace.writer import init_writer as init_trace_writer
from app.modules.trace.writer import shutdown_writer as shutdown_trace_writer

log = structlog.get_logger(__name__)


async def _connect_mcp(registry: ToolRegistry) -> None:
    """
    连接 MCP 服务器并把工具注册进 registry。

    ## 为什么失败不阻止启动

    MCP 是外部依赖，随时可能挂。一个配错的服务器（command 不存在、
    URL 打错）不应该让整个应用起不来 —— 那会让用户完全没法进设置页
    去修那个配置。

    连不上的服务器在 /api/mcp/servers 里显示具体原因。
    """
    from app.modules.mcp.loader import load_configs
    from app.modules.mcp.manager import get_manager
    from app.modules.mcp.tools import build_tools

    configs, errors = load_configs()
    for msg in errors:
        log.warning("mcp_config_error", detail=msg)
    if not configs:
        return

    try:
        await get_manager().connect_all(configs)
    except Exception as e:  # noqa: BLE001
        log.warning("mcp_connect_all_failed", err=str(e)[:300])
        return

    for tool in build_tools():
        registry.register(tool)
    log.info("mcp_tools_registered", count=len(build_tools()))

def build_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for tool in (
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        ListDirTool(),
        GlobTool(),
        GrepTool(),
        # 执行类工具都标了 requires_approval，manual 模式下每次都要人确认
        RunShellTool(),
        RunPythonTool(),
        LoadSkillTool(),
        LoadSkillFileTool(),
        SubAgentTool(),
        RememberTool(),
        RecallTool(),
        UpdateMemoryTool(),
        ForgetMemoryTool(),
        TodoWriteTool(),
        TodoReadTool(),
        # 宏和技能的增删改查。
        #
        # MacroPicker 的空态一直写着"对我说'把这个流程存成宏'，我会帮你建"，
        # 但 macros/ 不在白名单里，模型写不进去 —— 那句承诺落不了地。
        # 这个工具让它真的能建。
        ManageAssetTool(),
        # 主动压缩。原来只有被动压缩（涨到窗口 75% 才触发）——
        # 那个时机不由模型决定，它只看总量，不知道"调研阶段已经结束、
        # 几十条工具输出已经没用了"。
        CompactContextTool(),
    ):
        reg.register(tool)

    # 联网工具。
    #
    # web_fetch 总是注册（只依赖 httpx）；web_search 只在配了
    # JEEVES_WEBSEARCH__BACKEND 时注册 —— 没有后端时注册了也用不了，
    # 而它的工具定义每轮都在烧 token。
    from app.modules.web.tools import build_web_tools

    for tool in build_web_tools():
        reg.register(tool)

    return reg


def check_config() -> None:
    """
    启动期校验。缺加密密钥【拒绝启动】——
    带着缺失的密钥启动，所有对话都会在解密 API Key 时失败，
    而报错是"解密错误"，用户完全想不到是启动配置的问题。
    """
    if not settings.security.encryption_key.strip():
        raise SystemExit(
            "\n启动失败：未配置 SECURITY__ENCRYPTION_KEY\n\n"
            f"  1. 确认 .env 存在：{settings.env_file_path}\n"
            "  2. 生成密钥：\n"
            '     uv run python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"\n'
            "  3. 写入 .env：JEEVES_SECURITY__ENCRYPTION_KEY=<生成的值>\n\n"
            "  注意变量名必须带 JEEVES_ 前缀。\n"
            "  【备份这个密钥】丢了以后已存的 API Key 全部无法解密。\n"
        )

    if not settings.is_localhost:
        # 本项目无鉴权。绑到非本机地址等于把「能执行任意命令的接口」暴露到网络上。
        log.warning(
            "no_auth_non_localhost",
            host=settings.app.host,
            msg="服务无鉴权却绑定到非本机地址，任何能访问该端口的人都能执行命令",
        )


async def _run_migrations() -> None:
    """
    启动时自动跑迁移到最新版本。

    ## 为什么自动跑

    这是个本地单用户应用，用户不会记得手动执行 alembic upgrade。
    忘了跑的表现是"某个功能报 no such column"，而报错完全不提示
    "你需要跑迁移"。自动跑掉这一整类问题。

    ## 为什么在 asyncio.to_thread 里

    alembic 的 command API 是同步的，且它内部会自己 asyncio.run()
    （见 migrations/env.py 的 run_migrations_online）。在已经运行的
    event loop 里直接调会抛 "asyncio.run() cannot be called from a
    running event loop"。丢到线程里执行绕开这个限制。
    """
    from alembic import command
    from alembic.config import Config

    def _upgrade() -> None:
        cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(PROJECT_ROOT / "backend" / "migrations"))
        # embedded 让 env.py 跳过 fileConfig —— 否则它会覆盖已配好的
        # structlog handler，控制台开始打印裸的 format 字面量，
        # 而应用自己的结构化日志全部消失。
        cfg.attributes["embedded"] = True
        command.upgrade(cfg, "head")

    await asyncio.to_thread(_upgrade)
    log.info("migrations_applied")


async def init_runtime() -> None:
    """
    首次启动的自动初始化。全部幂等，可重复执行。

    这样 git clone + 填 .env + 启动就能用，不需要手动建目录。
    """
    for d in (
        settings.data_dir,
        settings.uploads_dir,
        settings.logs_dir,
        settings.workspace_dir,
        settings.workspace_dir / ".jeeves" / "tmp",
        settings.skills_dir,
        settings.macros_dir,
        settings.config_dir,
        settings.personas_dir,
        # 被截断的命令输出落在这里，read_file 要能读到
        settings.temp_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)

    # 人设文件从 example 复制（不覆盖已有的）。
    #
    # 【必须包含 AGENTS】。它原来是被 git 跟踪的，而设置页允许用户编辑
    # 它 —— 结果是用户改完之后 git pull 直接失败：
    #
    #   error: Your local changes to the following files would be
    #   overwritten by merge: personas/AGENTS.md
    #   Aborting
    #
    # 用户要么丢掉自己的修改，要么学会 git stash。两个都不该要求。
    #
    # not target.exists() 是关键：已有文件绝不覆盖，否则每次升级
    # 都把用户的人格设定重置回默认，而他不会想到是升级干的。
    for name in ("SOUL", "USER", "AGENTS"):
        target = settings.personas_dir / f"{name}.md"
        example = settings.personas_dir / f"{name}.example.md"
        if not target.exists() and example.exists():
            shutil.copy2(example, target)
            log.info("persona_initialized", file=target.name)

    await _run_migrations()

    async with get_sessionmaker()() as db:
        ws = await repo.ensure_default_workspace(db, str(settings.workspace_dir))

        # 路径白名单的内置项。
        #
        # ## 为什么逐条 upsert 而不是"表为空才插"
        #
        # 原来是 `if not existing` 一次性插两条。那样新增内置项时，
        # 【已经在用的用户永远拿不到】—— 他们表里有数据，整个分支被跳过。
        # 而症状是"文档说能写 skills/，我这儿报路径不在白名单内"。
        #
        # ## 为什么 skills/ 和 macros/ 要可写
        #
        # 技能不是单个 md 文件，它是一个目录：SKILL.md + references/ +
        # 可能还有脚本和模板。manage_asset 只能写 SKILL.md，
        # 而一份像样的技能往往要带参考资料。
        #
        # 白名单打开之后模型能用 write_file / edit_file / list_dir /
        # glob 在里面自由组织文件 —— 那是它已经熟练的工具，
        # 不需要为"写技能附件"再造一套 API。
        #
        # 硬拒止清单（.env / *.pem / credentials 等）优先级高于白名单，
        # 所以这不等于放开敏感文件。
        wanted = [
            (settings.workspace_dir.resolve(), 1, "默认工作区"),
            ((settings.data_dir / "uploads").resolve(), 0, "上传目录（只读）"),
            (
                settings.skills_dir.resolve(),
                1,
                "技能目录（模型可增删改技能及其附件）",
            ),
            (settings.macros_dir.resolve(), 1, "宏目录（模型可增删改宏）"),
        ]
        existing_paths = {
            r.path for r in (await db.execute(select(PathWhitelist))).scalars()
        }
        added = 0
        for p, can_write, note in wanted:
            if str(p) in existing_paths:
                continue
            db.add(
                PathWhitelist(
                    id=path_id(),
                    path=str(p),
                    can_write=can_write,
                    note=note,
                    builtin=1,
                )
            )
            added += 1
        if added:
            await db.commit()
            log.info("whitelist_builtins_added", count=added)

        rows = list((await db.execute(select(PathWhitelist))).scalars())
        allowed = [
            AllowedPath(path=Path(r.path), can_write=bool(r.can_write)) for r in rows
        ]
        # 命令输出的落盘目录必须可读。
        #
        # 输出被截断时我们告诉模型"完整输出在 data/tmp/xxx.txt"，
        # 但这个目录不在白名单里 —— 实测模型照提示去 read_file，
        # 拿到的是"路径不在白名单内"。落盘做了、路径给了、读不到，
        # 整条链路白费。
        #
        # 只读不写：模型没有理由改这些文件，它们是我们写给它看的。
        # 不入库是有意的：这是实现细节，不该出现在用户的白名单设置界面里，
        # 也不该被用户误删。
        allowed.append(
            AllowedPath(path=settings.temp_dir.resolve(), can_write=False)
        )
        set_allowed(allowed)

    # 清理超过 24h 的临时文件。
    #
    # 两个目录都要清：
    #   data/tmp        —— 命令输出的落盘文件 + run_python 的临时脚本
    #   workspace/.jeeves/tmp —— 历史遗留位置
    #
    # data/tmp 是必须清的：一条产出几百 MB 输出的命令就会留下一个同样大的
    # 文件，而它只在那一轮对话里有用。不清的话磁盘会被慢慢吃掉，
    # 且用户完全看不出是什么占的。
    cutoff = now_ms() - 24 * 3600 * 1000
    removed = 0
    for tmp in (settings.temp_dir, settings.workspace_dir / ".jeeves" / "tmp"):
        if not tmp.is_dir():
            continue
        for f in tmp.iterdir():
            with contextlib.suppress(OSError):
                if f.is_file() and f.stat().st_mtime * 1000 < cutoff:
                    f.unlink()
                    removed += 1
    if removed:
        log.info("tmp_cleaned", count=removed)

    log.info("workspace_ready", path=ws.root_path)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    check_config()

    # 这两行是排查"配置没生效 / 会话全没了"的唯一线索，必须打出来
    log.info(
        "starting",
        project_root=str(PROJECT_ROOT),
        env_file=str(settings.env_file_path),
        env_file_exists=settings.env_file_path.exists(),
        db=str(settings.db_path),
        env=settings.app.env,
    )

    await init_runtime()

    # 追踪写入器。必须在处理请求之前起来 ——
    # 起晚了的话最早那几个 run 的 span 会静默丢掉。
    init_trace_writer(get_sessionmaker())

    app.state.registry = build_registry()

    # MCP 服务器。
    #
    # 【失败不阻止启动】—— MCP 是外部依赖，一个配错的服务器不应该让
    # 整个应用起不来。连不上的会在设置页显示具体原因。
    await _connect_mcp(app.state.registry)

    app.state.chat_service = ChatService(
        sessionmaker=get_sessionmaker(), base_registry=app.state.registry
    )
    log.info("tools_registered", tools=app.state.registry.names())

    # ── 定时任务 ──
    #
    # 【失败不阻止启动】——和 MCP 同样的理由：一个配错的任务不该让
    # 整个应用起不来。装载时非法的 cron 表达式会被跳过并记 warning。
    try:
        from app.modules.cron import repo as cron_repo
        from app.modules.cron.runner import bind_chat_service
        from app.modules.cron.scheduler import scheduler as cron_scheduler

        # 把遗留的 running 记录标成失败。
        #
        # 进程被 kill -9 时那些记录永远停在 running，用户看到
        # "任务卡在执行中"且分不清是真在跑还是上次没退干净。
        async with get_sessionmaker()() as db:
            n = await cron_repo.clear_stale_running(db)
            await db.commit()
        if n:
            log.info("cron_stale_runs_cleared", count=n)

        bind_chat_service(app.state.chat_service)
        await cron_scheduler.start()
    except Exception as e:  # noqa: BLE001
        log.exception("cron_scheduler_start_failed", err=str(e)[:200])

    try:
        yield
    finally:
        await run_registry.cancel_all()
        # MCP 服务器。stdio 子进程必须在关闭时 terminate，否则会残留。
        # 放在其他资源之前关 —— 子进程残留比数据库连接多花费更难排查。
        from app.modules.mcp.manager import close_manager
        await close_manager()
        # 先停追踪写入器再关引擎 —— 顺序反了的话队列里剩下的 span
        # 会因为引擎已 dispose 而全部写失败，而那几条往往正是排查崩溃
        # 时最需要的。
        with contextlib.suppress(Exception):
            from app.modules.cron.scheduler import scheduler as cron_scheduler

            await cron_scheduler.stop()
        await shutdown_trace_writer()
        # 清掉所有沙箱容器。
        #
        # 只覆盖正常退出路径 —— kill -9 / 断电时这里不会执行，
        # 所以 DockerSandbox 还有启动时的 cleanup_orphans 兜底。
        with contextlib.suppress(Exception):
            from app.infra.sandbox.factory import get_sandbox

            await (await get_sandbox()).shutdown()
        await close_llm()
        await dispose_engine()
        log.info("stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Jeeves",
        version="0.1.0",
        description="本地个人 Agent 工作台",
        lifespan=lifespan,
    )

    # 开发时前端在 5173、后端在 8000，需要 CORS。
    # 【不用通配符】：虽然本项目无鉴权，通配符仍允许任意网页向本地 API 发请求
    # （一个恶意网页可以让你的 agent 执行命令）。显式列出开发端口。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id"],
    )

    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.to_detail()})

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI 默认的 422 结构与我们的不一致。统一成同一形状，
        # 前端只需处理一种错误结构。
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", []))
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "validation_error",
                    "message": "请求参数不合法",
                    "hint": f"{loc}: {first.get('msg', '')}" if loc else None,
                }
            },
        )

    app.include_router(routes_chat.router, prefix=settings.api_prefix, tags=["chat"])
    app.include_router(routes_config.router, prefix=settings.api_prefix, tags=["config"])
    app.include_router(routes_cron.router, prefix=settings.api_prefix, tags=["cron"])
    app.include_router(routes_files.router, prefix=settings.api_prefix, tags=["files"])
    app.include_router(
        routes_models.router, prefix=settings.api_prefix, tags=["models"]
    )

    # 静态文件【必须最后挂载】，在所有 API 路由注册之后 ——
    # 否则 "/" 的通配会吃掉 /api/*
    if settings.frontend_dist.is_dir():
        # SPA 回退：前端路由（/chat、/settings、/cron）在服务端不存在对应文件。
        #
        # 【StaticFiles(html=True) 不够】—— 它只在【目录】上补 index.html，
        # 所以 "/" 能出页面，但 "/chat" 会 404（那既不是文件也不是目录）。
        #
        # 实测后果：生产模式下在 /chat 或 /settings 页面按 F5 刷新就白屏 404，
        # 应用只有从 "/" 进入才能用。而这个问题在开发模式下完全看不出来 ——
        # vite dev server 自带 SPA 回退。
        class _SpaStatic(StaticFiles):
            @staticmethod
            def _no_store(resp: Response) -> Response:
                """
                让 index.html 永不缓存。

                ## 为什么这条必须有

                构建产物的文件名带 hash（index-Cqs10vA.js），所以 JS/CSS
                可以放心长期缓存 —— 内容变了文件名就变了。

                但 index.html 的名字不变，而它【引用】那些带 hash 的文件。
                StaticFiles 默认给它发 etag + last-modified，浏览器于是
                缓存它。下次更新后：新 JS 已经在服务器上，可浏览器还在用
                缓存的旧 HTML，那份 HTML 指向【旧的】JS 文件名。

                结果是"更新了但界面没变"，而且极难自查 —— 服务器上文件是
                新的、构建是成功的、日志一切正常，只有浏览器在骗你。
                真实踩到过：新加的工作目录选择器和设置页标签都上线了，
                但界面上看不到，需要 Ctrl+Shift+R 才出来。

                no-store 而不是 no-cache：后者仍允许缓存，只是每次要
                回源验证；前者根本不存。HTML 只有几百字节，不值得为它
                省一次往返却换来这类问题。
                """
                resp.headers["Cache-Control"] = "no-store, must-revalidate"
                # 有些代理只认这两个老字段，一并发出
                resp.headers["Pragma"] = "no-cache"
                resp.headers["Expires"] = "0"
                # etag / last-modified 必须去掉。留着的话浏览器会用
                # If-None-Match 换回一个 304，等于缓存仍然生效。
                #
                # 用 del 而不是 pop —— starlette 的 MutableHeaders 没有
                # pop 方法（它不是 dict），调了直接 500。
                # 而 __delitem__ 对不存在的键是静默的，不需要先判断。
                del resp.headers["etag"]
                del resp.headers["last-modified"]
                return resp

            async def get_response(self, path: str, scope: Any) -> Response:
                try:
                    resp = await super().get_response(path, scope)
                    # 目录请求（"/"）也会落到这里，此时 path 是 "."
                    if path in ("", ".", "index.html") or path.endswith("/"):
                        return self._no_store(resp)
                    return resp
                except StarletteHTTPException as e:
                    # 【必须 catch 异常而不是判断 status_code】。
                    #
                    # StaticFiles 找不到文件时是 `raise HTTPException(404)`，
                    # 不是返回一个 404 响应（starlette/staticfiles.py）。
                    # 我最初写的 `if resp.status_code == 404` 永远不会执行 ——
                    # 而表现和完全没写这段代码一样，很难看出问题在哪。
                    if e.status_code != 404:
                        raise
                    # 回退到 index.html，交给前端路由处理。
                    #
                    # 不判断路径是否像前端路由（比如"没有扩展名"）——
                    # /api/* 已经被前面的路由吃掉了，能走到这里的
                    # 要么是前端路由，要么是真的不存在的资源。
                    # 后者拿到 index.html 也无害：前端会显示"页面不存在"。
                    #
                    # 这条回退路径也要禁缓存 —— /chat、/settings 这些
                    # 前端路由全走这里，是用户最常访问的入口。
                    return self._no_store(
                        await super().get_response("index.html", scope)
                    )

        app.mount(
            "/",
            _SpaStatic(directory=str(settings.frontend_dist), html=True),
            name="static",
        )

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.app.host,
        port=settings.app.port,
        reload=settings.app.is_dev,
        log_config=None,  # 用我们自己的 structlog 配置
    )


if __name__ == "__main__":
    main()
