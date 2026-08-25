"""
模型个体管理、会话级模型选择、人格文件编辑。

## 对应的用户诉求

- 模型配置不该以模型组为整体：加一个模型不该重建整个模型组
- 设置页要能启用/禁用模型，控制对话页切换菜单里出现什么
- 对话页要能快捷切模型，且只影响当前对话
- 缺人格/个人偏好的设置入口
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from app.core.ids import binding_id, endpoint_id, model_id
from app.modules.endpoint.models import Endpoint, Model, ModelBinding
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


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


@pytest_asyncio.fixture
async def endpoint(db: AsyncSession) -> Endpoint:
    from app.core.crypto import encrypt

    p = Endpoint(
        id=endpoint_id(),
        name="测试端点",
        base_url="https://example.com/v1",
        api_key_cipher=encrypt("sk-test-key-1234567890"),
    )
    db.add(p)
    await db.flush()
    return p


@pytest_asyncio.fixture
async def model(db: AsyncSession, endpoint: Endpoint) -> Model:
    m = Model(
        id=model_id(),
        endpoint_id=endpoint.id,
        model_id="test-chat",
        display_name="测试模型",
        context_window=32768,
        enabled=1,
    )
    db.add(m)
    await db.flush()
    return m


class TestModelIndividually:
    """加/删模型不该动端点。"""

    async def test_add_single_model(
        self, client: AsyncClient, endpoint: Endpoint
    ) -> None:
        r = await client.post(
            "/api/models",
            json={
                "endpoint_id": endpoint.id,
                "model_id": "new-model",
                "display_name": "新模型",
                "context_window": 65536,
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["model_id"] == "new-model"
        assert body["enabled"] is True
        # 带上端点名，省前端一次往返
        assert body["endpoint_name"] == "测试端点"

    async def test_reject_duplicate(
        self, client: AsyncClient, endpoint: Endpoint, model: Model
    ) -> None:
        r = await client.post(
            "/api/models",
            json={"endpoint_id": endpoint.id, "model_id": model.model_id},
        )
        assert r.status_code == 409

    async def test_reject_unknown_provider(self, client: AsyncClient) -> None:
        r = await client.post(
            "/api/models", json={"endpoint_id": "prv_nope", "model_id": "x"}
        )
        assert r.status_code == 404

    async def test_delete_single_model(
        self, client: AsyncClient, model: Model
    ) -> None:
        r = await client.delete(f"/api/models/{model.id}")
        assert r.status_code == 200

    async def test_delete_refuses_when_bound(
        self, client: AsyncClient, db: AsyncSession, model: Model
    ) -> None:
        """
        被功能位绑定的模型不能直接删。

        删了那个功能位就悬空 —— 下次对话报"未配置模型"，
        而用户只是删了一个看起来没在用的模型，联系不起来。
        """
        db.add(
            ModelBinding(
                id=binding_id(), agent_name="", purpose="chat", model_pk=model.id
            )
        )
        await db.flush()

        r = await client.delete(f"/api/models/{model.id}")
        assert r.status_code == 409
        # 错误信息要说清是哪个功能位
        assert "对话" in r.text


class TestEnableDisable:
    """禁用只影响对话页的切换菜单，不动配置。"""

    async def test_toggle_enabled(self, client: AsyncClient, model: Model) -> None:
        r = await client.patch(f"/api/models/{model.id}", json={"enabled": False})
        assert r.status_code == 200
        assert r.json()["enabled"] is False

        r2 = await client.patch(f"/api/models/{model.id}", json={"enabled": True})
        assert r2.json()["enabled"] is True

    async def test_disable_auto_unbinds(
        self, client: AsyncClient, db: AsyncSession, model: Model
    ) -> None:
        """禁用被绑定的模型 → 自动解绑，功能位变空，而不是报错。"""
        db.add(
            ModelBinding(id=binding_id(), agent_name="", purpose="chat", model_pk=model.id)
        )
        db.add(
            ModelBinding(id=binding_id(), agent_name="", purpose="memory", model_pk=model.id)
        )
        await db.flush()

        r = await client.patch(f"/api/models/{model.id}", json={"enabled": False})
        assert r.status_code == 200
        assert r.json()["enabled"] is False

        remaining = (
            await db.execute(select(ModelBinding).where(ModelBinding.model_pk == model.id))
        ).scalars().all()
        assert remaining == []

    async def test_list_returns_bindings(
        self, client: AsyncClient, db: AsyncSession, model: Model
    ) -> None:
        """模型卡片要显示被配置成了什么功能，列表接口需返回 bindings。"""
        db.add(
            ModelBinding(id=binding_id(), agent_name="", purpose="chat", model_pk=model.id)
        )
        db.add(
            ModelBinding(
                id=binding_id(), agent_name="", purpose="memory_rerank", model_pk=model.id
            )
        )
        await db.flush()

        items = (await client.get("/api/models")).json()["items"]
        m = next(x for x in items if x["id"] == model.id)
        assert sorted(m["bindings"]) == ["chat", "memory_rerank"]

    async def test_list_bindings_empty_when_unbound(
        self, client: AsyncClient, model: Model
    ) -> None:
        items = (await client.get("/api/models")).json()["items"]
        m = next(x for x in items if x["id"] == model.id)
        assert m["bindings"] == []

    async def test_enabled_only_filters(
        self, client: AsyncClient, db: AsyncSession, endpoint: Endpoint, model: Model
    ) -> None:
        off = Model(
            id=model_id(),
            endpoint_id=endpoint.id,
            model_id="disabled-one",
            context_window=8192,
            enabled=0,
        )
        db.add(off)
        await db.flush()

        all_ = (await client.get("/api/models")).json()["items"]
        assert {m["model_id"] for m in all_} >= {"test-chat", "disabled-one"}

        # 对话页用这个 —— 禁用的不该出现
        on = (await client.get("/api/models?enabled_only=true")).json()["items"]
        ids = {m["model_id"] for m in on}
        assert "test-chat" in ids
        assert "disabled-one" not in ids

    async def test_disabled_stays_configured(
        self, client: AsyncClient, db: AsyncSession, model: Model
    ) -> None:
        """禁用不是删除 —— 设置页仍要看到它，否则没法重新启用。"""
        await client.patch(f"/api/models/{model.id}", json={"enabled": False})
        still = (
            await db.execute(select(Model).where(Model.id == model.id))
        ).scalars().first()
        assert still is not None
        assert still.enabled == 0

    async def test_rejects_tiny_window(
        self, client: AsyncClient, model: Model
    ) -> None:
        r = await client.patch(f"/api/models/{model.id}", json={"context_window": 10})
        assert r.status_code == 400


class TestSessionModelChoice:
    """对话页切模型只影响当前对话。"""

    async def test_set_and_clear(self, client: AsyncClient, model: Model) -> None:
        sid = (await client.post("/api/sessions", json={"title": "t"})).json()["id"]
        # 新会话跟随默认绑定
        assert (await client.get(f"/api/sessions/{sid}")).json()["model_pk"] == ""

        r = await client.patch(f"/api/sessions/{sid}", json={"model_pk": model.id})
        assert r.status_code == 200
        assert r.json()["model_pk"] == model.id

        r2 = await client.patch(f"/api/sessions/{sid}", json={"model_pk": ""})
        assert r2.json()["model_pk"] == ""

    async def test_rejects_unknown_model(self, client: AsyncClient) -> None:
        sid = (await client.post("/api/sessions", json={"title": "t"})).json()["id"]
        r = await client.patch(f"/api/sessions/{sid}", json={"model_pk": "mdl_nope"})
        assert r.status_code == 400

    async def test_rejects_disabled_model(
        self, client: AsyncClient, model: Model
    ) -> None:
        """
        禁用的模型不出现在菜单里，能传上来说明前端数据过期。

        静默接受的话用户以为切成功了，实际下一轮又回到默认模型，
        而没有任何提示。
        """
        await client.patch(f"/api/models/{model.id}", json={"enabled": False})
        sid = (await client.post("/api/sessions", json={"title": "t"})).json()["id"]
        r = await client.patch(f"/api/sessions/{sid}", json={"model_pk": model.id})
        assert r.status_code == 400
        assert "禁用" in r.text

    async def test_only_affects_that_session(
        self, client: AsyncClient, model: Model
    ) -> None:
        a = (await client.post("/api/sessions", json={"title": "a"})).json()["id"]
        b = (await client.post("/api/sessions", json={"title": "b"})).json()["id"]
        await client.patch(f"/api/sessions/{a}", json={"model_pk": model.id})
        assert (await client.get(f"/api/sessions/{b}")).json()["model_pk"] == ""

    async def test_override_only_for_chat(
        self, client: AsyncClient, db: AsyncSession, endpoint: Endpoint, model: Model
    ) -> None:
        """
        override 只管 chat。

        标题生成和上下文压缩是后台动作，用便宜模型是有意的配置 ——
        跟着切会让每次压缩都烧贵模型的 token，而用户看不到这件事。
        """
        from app.modules.endpoint import service as ps

        cheap = Model(
            id=model_id(),
            endpoint_id=endpoint.id,
            model_id="cheap-title",
            context_window=8192,
            enabled=1,
        )
        db.add(cheap)
        db.add(
            ModelBinding(
                id=binding_id(), agent_name="", purpose="chat", model_pk=cheap.id
            )
        )
        db.add(
            ModelBinding(
                id=binding_id(), agent_name="", purpose="title", model_pk=cheap.id
            )
        )
        await db.flush()

        chat = await ps.resolve(db, purpose="chat", override_pk=model.id)
        assert chat.model_id == model.model_id, "chat 应该用 override"

        title = await ps.resolve(db, purpose="title", override_pk=model.id)
        assert title.model_id == "cheap-title", "title 不该被 override 影响"

    async def test_missing_override_falls_back(
        self, client: AsyncClient, db: AsyncSession, endpoint: Endpoint, model: Model
    ) -> None:
        """
        模型被删后 model_pk 悬空（没有外键）。

        这时报错会让整个会话打不开，回落到默认绑定至少能继续用。
        """
        from app.modules.endpoint import service as ps

        db.add(
            ModelBinding(
                id=binding_id(), agent_name="", purpose="chat", model_pk=model.id
            )
        )
        await db.flush()
        r = await ps.resolve(db, purpose="chat", override_pk="mdl_deleted")
        assert r.model_id == model.model_id


class TestPersonas:
    """提示词读取无缓存。"""

    async def test_takes_effect_without_restart(self) -> None:
        """
        prompts.py 的 _read 不能有缓存。

        加了缓存的话改完要重启才生效，而用户会以为"改了没用"。
        """
        src = Path("backend/app/modules/agent/prompts.py").read_text(encoding="utf-8")
        # 找 _read 的定义，确认它上面没有 lru_cache 装饰器
        idx = src.index("def _read(")
        before = src[max(0, idx - 200) : idx]
        assert "lru_cache" not in before, "_read 不该被缓存"


@pytest.mark.parametrize("purpose", ["chat", "vision", "title", "compact"])
async def test_binding_purposes_unchanged(
    client: AsyncClient, model: Model, purpose: str
) -> None:
    """功能位绑定仍然按 purpose 工作 —— 这次改动不该破坏它。"""
    r = await client.put(
        "/api/bindings", json={"purpose": purpose, "model_pk": model.id}
    )
    assert r.status_code in (200, 201), r.text


class TestGuessEndpointName:
    """"添加模型"自动分组的名字从地址推断。"""

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://api.deepseek.com/v1", "DeepSeek"),
            ("https://api.siliconflow.cn/v1", "SiliconFlow"),
            ("https://api.openai.com/v1", "OpenAI"),
            ("https://open.bigmodel.cn/api/paas/v4", "智谱"),
            ("http://localhost:11434", "本地"),
            ("https://api.moonshot.cn/v1", "Moonshot"),
            # 未知主机取第一个非通用段
            ("https://api.somevendor.io/v1", "Somevendor"),
            ("https://foo.bar.example.co/v1", "Foo"),
        ],
    )
    def test_cases(self, url: str, expected: str) -> None:
        from app.modules.endpoint import service as ps

        assert ps.guess_endpoint_name(url) == expected

    def test_unparseable_falls_back(self) -> None:
        from app.modules.endpoint import service as ps

        assert ps.guess_endpoint_name("not a url") == "自定义"


class TestMoveModelAcrossGroups:
    """拖动改分组 = PATCH 模型的 endpoint_id。绑定不受影响。"""

    async def test_move_to_other_endpoint(
        self, client: AsyncClient, endpoint: Endpoint, model: Model
    ) -> None:
        r = await client.post(
            "/api/endpoints",
            json={
                "name": "另一组",
                "base_url": "https://other.example.com/v1",
                "api_key": "sk-other",
            },
        )
        other_id = r.json()["id"]

        r2 = await client.patch(
            f"/api/models/{model.id}", json={"endpoint_id": other_id}
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["endpoint_id"] == other_id
        assert r2.json()["endpoint_name"] == "另一组"

    async def test_move_rejects_unknown_group(
        self, client: AsyncClient, model: Model
    ) -> None:
        r = await client.patch(
            f"/api/models/{model.id}", json={"endpoint_id": "prv_nope"}
        )
        assert r.status_code == 404

    async def test_move_rejects_duplicate(
        self, client: AsyncClient, db: AsyncSession, endpoint: Endpoint, model: Model
    ) -> None:
        """目标分组已有同名模型时拒绝，避免撞唯一索引。"""
        from app.core.crypto import encrypt

        other = Endpoint(
            id=endpoint_id(),
            name="另一组",
            base_url="https://dup.example.com/v1",
            api_key_cipher=encrypt("sk-dup-key-00000001"),
        )
        db.add(other)
        db.add(
            Model(
                id=model_id(),
                endpoint_id=other.id,
                model_id=model.model_id,
                context_window=8192,
            )
        )
        await db.flush()

        r = await client.patch(
            f"/api/models/{model.id}", json={"endpoint_id": other.id}
        )
        assert r.status_code == 409

    async def test_move_keeps_binding(
        self, client: AsyncClient, db: AsyncSession, model: Model
    ) -> None:
        """绑定引用 model_pk，跨组移动后绑定仍指向同一个模型。"""
        db.add(
            ModelBinding(
                id=binding_id(), agent_name="", purpose="chat", model_pk=model.id
            )
        )
        await db.flush()

        r = await client.post(
            "/api/endpoints",
            json={
                "name": "另一组",
                "base_url": "https://keep.example.com/v1",
                "api_key": "sk-keep",
            },
        )
        other_id = r.json()["id"]

        r2 = await client.patch(
            f"/api/models/{model.id}", json={"endpoint_id": other_id}
        )
        assert r2.status_code == 200

        bindings = (await client.get("/api/bindings")).json()["items"]
        chat = next(b for b in bindings if b["purpose"] == "chat")
        assert chat["model_pk"] == model.id


class TestUpdateEndpoint:
    """编辑分组：改名字 / 地址 / Key，Key 留空等于不改。"""

    async def test_update_name_and_url(
        self, client: AsyncClient, endpoint: Endpoint
    ) -> None:
        r = await client.patch(
            f"/api/endpoints/{endpoint.id}",
            json={"name": "新名字", "base_url": "https://new.example.com/v1"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "新名字"
        assert body["base_url"] == "https://new.example.com/v1"

    async def test_empty_key_keeps_existing(
        self, client: AsyncClient, endpoint: Endpoint
    ) -> None:
        """Key 留空不能把原 Key 清掉 —— 前端拿不到明文。"""
        before = (await client.get("/api/endpoints")).json()["items"]
        hint = next(p["key_hint"] for p in before if p["id"] == endpoint.id)

        r = await client.patch(
            f"/api/endpoints/{endpoint.id}", json={"name": "改名", "api_key": ""}
        )
        assert r.status_code == 200
        assert r.json()["key_hint"] == hint

    async def test_update_key_rotates_hint(
        self, client: AsyncClient, endpoint: Endpoint
    ) -> None:
        before = (await client.get("/api/endpoints")).json()["items"]
        old = next(p["key_hint"] for p in before if p["id"] == endpoint.id)

        r = await client.patch(
            f"/api/endpoints/{endpoint.id}", json={"api_key": "sk-brand-new-key-99"}
        )
        assert r.status_code == 200
        assert r.json()["key_hint"] == "y-99"
        assert r.json()["key_hint"] != old


class TestOverrideReachesRealCall:
    """
    切换的模型必须传到【真正执行的那次 resolve】。

    ## 这个测试对应的真实 bug

    prepare() 里有一次 resolve 做预检（提前发现"没配模型"好在 400
    里报出来，而不是等流开始后才失败），_run_agent 里还有一次 ——
    后者才是真正跑的。

    我第一次只改了预检那处。现象是：界面上切换成功、会话字段也存了，
    但追踪里显示用的还是默认模型。看起来像追踪的 bug，
    实际是 override 根本没到执行路径。

    真实模型验证抓到了它 —— 单测当时是全过的。
    """

    def test_run_agent_takes_model_pk(self) -> None:
        src = Path("backend/app/modules/agent/chat_service.py").read_text(
            encoding="utf-8"
        )
        # _run_agent 的签名里必须有 model_pk
        idx = src.index("async def _run_agent(")
        sig = src[idx : src.index(") -> None:", idx)]
        assert "model_pk" in sig, "_run_agent 没收 model_pk —— 切换不会生效"

    def test_run_agent_passes_override(self) -> None:
        src = Path("backend/app/modules/agent/chat_service.py").read_text(
            encoding="utf-8"
        )
        idx = src.index("async def _run_agent(")
        body = src[idx : idx + 3000]
        assert "override_pk" in body, (
            "_run_agent 里的 resolve 没传 override_pk"
        )

    def test_caller_passes_it(self) -> None:
        src = Path("backend/app/modules/agent/chat_service.py").read_text(
            encoding="utf-8"
        )
        assert "model_pk=prep.model_pk" in src, "调用 _run_agent 时没传 model_pk"

    def test_prepared_chat_carries_it(self) -> None:
        """
        PreparedChat 要带着它 —— produce() 在另一个 task 里跑，
        拿不到原来的 session 对象。
        """
        from app.modules.agent.chat_service import PreparedChat

        assert "model_pk" in PreparedChat.__annotations__
