"""
模型管理与 API 端点探测。

## 为什么模型要能单独增删

原来添加端点时必须一次性填好所有模型，之后要加一个模型只能
把整个端点删掉重建 —— 而删端点会级联删掉它下面所有模型的
功能位绑定，用户等于要把配置重做一遍。

端点角色降为【分组】：模型属于哪个端点、用哪个 API Key。增删的单位是模型。
"""

from __future__ import annotations

import structlog
from app.api.deps import get_registry
from app.api.schemas import (
    ModelCreate,
    ModelOut,
    ModelPatch,
)
from app.core.config import settings
from app.core.crypto import decrypt
from app.core.exceptions import (
    AppError,
    BadRequestError,
    ConflictError,
    NotFoundError,
)
from app.core.ids import model_id as new_model_id
from app.infra.db.session import get_db
from app.infra.llm.openai_compat import get_llm
from app.modules.agent import prompts
from app.modules.agent.tokens import count_text, count_tools
from app.modules.agent.tools.base import ToolRegistry
from app.modules.endpoint import service as ps
from app.modules.endpoint.models import Endpoint, Model, ModelBinding
from app.modules.session.models import Session
from app.modules.skill import registry as skill_registry
from app.modules.skill import state as skill_state
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)
router = APIRouter(tags=["模型与人格"])

# 功能位的中文名。
#
# 错误信息里要说清是哪个功能在用这个模型 —— 只报 chat 的话
# 用户还得自己去对照设置页才知道那是什么。
_PURPOSE_CN = {
    "chat": "对话",
    "vision": "看图",
    "title": "标题",
    "compact": "压缩",
}


def _out(m: Model, endpoint_name: str = "") -> ModelOut:
    return ModelOut(
        id=m.id,
        endpoint_id=m.endpoint_id,
        endpoint_name=endpoint_name,
        model_id=m.model_id,
        display_name=m.display_name or m.model_id,
        context_window=m.context_window,
        window_source=m.window_source,
        supports_vision=m.supports_vision,
        supports_tools=m.supports_tools,
        enabled=bool(m.enabled),
        price_in_per_1m=m.price_in_per_1m,
        price_out_per_1m=m.price_out_per_1m,
    )


@router.post("/models", response_model=ModelOut, status_code=201, summary="添加单个模型")
async def add_model(body: ModelCreate, db: AsyncSession = Depends(get_db)) -> ModelOut:
    """
    往已有端点下加一个模型。

    不需要重建端点 —— 那会级联删掉所有功能位绑定，
    用户要把配置重做一遍。
    """
    p = (
        await db.execute(select(Endpoint).where(Endpoint.id == body.endpoint_id))
    ).scalars().first()
    if p is None:
        raise NotFoundError("端点不存在", code="endpoint_not_found")

    mid = body.model_id.strip()
    if not mid:
        raise BadRequestError("模型 ID 不能为空", code="empty_model_id")

    dup = (
        await db.execute(
            select(Model).where(
                Model.endpoint_id == body.endpoint_id, Model.model_id == mid
            )
        )
    ).scalars().first()
    if dup is not None:
        raise ConflictError(f"该端点下已有 {mid} 了", code="model_exists")

    m = Model(
        id=new_model_id(),
        endpoint_id=body.endpoint_id,
        model_id=mid,
        display_name=body.display_name or "",
        context_window=body.context_window,
        window_source="manual",
        enabled=1,
    )
    db.add(m)
    await db.commit()
    log.info("model_added", model_id=mid, provider=p.name)
    return _out(m, p.name)


@router.patch("/models/{model_pk}", response_model=ModelOut, summary="改模型")
async def patch_model(
    model_pk: str, body: ModelPatch, db: AsyncSession = Depends(get_db)
) -> ModelOut:
    """
    改模型属性，包括启用/禁用。

    ## 禁用不等于删除

    禁用只是把它从对话页的快捷切换菜单里去掉 —— 已有的功能位绑定
    和历史消息都不动。用途是"这个模型我暂时不用，但别让我重新配"。

    删除才是真的移除。
    """
    m = (await db.execute(select(Model).where(Model.id == model_pk))).scalars().first()
    if m is None:
        raise NotFoundError("模型不存在", code="model_not_found")

    data = body.model_dump(exclude_unset=True)
    if "enabled" in data and data["enabled"] is not None:
        want = bool(data["enabled"])
        if not want:
            # 【禁用被功能位绑定的模型要拒绝】。
            #
            # 删除有这个检查，禁用却没有 —— 而后果是一样的：那个功能位
            # 指向一个禁用的模型，下次对话报错或静默降级，而用户只是
            # "把一个看起来没在用的模型关掉了"，完全联系不起来。
            #
            # 尤其是对话位：禁用它等于让整个应用不能对话，
            # 而报错信息不会提"你刚才禁用了它"。
            bound = list(
                (
                    await db.execute(
                        select(ModelBinding).where(ModelBinding.model_pk == model_pk)
                    )
                ).scalars()
            )
            if bound:
                used = "、".join(_PURPOSE_CN.get(b.purpose, b.purpose) for b in bound)
                raise ConflictError(
                    f"这个模型正被【{used}】使用，禁用会让那些功能不可用。"
                    "先在下面的功能位绑定里换成别的模型。",
                    code="model_in_use",
                )
        m.enabled = 1 if want else 0
    if "display_name" in data and data["display_name"] is not None:
        m.display_name = data["display_name"]
    if "context_window" in data and data["context_window"] is not None:
        if data["context_window"] < 1024:
            raise BadRequestError("上下文窗口太小", code="window_too_small")
        m.context_window = data["context_window"]
        m.window_source = "manual"
    if "price_in_per_1m" in data:
        m.price_in_per_1m = data["price_in_per_1m"]
    if "price_out_per_1m" in data:
        m.price_out_per_1m = data["price_out_per_1m"]

    await db.commit()
    p = (
        await db.execute(select(Endpoint).where(Endpoint.id == m.endpoint_id))
    ).scalars().first()
    return _out(m, p.name if p else "")


@router.delete("/models/{model_pk}", summary="删除单个模型")
async def delete_model(
    model_pk: str, db: AsyncSession = Depends(get_db)
) -> dict[str, bool]:
    """
    删一个模型。

    ## 为什么要先检查绑定

    模型被功能位绑定着（比如"对话"用它）时直接删掉，那个功能位就
    悬空了 —— 下次对话报"未配置模型"，而用户刚才只是删了一个
    看起来没在用的模型，完全联系不起来。

    所以拒绝，并在错误信息里说清是哪些功能位。
    """
    m = (await db.execute(select(Model).where(Model.id == model_pk))).scalars().first()
    if m is None:
        raise NotFoundError("模型不存在", code="model_not_found")

    bound = list(
        (
            await db.execute(
                select(ModelBinding).where(ModelBinding.model_pk == model_pk)
            )
        ).scalars()
    )
    if bound:
        used = "、".join(_PURPOSE_CN.get(b.purpose, b.purpose) for b in bound)
        raise ConflictError(
            f"这个模型正被【{used}】使用，先在功能位绑定里换成别的",
            code="model_in_use",
        )

    await db.delete(m)
    await db.commit()
    log.info("model_deleted", model_id=m.model_id)
    return {"ok": True}


@router.get(
    "/providers/{provider_id}/available-models",
    response_model=dict,
    summary="拉取端点可用的模型列表",
)
async def available_models(
    provider_id: str, db: AsyncSession = Depends(get_db)
) -> dict[str, object]:
    """
    用已存的 base_url + Key 去拉模型列表。

    ## 为什么需要这个而不是复用 /providers/probe

    那个要求请求体里带 base_url 和 api_key。而这里的场景是"往【已有】
    端点下加模型"—— 端点和 Key 都已经存过了，让用户再填一遍
    是荒谬的（而且 Key 存的是密文，前端根本拿不到明文）。

    ## 返回值里标出已添加的

    前端要能把已加过的置灰或打勾。不标的话用户点了才知道重复，
    而重复添加会撞 (provider_id, model_id) 唯一索引。
    """
    p_ = (
        await db.execute(select(Endpoint).where(Endpoint.id == provider_id))
        ).scalars().first()
    if p_ is None:
        raise NotFoundError("端点不存在", code="endpoint_not_found")

    have = {
        m.model_id
        for m in (
            await db.execute(select(Model).where(Model.endpoint_id == provider_id))
        ).scalars()
    }

    try:
        _, probed = await ps.probe_models(
            get_llm(), p_.base_url, decrypt(p_.api_key_cipher)
        )
    except AppError:
        # 拉取失败不该让"加模型"整个不可用 —— 用户仍然可以手填。
        # 所以返回空列表 + 原因，而不是把错误抛给前端。
        raise
    return {
        "items": [
            {
                "model_id": m.model_id,
                "context_window": m.context_window,
                "window_source": m.window_source,
                "looks_non_chat": m.looks_non_chat,
                # 已经加过的，前端置灰
                "already_added": m.model_id in have,
            }
            for m in probed
        ]
    }

@router.get("/context-overhead", summary="固定上下文开销")
async def context_overhead(
    session_id: str = Query("", description="留空则按 chat 功能位的模型算"),
    db: AsyncSession = Depends(get_db),
    registry: ToolRegistry = Depends(get_registry),
) -> dict[str, object]:
    """
    工具定义 + 系统提示词占多少 token。

    ## 为什么要有这个接口

    这两项在【发消息之前】就确定了：工具集和人格文件都是配置，
    不随对话变。而用户想知道"还剩多少空间粘代码"恰恰是在发消息之前。

    只在 run 期间由 context_usage 事件报的话，切一次页面就只剩
    对话内容那一段，看起来像固定开销凭空消失了。

    ## 为什么不放进会话详情

    会话详情的路由拿不到 ToolRegistry（它在 app.state 上，
    要走依赖注入）。而且这个值和会话无关 —— 全局一份，
    换会话不变。放进详情会让每次开会话都白算一遍。

    ## 返回的是估算值

    本地用 tiktoken(cl100k_base)，而各家模型用自己的分词器，
    实测偏高 30% 左右。有真实 usage 时前端按比例校正 ——
    所以这里必须标明 is_estimate。
    """
    specs = registry.to_specs()
    tool_names = [
        str((s.get("function") or {}).get("name", "")) for s in specs
    ]
    # 【必须带 skills】。
    #
    # 不带的话这里算出的系统提示词比真实的小 —— 技能清单（每个技能一行
    # 名字+描述）是常驻上下文的一部分，漏掉它会让上下文条少报几百 token。
    #
    # 而且开关技能时这个数字不会变，用户点了开关看不到任何反应，
    # 会以为开关没生效。
    system = prompts.build_system_prompt(
        workspace=str(settings.workspace_dir),
        tool_names=tool_names,
        skills=await skill_state.filter_l1(db, skill_registry.get_index().l1()),
    )

    win = 0
    try:
        sess_model_pk = ""
        if session_id:
            row = (
                await db.execute(select(Session).where(Session.id == session_id))
            ).scalars().first()
            if row is not None:
                sess_model_pk = row.model_pk or ""
        m = await ps.resolve(db, purpose="chat", override_pk=sess_model_pk)
        win = m.context_window
    except AppError:
        # 没配模型时窗口未知。返回 0，前端回落到默认值 ——
        # 那时连对话都发不出去，比例准不准无关紧要。
        win = 0

    return {
        "tools_tokens": count_tools(specs) if specs else 0,
        "system_tokens": count_text(system),
        "tool_count": len(specs),
        "window_tokens": win,
        # 【必须标出来】。这是本地分词器的数，不是模型给的。
        "is_estimate": True,
    }
