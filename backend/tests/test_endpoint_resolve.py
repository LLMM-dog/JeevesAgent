"""
端点解析服务测试 —— resolve、绑定、创建、删除等。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from app.core.crypto import encrypt
from app.core.ids import endpoint_id, model_id
from app.modules.endpoint import service as ps
from app.modules.endpoint.models import Model
from sqlalchemy.ext.asyncio import AsyncSession


@pytest_asyncio.fixture
async def endpoint(db: AsyncSession) -> ps.Endpoint:
    p = ps.Endpoint(
        id=endpoint_id(),
        name="resolve 测试端点",
        base_url="https://resolve.example.com/v1",
        api_key_cipher=encrypt("sk-resolve-key-000001"),
    )
    db.add(p)
    await db.flush()
    return p


@pytest_asyncio.fixture
async def chat_model(db: AsyncSession, endpoint: ps.Endpoint) -> Model:
    m = Model(
        id=model_id(),
        endpoint_id=endpoint.id,
        model_id="resolve-chat-model",
        context_window=65536,
        enabled=1,
    )
    db.add(m)
    await db.flush()
    return m


@pytest_asyncio.fixture
async def compact_model(db: AsyncSession, endpoint: ps.Endpoint) -> Model:
    m = Model(
        id=model_id(),
        endpoint_id=endpoint.id,
        model_id="resolve-compact-model",
        context_window=8192,
        enabled=1,
    )
    db.add(m)
    await db.flush()
    return m


class TestResolve:
    async def test_resolve_chat_with_global_default(
        self, db: AsyncSession, chat_model: Model
    ) -> None:
        """绑定全局 chat 位后应能解析。"""
        await ps.set_binding(db, purpose="chat", model_pk=chat_model.id)
        result = await ps.resolve(db, purpose="chat")
        assert result.model_id == "resolve-chat-model"
        assert result.context_window == 65536

    async def test_resolve_fails_without_binding(self, db: AsyncSession) -> None:
        """无任何绑定时抛出 NoModelBoundError。"""
        from app.core.exceptions import NoModelBoundError

        with pytest.raises(NoModelBoundError):
            await ps.resolve(db, purpose="chat")

    async def test_resolve_with_override_pk(
        self, db: AsyncSession, endpoint: ps.Endpoint, chat_model: Model
    ) -> None:
        """override_pk 优先级最高。"""
        await ps.set_binding(db, purpose="chat", model_pk=chat_model.id)
        alt = Model(
            id=model_id(),
            endpoint_id=endpoint.id,
            model_id="override-model",
            context_window=128000,
            enabled=1,
        )
        db.add(alt)
        await db.flush()
        result = await ps.resolve(db, purpose="chat", override_pk=alt.id)
        assert result.model_id == "override-model"

    async def test_resolve_override_uses_disabled_too(
        self, db: AsyncSession, chat_model: Model
    ) -> None:
        """已禁用的 override 仍生效（用户可能先选了再在设置页禁用）。"""
        await ps.set_binding(db, purpose="chat", model_pk=chat_model.id)
        disabled = Model(
            id=model_id(),
            endpoint_id=chat_model.endpoint_id,
            model_id="disabled-override",
            context_window=4096,
            enabled=0,
        )
        db.add(disabled)
        await db.flush()
        result = await ps.resolve(db, purpose="chat", override_pk=disabled.id)
        assert result.model_id == "disabled-override"

    async def test_resolve_agent_specific_over_global(
        self, db: AsyncSession, endpoint: ps.Endpoint, chat_model: Model, compact_model: Model
    ) -> None:
        """agent 级绑定优先于全局绑定。"""
        await ps.set_binding(db, purpose="compact", model_pk=chat_model.id)
        await ps.set_binding(
            db, purpose="compact", model_pk=compact_model.id, agent_name="researcher"
        )
        result = await ps.resolve(db, purpose="compact", agent_name="researcher")
        assert result.model_id == "resolve-compact-model"

    async def test_resolve_fallback_to_chat(
        self, db: AsyncSession, chat_model: Model
    ) -> None:
        """compact 位未绑定时回退到 chat 位。"""
        await ps.set_binding(db, purpose="chat", model_pk=chat_model.id)
        result = await ps.resolve(db, purpose="compact")
        assert result.model_id == "resolve-chat-model"

    async def test_resolve_with_pricing(
        self, db: AsyncSession, endpoint: ps.Endpoint
    ) -> None:
        """模型定价信息应透传到 ResolvedModel。"""
        m = Model(
            id=model_id(),
            endpoint_id=endpoint.id,
            model_id="priced-model",
            context_window=32768,
            enabled=1,
            price_in_per_1m=1.25,
            price_out_per_1m=2.50,
        )
        db.add(m)
        await db.flush()
        await ps.set_binding(db, purpose="chat", model_pk=m.id)
        result = await ps.resolve(db, purpose="chat")
        assert result.price_in_per_1m == 1.25
        assert result.price_out_per_1m == 2.50


class TestCreateEndpoint:
    async def test_create_endpoint_basic(self, db: AsyncSession) -> None:
        p = await ps.create_endpoint(
            db,
            name="测试创建",
            base_url="https://create.example.com/v1",
            api_key="sk-create-key-000001",
            models=[],
        )
        assert p.name == "测试创建"
        assert p.base_url == "https://create.example.com/v1"
        assert p.key_hint == "0001"

    async def test_create_endpoint_with_models(self, db: AsyncSession) -> None:
        p = await ps.create_endpoint(
            db,
            name="带模型",
            base_url="https://with-models.example.com/v1",
            api_key="sk-models-key-000001",
            models=[
                {"model_id": "model-alpha", "display_name": "Alpha"},
                {"model_id": "model-beta", "display_name": "Beta", "context_window": 131072},
            ],
        )
        assert p.name == "带模型"
        models = await ps.list_models(db)
        assert len(models) == 2

    async def test_create_endpoint_dedup_by_name(self, db: AsyncSession) -> None:
        """同名端点应合并而非创建新记录。"""
        p1 = await ps.create_endpoint(
            db, name="同名测试", base_url="https://dup1.example.com/v1", api_key="sk-1111", models=[]
        )
        p2 = await ps.create_endpoint(
            db, name="同名测试", base_url="https://dup2.example.com/v1", api_key="sk-2222", models=[]
        )
        assert p1.id == p2.id
        assert p2.base_url == "https://dup2.example.com/v1"


class TestListEndpoints:
    async def test_list_with_model_count(self, db: AsyncSession) -> None:
        await ps.create_endpoint(
            db, name="有模型的", base_url="https://x.com/v1", api_key="sk-count",
            models=[{"model_id": "a"}, {"model_id": "b"}, {"model_id": "c"}],
        )
        items = await ps.list_endpoints(db)
        assert len(items) == 1
        p, count = items[0]
        assert count == 3

    async def test_delete_endpoint(self, db: AsyncSession) -> None:
        p = await ps.create_endpoint(
            db, name="待删", base_url="https://del.example.com/v1", api_key="sk-del", models=[]
        )
        await ps.delete_endpoint(db, p.id)
        items = await ps.list_endpoints(db)
        assert len(items) == 0


class TestHasChatModel:
    async def test_has_chat_false_when_none_bound(self, db: AsyncSession) -> None:
        assert await ps.has_chat_model(db) is False

    async def test_has_chat_true_after_binding(
        self, db: AsyncSession, chat_model: Model
    ) -> None:
        await ps.set_binding(db, purpose="chat", model_pk=chat_model.id)
        assert await ps.has_chat_model(db) is True


class TestVisionVerify:
    async def test_verify_vision_unknown_model_returns_404(
        self, db: AsyncSession
    ) -> None:
        from app.core.exceptions import NotFoundError
        from app.infra.llm.openai_compat import get_llm

        with pytest.raises(NotFoundError):
            await ps.verify_vision(db, get_llm(), "mdl_nonexistent")
