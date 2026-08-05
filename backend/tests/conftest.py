"""
pytest 公共 fixture。
"""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.infra.db.base import Base

# 【所有模型模块都要 import】—— create_all 只建 metadata 里已注册的表。
#
# 少 import 一个的话，单独跑某个测试文件时那张表不存在，
# 报的是 "no such table"，而错误不指向"conftest 没 import 模型"。
#
# 靠测试文件自己的 import 恰好在 fixture 之前发生是不可靠的：
# 单独运行别的文件时就不成立了。
from app.modules.cron import models as _cron_models  # noqa: F401
from app.modules.session import models  # noqa: F401  确保模型被注册到 metadata
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest.fixture(autouse=True)
def auto_approve_by_default() -> AsyncIterator[None]:
    """
    测试默认走自动批准模式。

    ## 为什么需要这个

    生产默认是 `manual` —— 执行类工具和写文件都要人确认。这是对的默认值
    （用户离开电脑时不该有命令自己跑起来），但在测试里意味着任何调用
    `write_file` 的测试都会挂 300 秒等一个永远不会来的审批。

    审批本身的测试显式把模式切回 manual，见 test_approval.py。

    ## 为什么用 autouse 而不是让每个测试自己设

    漏设的后果是挂死 300 秒而不是失败 —— 排查成本远高于收益。
    默认安全（挂住）在生产是对的，在测试里是纯粹的噪音。
    """
    from app.core import runtime_state

    # 用 runtime_state 提供的钩子，不 patch dataclass 内部。
    #
    # 试过两种 patch 都不行：dataclass 在类创建时就把默认值烧进 __init__ 签名，
    # 改 `SessionRuntime.approval_mode` 和改 `__dataclass_fields__[...].default`
    # 对新建实例都完全无效。所以在 runtime_state 里加了模块级默认值。
    previous = runtime_state.set_default_approval_mode("auto")
    # 前面的测试可能已经给某个 session_id 建过 runtime，清掉
    runtime_state._sessions.clear()
    yield
    runtime_state.set_default_approval_mode(previous)
    runtime_state._sessions.clear()


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """
    每个测试一个独立的内存库。

    用 create_all 而非跑 alembic：测试关心的是"当前表结构对不对"，
    不是"迁移链能不能跑通"。迁移单独测。

    但【必须开 foreign_keys=ON】—— SQLite 默认关闭。测试里不开的话，
    外键与级联删除的 bug 在测试里发现不了，到生产才暴露。
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    def _pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    event.listen(engine.sync_engine, "connect", _pragmas)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False)

    # 【把调度器的会话工厂也指到这个内存库】。
    #
    # 不做的话，任何触发 scheduler.reload() 的测试都会去读写
    # 真实的 data/jeeves.db —— FastAPI 的 dependency_overrides[get_db]
    # 拦不住它，因为调度器是自己开会话的，不走依赖注入。
    #
    # 实测：12 个路由测试往真实库写了 7 条 cron_run、建了 3 个真实会话。
    # 而且开发机上如果有一个 on_missed=run_once 且在补偿窗口内的任务，
    # 跑一次 pytest 会真的拉起一次 agent 对话（强制 auto 审批）。
    from app.modules.cron.scheduler import scheduler as _cron_scheduler

    _cron_scheduler.bind_sessionmaker(maker)

    async with maker() as session:
        yield session

    # 解绑 —— 留着的话下一个测试会用到一个已 dispose 的 engine，
    # 报的是 "Cannot operate on a closed database"，
    # 而那个错误完全不指向"上一个测试的 fixture 没清理"
    _cron_scheduler.bind_sessionmaker(None)
    _cron_scheduler._tasks.clear()  # noqa: SLF001
    _cron_scheduler._heap.clear()  # noqa: SLF001

    await engine.dispose()


@pytest_asyncio.fixture
async def workspace_id(db: AsyncSession) -> str:
    from app.modules.session import repo

    ws = await repo.ensure_default_workspace(db, "/tmp/ws-test")
    return ws.id


@pytest_asyncio.fixture
async def session_id(db: AsyncSession, workspace_id: str) -> str:
    from app.modules.session import repo

    s = await repo.create_session(db, workspace_id=workspace_id)
    return s.id


@pytest.fixture
def assert_fk_on(db: AsyncSession) -> None:
    """确认 fixture 真的开了外键（这个 fixture 本身是自检）。"""

    async def _check() -> None:
        v = (await db.execute(text("PRAGMA foreign_keys"))).scalar()
        assert v == 1, "外键未开启，级联删除测试会假通过"

    return _check  # type: ignore[return-value]


def code_only(src: str) -> str:
    """
    去掉注释和文档字符串，只留可执行代码。

    ## 为什么需要

    源码断言（`assert "xxx" not in src`）会命中【注释里的字样】。
    这个坑很容易踩：

    - 断言"挂载参数里不该出现 .env"，而注释里写着"容器能读 .env"
    - 断言"不该 import app.main"，而注释里解释着"不在这里 import app.main"

    每次都是测试在检查自己的注释，而这类假失败会让人去改本来正确的实现。

    ## 只去注释和文档字符串，保留普通字符串字面量

    参数断言查的正是字符串字面量（`"--network"`、`approval_mode = "auto"`）——
    把所有 STRING 都去掉的话这些断言全部失效，测试变成"永远通过"，
    比假失败更糟（假失败会被发现，永远通过的不会）。

    用 tokenize 而不是正则：正则处理不了字符串里的 `#`。

    注意返回值是按 token 重组的 —— `args.index` 会变成 `args . index`，
    断言前可能要 `.replace(" ", "")`。
    """
    import io
    import tokenize

    out: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and tok.string.startswith(
                ('"""', "'''", 'r"""', "r'''")
            ):
                continue
            out.append(tok.string)
    except (tokenize.TokenError, IndentationError):
        # 源码片段缩进不完整时 tokenize 会失败。
        # 退回原文比抛异常好，只是可能有假失败
        return src
    return " ".join(out)
