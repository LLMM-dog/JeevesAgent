"""
会话级工作目录与路径白名单。

## 这些测试对应的用户抱怨

1. 新开对话时工作目录自动是程序自己的运行目录 —— 用户没授权就被
   指定了一个目录
2. `@` 路径匹配永远显示"没有匹配文件" —— 因为它搜的是项目自己的
   workspace/，而那里几乎是空的
3. 看不到白名单里有什么，也没有地方去改
4. 给 A 会话开的目录权限不该让 B 会话也拿到
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from app.core.exceptions import PathDeniedError
from app.core.ids import path_id
from app.modules.agent.pathguard import (
    AllowedPath,
    get_guard,
    load_session_allowed,
    scoped_guard,
    set_allowed,
)
from app.modules.provider.models import PathWhitelist
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


@pytest_asyncio.fixture
async def client(db: AsyncSession, workspace_id: str) -> Any:
    """
    连到真实 app 的测试客户端。

    ## 三个必须做的覆盖

    1. `get_db` —— 不覆盖的话接口开自己的连接（连到真实的
       data/jeeves.db），测试数据写进开发库。
    2. `get_registry` —— ref-candidates 的 tool 分支要工具注册表，
       它平时在 app.state 里由 lifespan 填。不覆盖会报
       "'State' object has no attribute 'registry'"。
    3. `workspace_id` 依赖 —— 建会话要求库里已有默认工作区，
       否则 POST /api/sessions 返回错误体，表现为 KeyError: 'id'
       （看起来像接口变了，其实是缺前置数据）。

    ## 为什么不跑 lifespan

    跑的话会起追踪写入器和 LLM 客户端等后台任务，pytest 的 loop
    关闭后它们仍引用旧 loop —— teardown 阶段一堆
    "RuntimeError: Event loop is closed"，而测试本身是通过的。
    """
    from app.api import deps
    from app.infra.db.session import get_db
    from app.main import create_app
    from app.modules.agent.tools.base import ToolRegistry
    from app.modules.agent.tools.exec import RunShellTool

    reg = ToolRegistry()
    reg.register(RunShellTool())

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[deps.get_registry] = lambda: reg
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


class TestNewSessionHasNoWorkDir:
    """
    新会话不该自动拿到工作目录。

    自动指向某个目录意味着用户在不知情的情况下让 agent 能读写那里。
    要它干活，先告诉它在哪干 —— 这个动作本身就是授权。
    """

    async def test_created_session_work_dir_empty(self, client: AsyncClient) -> None:
        r = await client.post("/api/sessions", json={"title": "新会话"})
        assert r.status_code == 201
        sid = r.json()["id"]

        d = await client.get(f"/api/sessions/{sid}")
        assert d.status_code == 200
        assert d.json()["work_dir"] == "", "新会话不该有工作目录"


class TestSetWorkDir:
    async def test_set_and_clear(self, client: AsyncClient, tmp_path: Path) -> None:
        sid = (await client.post("/api/sessions", json={"title": "t"})).json()["id"]
        target = tmp_path / "proj"
        target.mkdir()

        r = await client.patch(
            f"/api/sessions/{sid}", json={"work_dir": str(target)}
        )
        assert r.status_code == 200
        # 返回的是 resolve 过的绝对路径
        assert Path(r.json()["work_dir"]) == target.resolve()

        # 清除
        r2 = await client.patch(f"/api/sessions/{sid}", json={"work_dir": ""})
        assert r2.json()["work_dir"] == ""

    async def test_rejects_missing_dir(self, client: AsyncClient, tmp_path: Path) -> None:
        """
        不存在的目录必须在入口处拦掉。

        放过去的话之后每次工具调用都失败，而错误信息指向白名单，
        不指向"这个目录不存在"。
        """
        sid = (await client.post("/api/sessions", json={"title": "t"})).json()["id"]
        r = await client.patch(
            f"/api/sessions/{sid}", json={"work_dir": str(tmp_path / "nope")}
        )
        assert r.status_code == 400
        assert "不存在" in r.text

    async def test_rejects_file(self, client: AsyncClient, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("x", encoding="utf-8")
        sid = (await client.post("/api/sessions", json={"title": "t"})).json()["id"]
        r = await client.patch(f"/api/sessions/{sid}", json={"work_dir": str(f)})
        assert r.status_code == 400

    async def test_auto_grants_whitelist(
        self, client: AsyncClient, db, tmp_path: Path
    ) -> None:
        """
        设工作目录必须顺手加白名单。

        不加的话结果是"设了工作目录但 agent 读不了里面任何文件"——
        工具报"不在白名单内"，而用户刚指定了这个目录，
        完全想不到还要单独授权一次。
        """
        sid = (await client.post("/api/sessions", json={"title": "t"})).json()["id"]
        target = tmp_path / "ws"
        target.mkdir()
        await client.patch(f"/api/sessions/{sid}", json={"work_dir": str(target)})

        rows = list(
            (
                await db.execute(
                    select(PathWhitelist).where(PathWhitelist.session_id == sid)
                )
            ).scalars()
        )
        assert len(rows) == 1
        assert Path(rows[0].path) == target.resolve()
        assert rows[0].can_write == 1, "工作目录必须可写，只读的没有使用场景"

    async def test_switching_dir_drops_old_grant(
        self, client: AsyncClient, db, tmp_path: Path
    ) -> None:
        """换目录时旧的自动授权要撤掉，否则越用越多。"""
        sid = (await client.post("/api/sessions", json={"title": "t"})).json()["id"]
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()

        await client.patch(f"/api/sessions/{sid}", json={"work_dir": str(a)})
        await client.patch(f"/api/sessions/{sid}", json={"work_dir": str(b)})

        rows = list(
            (
                await db.execute(
                    select(PathWhitelist).where(PathWhitelist.session_id == sid)
                )
            ).scalars()
        )
        assert len(rows) == 1, "旧的自动条目没被撤掉"
        assert Path(rows[0].path) == b.resolve()


class TestSessionIsolation:
    """给 A 会话开的权限不该让 B 会话也拿到。"""

    async def test_scoped_guard_isolates(self) -> None:
        set_allowed([AllowedPath(path=Path("C:/global"), can_write=True)])
        with scoped_guard([AllowedPath(path=Path("C:/only_a"), can_write=True)]):
            g = get_guard()
            assert [str(x.path) for x in g.allowed] == [str(Path("C:/only_a"))]
            with pytest.raises(PathDeniedError):
                g.check(Path("C:/global/x.txt"))
        # 退出后恢复全局
        assert [str(x.path) for x in get_guard().allowed] == [str(Path("C:/global"))]

    async def test_nested_scope_restores_outer(self) -> None:
        """
        子代理在自己的 task 里跑，退出内层不能把外层的 guard 也清掉。

        用 reset(token) 而不是设回 None 就是为了这个。
        """
        with scoped_guard([AllowedPath(path=Path("C:/outer"), can_write=False)]):
            with scoped_guard([AllowedPath(path=Path("C:/inner"), can_write=False)]):
                assert "inner" in str(get_guard().allowed[0].path)
            assert "outer" in str(get_guard().allowed[0].path)

    async def test_load_merges_global_and_session(
        self, db, workspace_id: str, tmp_path: Path
    ) -> None:
        """
        会话级 + 全局要合并。

        只用会话级的话，没设工作目录的会话一条白名单都没有，
        连上传的图片都读不了。
        """
        g = tmp_path / "global"
        s = tmp_path / "mine"
        g.mkdir()
        s.mkdir()
        db.add(
            PathWhitelist(
                id=path_id(), session_id=None, path=str(g), can_write=0, builtin=1
            )
        )
        # session_id 必须指向真实存在的会话 —— 有外键约束，
        # 用编造的 id 会报 "FOREIGN KEY constraint failed"
        from app.modules.session import repo as srepo

        sess = await srepo.create_session(db, workspace_id=workspace_id, title="x")
        db.add(
            PathWhitelist(
                id=path_id(), session_id=sess.id, path=str(s), can_write=1, builtin=0
            )
        )
        await db.flush()

        allowed = await load_session_allowed(db, sess.id)
        paths = {str(a.path) for a in allowed}
        assert str(g) in paths
        assert str(s) in paths

        # 别的会话看不到 ses_x 的条目
        other_sess = await srepo.create_session(
            db, workspace_id=workspace_id, title="y"
        )
        other = await load_session_allowed(db, other_sess.id)
        assert str(s) not in {str(a.path) for a in other}
        assert str(g) in {str(a.path) for a in other}


class TestRefCandidatesFix:
    """
    `@` 文件补全的修复。

    原来这里是 `select(Workspace).limit(1)` —— 取任意一个工作区，
    恒等于项目自己的 workspace/（基本是空的）。结果 @ 永远显示
    "没有匹配的文件"，而用户的代码就在他指定的目录里。
    """

    async def test_no_session_reports_reason(self, client: AsyncClient) -> None:
        r = await client.get("/api/ref-candidates?kind=file&q=x")
        assert r.status_code == 200
        body = r.json()
        assert body["items"] == []
        # 必须区分"没设工作目录"和"目录里真的没有匹配"
        assert body.get("reason") == "no_work_dir"
        assert "工作目录" in body.get("hint", "")

    async def test_searches_session_work_dir(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        ws = tmp_path / "code"
        ws.mkdir()
        (ws / "main.py").write_text("print(1)", encoding="utf-8")
        (ws / "helper.py").write_text("x = 1", encoding="utf-8")

        sid = (await client.post("/api/sessions", json={"title": "t"})).json()["id"]
        await client.patch(f"/api/sessions/{sid}", json={"work_dir": str(ws)})

        r = await client.get(
            f"/api/ref-candidates?kind=file&q=main&session_id={sid}"
        )
        assert r.status_code == 200
        names = [i["name"] for i in r.json()["items"]]
        assert "main.py" in names, f"应该搜到 main.py，实际 {names}"

    async def test_skips_dependency_dirs(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        """node_modules 会把候选列表淹掉，用户搜 main 先看到几十个无关文件。"""
        ws = tmp_path / "app"
        (ws / "node_modules" / "pkg").mkdir(parents=True)
        (ws / "node_modules" / "pkg" / "main.js").write_text("x", encoding="utf-8")
        (ws / "main.py").write_text("y", encoding="utf-8")

        sid = (await client.post("/api/sessions", json={"title": "t"})).json()["id"]
        await client.patch(f"/api/sessions/{sid}", json={"work_dir": str(ws)})

        r = await client.get(
            f"/api/ref-candidates?kind=file&q=main&session_id={sid}"
        )
        paths = [i.get("path", "") for i in r.json()["items"]]
        assert not any("node_modules" in p for p in paths)


class TestWhitelistApi:
    """第 4 条抱怨：看不到白名单，也没有地方去改。"""

    async def test_list_global(self, client: AsyncClient) -> None:
        r = await client.get("/api/whitelist")
        assert r.status_code == 200
        assert isinstance(r.json()["items"], list)

    async def test_add_and_delete(self, client: AsyncClient, tmp_path: Path) -> None:
        d = tmp_path / "extra"
        d.mkdir()
        r = await client.post(
            "/api/whitelist", json={"path": str(d), "can_write": False}
        )
        assert r.status_code == 201
        item = r.json()
        assert Path(item["path"]) == d.resolve()
        assert item["can_write"] is False
        assert item["exists"] is True

        rd = await client.delete(f"/api/whitelist/{item['id']}")
        assert rd.status_code == 200

    async def test_duplicate_rejected(self, client: AsyncClient, tmp_path: Path) -> None:
        d = tmp_path / "dup"
        d.mkdir()
        await client.post("/api/whitelist", json={"path": str(d)})
        r = await client.post("/api/whitelist", json={"path": str(d)})
        assert r.status_code == 409

    async def test_reports_missing_path(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        """
        允许加不存在的目录（可能先授权再创建），但要标出来。

        不标的话目录被移走后界面还显示正常，而用户正纳闷
        为什么 agent 读不到文件。
        """
        r = await client.post(
            "/api/whitelist", json={"path": str(tmp_path / "later")}
        )
        assert r.status_code == 201
        assert r.json()["exists"] is False

    async def test_builtin_protected(self, client: AsyncClient, db) -> None:
        """内置项不可删也不可改权限 —— 删了 agent 完全不能读写文件。"""
        row = PathWhitelist(
            id=path_id(), session_id=None, path="C:/builtin_x", can_write=0, builtin=1
        )
        db.add(row)
        await db.flush()

        rd = await client.delete(f"/api/whitelist/{row.id}")
        assert rd.status_code == 400
        rp = await client.patch(
            f"/api/whitelist/{row.id}", json={"can_write": True}
        )
        assert rp.status_code == 400

    async def test_session_scope_in_list(
        self, client: AsyncClient, tmp_path: Path
    ) -> None:
        """带 session_id 时返回该会话生效的全部集合（会话级 + 全局）。"""
        sid = (await client.post("/api/sessions", json={"title": "t"})).json()["id"]
        d = tmp_path / "mine"
        d.mkdir()
        await client.post(
            f"/api/whitelist?session_id={sid}", json={"path": str(d), "can_write": True}
        )

        r = await client.get(f"/api/whitelist?session_id={sid}")
        paths = [i["path"] for i in r.json()["items"]]
        assert str(d.resolve()) in paths

        # 不带 session_id 只看全局，看不到会话级的
        r2 = await client.get("/api/whitelist")
        assert str(d.resolve()) not in [i["path"] for i in r2.json()["items"]]


class TestBrowse:
    """第 4 条的另一半：得能浏览目录才能选工作目录。"""

    async def test_empty_path_returns_roots(self, client: AsyncClient) -> None:
        r = await client.get("/api/browse")
        assert r.status_code == 200
        roots = r.json()["roots"]
        assert len(roots) > 0, "至少要有主目录和项目目录"
        assert any("主目录" in x["name"] for x in roots)

    async def test_lists_subdirs(self, client: AsyncClient, tmp_path: Path) -> None:
        (tmp_path / "sub1").mkdir()
        (tmp_path / "sub2").mkdir()
        (tmp_path / "f.txt").write_text("x", encoding="utf-8")

        r = await client.get(f"/api/browse?path={tmp_path}")
        assert r.status_code == 200
        body = r.json()
        names = [e["name"] for e in body["entries"]]
        assert "sub1" in names
        assert "sub2" in names
        # dirs_only 默认 True，文件不该出现
        assert "f.txt" not in names
        assert body["parent"] is not None, "要能回上一级，否则进深层就出不来"

    async def test_hides_dot_dirs(self, client: AsyncClient, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "src").mkdir()
        r = await client.get(f"/api/browse?path={tmp_path}")
        names = [e["name"] for e in r.json()["entries"]]
        assert ".git" not in names
        assert "src" in names

    async def test_missing_dir_404(self, client: AsyncClient, tmp_path: Path) -> None:
        r = await client.get(f"/api/browse?path={tmp_path / 'nope'}")
        assert r.status_code == 404
