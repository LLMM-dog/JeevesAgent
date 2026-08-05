"""
第二轮用户反馈的五条修复。

## 对应关系

1. 启用/禁用报"请求参数不合法" —— 前端用 body 而不是 json，
   请求少了 Content-Type，FastAPI 直接 422
2. 再次添加供应商报"无法添加" —— 应该并入已有分组并去重模型
3. 加模型要手敲名字 —— 应该自动拉可用列表 + 模糊搜索
4. 切换模型后按钮文字不更新 —— zustand 状态没被 invalidateQueries 影响
5. 工作目录按钮该直接显示当前目录，长路径优先保留尾部
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest_asyncio
from app.core.crypto import encrypt
from app.core.ids import provider_id
from app.modules.provider.models import Model, Provider
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / "frontend" / "src"


@pytest_asyncio.fixture
async def client(db: AsyncSession, workspace_id: str) -> Any:
    from app.api import deps
    from app.infra.db.session import get_db
    from app.main import create_app
    from app.modules.agent.tools.base import ToolRegistry

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[deps.get_registry] = lambda: ToolRegistry()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    app.dependency_overrides.clear()


class TestApiUsesJsonNotBody:
    """
    第 1 条：点启用/禁用报"请求参数不合法"。

    ## 根因

    api.ts 的 request() 只在传了 json 时才设 Content-Type：

        if (json !== undefined) headers.set("Content-Type", "application/json");

    用 `body: JSON.stringify(...)` 的话请求没有 JSON 内容类型，
    FastAPI 返回 422 "请求参数不合法"—— 而那句话完全不指向
    "少了个请求头"，所以现象是"点一下就报参数不合法"。

    上一轮我加的所有写接口都踩了这个，因为没看 request() 的签名。
    """

    def test_no_raw_body_stringify(self) -> None:
        src = (FRONT / "lib" / "api.ts").read_text(encoding="utf-8")
        hits = re.findall(r"body: JSON\.stringify\(", src)
        assert not hits, (
            f"还有 {len(hits)} 处用 body: JSON.stringify —— "
            "这些请求少了 Content-Type，会被后端 422 拒掉"
        )

    def test_request_sets_content_type_only_for_json(self) -> None:
        """
        锁住这个前提。哪天 request() 改成对 body 也设 Content-Type，
        上面那条测试就可以放宽 —— 但现在不行。
        """
        src = (FRONT / "lib" / "api.ts").read_text(encoding="utf-8")
        assert 'if (json !== undefined) headers.set("Content-Type"' in src

    async def test_patch_model_without_content_type_is_422(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """
        直接验后端行为：不带 Content-Type 就是 422。

        这条不是在测我们的代码，是在锁住"为什么必须用 json"——
        以后有人想改回 body 时，这条会告诉他后果。
        """
        p = Provider(
            id=provider_id(),
            name="ct-test",
            base_url="https://example.com/v1",
            api_key_cipher=encrypt("sk-test-x-000000000000"),
        )
        db.add(p)
        await db.flush()
        m = Model(
            id="mdl_cttest0000000000000000",
            provider_id=p.id,
            model_id="m1",
            context_window=8192,
            enabled=1,
        )
        db.add(m)
        await db.flush()

        # 【必须显式设成别的类型】。
        #
        # httpx 传 content= 时会自动补 Content-Type，所以"什么都不设"
        # 在这里测不出来 —— 而浏览器 fetch 不会自动补，那才是真实场景。
        #
        # 用 text/plain 模拟"内容类型不对"，效果和缺失一样：
        # FastAPI 拿不到 JSON body，返回 422。
        r = await client.patch(
            f"/api/models/{m.id}",
            content=b'{"enabled": false}',
            headers={"Content-Type": "text/plain"},
        )
        assert r.status_code == 422, (
            f"内容类型不对却返回 {r.status_code} —— "
            "那这条测试就锁不住 json/body 的区别了"
        )
        assert "参数不合法" in r.text

        # 带正确类型就正常
        r2 = await client.patch(
            f"/api/models/{m.id}",
            content=b'{"enabled": false}',
            headers={"Content-Type": "application/json"},
        )
        assert r2.status_code == 200
        assert r2.json()["enabled"] is False


class TestProviderUpsert:
    """
    第 2 条：再次添加供应商报"无法添加"。

    用户的实际路径是"先加端点，之后想再加几个模型，于是又走一次
    添加供应商"—— 他不想新建供应商，只想往里加模型。
    """

    async def test_same_name_merges(self, client: AsyncClient) -> None:
        body = {
            "name": "同名测试",
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-test-first-000000",
            "models": [{"model_id": "m-a", "context_window": 8192}],
        }
        r1 = await client.post("/api/providers", json=body)
        assert r1.status_code == 201
        pid = r1.json()["id"]

        # 第二次：加一个新模型
        body2 = dict(body, models=[{"model_id": "m-b", "context_window": 8192}])
        r2 = await client.post("/api/providers", json=body2)
        assert r2.status_code == 201, r2.text
        assert r2.json()["id"] == pid, "同名应该并入同一个供应商，不是新建"
        assert r2.json()["model_count"] == 2

    async def test_same_endpoint_and_key_merges(self, client: AsyncClient) -> None:
        """
        名字不同但端点和 Key 相同 —— 也该并入。
        用户可能第二次随手起了个别的名字。
        """
        base = {
            "base_url": "https://api.same.com/v1",
            "api_key": "sk-test-identical-11111",
            "models": [{"model_id": "x-1", "context_window": 8192}],
        }
        r1 = await client.post("/api/providers", json=dict(base, name="第一次"))
        r2 = await client.post(
            "/api/providers",
            json=dict(base, name="第二次", models=[{"model_id": "x-2"}]),
        )
        assert r1.json()["id"] == r2.json()["id"]

    async def test_different_key_stays_separate(self, client: AsyncClient) -> None:
        """
        同端点但不同 Key 要分开 —— 个人额度和团队额度是合理场景。
        """
        base = {
            "base_url": "https://api.split.com/v1",
            "models": [{"model_id": "s-1", "context_window": 8192}],
        }
        r1 = await client.post(
            "/api/providers", json=dict(base, name="个人", api_key="sk-fake-personal-0001")
        )
        r2 = await client.post(
            "/api/providers", json=dict(base, name="团队", api_key="sk-fake-team-0002")
        )
        assert r1.json()["id"] != r2.json()["id"]

    async def test_duplicate_models_deduped(self, client: AsyncClient) -> None:
        """
        重复的模型要静默跳过。

        不去重的话 (provider_id, model_id) 唯一索引会抛 IntegrityError，
        而那个报错指向数据库约束，完全不提示"这个模型你已经加过了"。
        """
        body = {
            "name": "去重测试",
            "base_url": "https://api.dedup.com/v1",
            "api_key": "sk-test-dedup-000022",
            "models": [
                {"model_id": "same-one", "context_window": 8192},
                {"model_id": "same-one", "context_window": 8192},
            ],
        }
        r = await client.post("/api/providers", json=body)
        assert r.status_code == 201
        assert r.json()["model_count"] == 1, "同一次请求里的重复也要去掉"

        # 再来一次，仍然只有一个
        r2 = await client.post("/api/providers", json=body)
        assert r2.json()["model_count"] == 1

    async def test_merge_updates_key(self, client: AsyncClient, db: AsyncSession) -> None:
        """
        并入时要更新 Key —— 用户重复走这个流程通常就是来换 Key 的，
        沿用旧的会让他以为改了但没生效。
        """
        body = {
            "name": "换key测试",
            "base_url": "https://api.rekey.com/v1",
            "api_key": "sk-test-rotated-000001",
            "models": [{"model_id": "k-1", "context_window": 8192}],
        }
        r1 = await client.post("/api/providers", json=body)
        old_hint = r1.json()["key_hint"]

        r2 = await client.post(
            "/api/providers", json=dict(body, api_key="sk-test-rotated-000002")
        )
        assert r2.json()["key_hint"] != old_hint


class TestAvailableModels:
    """第 3 条：加模型该自动拉列表，不该让用户手敲。"""

    async def test_endpoint_exists(self, client: AsyncClient) -> None:
        r = await client.get("/api/providers/prv_nope/available-models")
        # 供应商不存在是 404，不是 405 —— 说明路由注册了
        assert r.status_code == 404

    def test_form_has_fuzzy_search(self) -> None:
        f = FRONT / "components" / "AddModelForm.tsx"
        assert f.is_file(), "AddModelForm 不存在"
        src = f.read_text(encoding="utf-8")
        # 模糊匹配，不是精确 includes
        assert "indexOf(ch, i)" in src, "没有模糊匹配"
        assert "availableModels" in src, "没有拉取可用列表"

    def test_form_allows_manual_entry(self) -> None:
        """
        有些端点不实现 /v1/models。拉不到时必须还能手填，
        否则这个供应商就没法加模型了。
        """
        src = (FRONT / "components" / "AddModelForm.tsx").read_text(encoding="utf-8")
        assert "拉不到模型列表" in src, "没有处理拉取失败"
        assert "直接输入模型 ID" in src or "也可以" in src

    def test_form_marks_already_added(self) -> None:
        src = (FRONT / "components" / "AddModelForm.tsx").read_text(encoding="utf-8")
        assert "already_added" in src
        assert "已添加" in src

    def test_panel_uses_form(self) -> None:
        src = (FRONT / "components" / "ModelsPanel.tsx").read_text(encoding="utf-8")
        assert "<AddModelForm" in src
        # 旧的裸输入框不该还在
        assert "模型 ID，如 gpt-4o-mini" not in src


class TestStoreSyncAfterSwitch:
    """
    第 4 条：切换模型后按钮文字不更新。

    ## 根因

    modelPk / workDir 存在 zustand 里，而 invalidateQueries 只影响
    react-query 的缓存 —— 两套状态互不相干。只 invalidate 的结果是：
    请求发出去了、库里也改了，但按钮上的文字还是旧的，要刷新页面才对。
    用户看到的就是"切了没反应"。
    """

    def test_switcher_uses_store_setter(self) -> None:
        src = (FRONT / "components" / "ModelSwitcher.tsx").read_text(encoding="utf-8")
        assert "setWorkModel" in src, "ModelSwitcher 没走 store setter"
        assert "api.patchSession" not in src, "还在直接调 api，store 不会更新"

    def test_workdir_uses_store_setter(self) -> None:
        """WorkDirPicker 有同一个 bug。"""
        src = (FRONT / "components" / "WorkDirPicker.tsx").read_text(encoding="utf-8")
        assert "setWorkDirInStore" in src or "s.setWorkDir" in src
        assert "api.patchSession" not in src

    def test_store_has_both_setters(self) -> None:
        src = (FRONT / "store" / "chat.ts").read_text(encoding="utf-8")
        assert "async setWorkModel(" in src
        assert "async setWorkDir(" in src
        # 都要用后端返回值，不做乐观更新 —— 后端会拒绝禁用的模型
        assert "s.model_pk" in src
        assert "s.work_dir" in src


class TestWorkDirLabel:
    """
    第 5 条：工作目录按钮该直接显示当前目录。
    """

    def test_shows_current_dir(self) -> None:
        src = (FRONT / "components" / "WorkDirPicker.tsx").read_text(encoding="utf-8")
        assert "当前：" in src, "折叠时没显示当前目录"

    def test_truncates_keeping_tail(self) -> None:
        """
        长路径要优先保留尾部：信息量集中在末尾几段（项目名、子目录），
        开头往往是 C:\\Users\\某某\\Documents 这类所有路径都一样的前缀。
        从头截断的话十个目录看起来全都一样。
        """
        src = (FRONT / "components" / "WorkDirPicker.tsx").read_text(encoding="utf-8")
        assert "shortDir" in src
        # 从后往前拼
        assert "for (let i = parts.length - 1" in src
        assert "优先保留" in src, "没解释为什么保尾部"
