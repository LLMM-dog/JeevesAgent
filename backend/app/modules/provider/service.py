"""
供应商与模型服务。

核心是 probe_models —— 用户点名要的"不用手动输入模型名"。
"""

from dataclasses import dataclass
from typing import Any

import structlog
from app.core.crypto import decrypt, encrypt, key_hint
from app.core.events import Ev, emit
from app.core.exceptions import NoModelBoundError, NotFoundError
from app.core.ids import binding_id, model_id, provider_id
from app.core.time import now_ms
from app.infra.llm.openai_compat import normalize_base_url
from app.infra.llm.port import LLMPort, ResolvedModel
from app.modules.provider.models import Model, ModelBinding, Provider
from app.modules.provider.windows import looks_non_chat, lookup_window
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

PURPOSES = ("chat", "vision", "title", "compact", "embedding")


@dataclass
class ProbedModel:
    model_id: str
    context_window: int
    window_source: str
    looks_non_chat: bool


async def probe_models(
    llm: LLMPort, base_url: str, api_key: str
) -> tuple[str, list[ProbedModel]]:
    """
    探测模型列表。纯查询，不落库。

    返回 (规范化后的 base_url, 模型列表)。规范化后的 URL 要回显给用户 ——
    他填的可能被改过（补了 /v1），让他看到实际会用哪个地址。
    """
    normalized = normalize_base_url(base_url)
    names = await llm.list_models(base_url, api_key)

    out: list[ProbedModel] = []
    for name in names:
        window, source = lookup_window(name)
        out.append(
            ProbedModel(
                model_id=name,
                context_window=window,
                window_source=source,
                looks_non_chat=looks_non_chat(name),
            )
        )
    # 对话模型排前面，同类按名字排
    out.sort(key=lambda m: (m.looks_non_chat, m.model_id))
    log.info("probe_ok", base_url=normalized, count=len(out))
    return normalized, out


async def create_provider(
    db: AsyncSession,
    *,
    name: str,
    base_url: str,
    api_key: str,
    models: list[dict[str, Any]],
) -> Provider:
    """
    建 provider + 它的 model。同名或同端点则【并入已有分组】。

    ## 为什么不再报"名称已存在"

    原来同名直接 409。但用户的实际操作路径是：先加一个端点，之后想
    再加几个模型，于是又走一次「添加供应商」——得到"无法添加"。
    而他并不想新建供应商，只是想往这个端点下加模型。

    现在的行为：

      - 同名，或者同 base_url + 同 Key 尾号 → 并入那个分组
      - 并入时更新 Key（用户可能就是来换 Key 的）
      - 模型按 model_id 去重，已有的静默跳过

    判据里带 Key 尾号是有意的：同一个端点用两个不同的 Key
    （比如个人额度和团队额度）是合理场景，那种情况该分成两组。
    """
    norm_url = normalize_base_url(base_url)
    hint = key_hint(api_key)

    # 先按名字找，再按"同端点同 Key"找
    p = (
        await db.execute(select(Provider).where(Provider.name == name))
    ).scalar_one_or_none()
    if p is None:
        p = (
            await db.execute(
                select(Provider).where(
                    Provider.base_url == norm_url, Provider.key_hint == hint
                )
            )
        ).scalars().first()

    if p is None:
        p = Provider(
            id=provider_id(),
            name=name,
            base_url=norm_url,
            api_key_cipher=encrypt(api_key),
            key_hint=hint,
            last_probe_at=now_ms(),
        )
        db.add(p)
    else:
        # 并入：更新端点和 Key。用户重复走这个流程通常是因为
        # 换了 Key 或者端点变了，沿用旧的会让他以为改了但没生效。
        p.base_url = norm_url
        p.api_key_cipher = encrypt(api_key)
        p.key_hint = hint
        p.last_probe_at = now_ms()

    # 【必须显式 flush】。本项目不声明 relationship()（async 下惰性加载
    # 会抛 MissingGreenlet，是个更大的坑），而没有 relationship 时
    # SQLAlchemy 的 unit of work 不保证父行先插 —— 实测它会先 INSERT model
    # 再 INSERT provider，在 foreign_keys=ON 下直接 IntegrityError。
    # 报错指向 model 表，完全不提示"顺序不对"。
    await db.flush()

    # 已有的模型 id，用来去重。
    #
    # 不去重的话 (provider_id, model_id) 上的唯一索引会抛
    # IntegrityError，而那个报错指向数据库约束，
    # 完全不提示"这个模型你已经加过了"。
    have = {
        m.model_id
        for m in (
            await db.execute(select(Model).where(Model.provider_id == p.id))
        ).scalars()
    }

    for m in models:
        mid = str(m.get("model_id", "")).strip()
        if not mid or mid in have:
            continue
        have.add(mid)
        window = m.get("context_window")
        if window:
            source = "manual"
        else:
            window, source = lookup_window(mid)
        db.add(
            Model(
                id=model_id(),
                provider_id=p.id,
                model_id=mid,
                display_name=str(m.get("display_name", "") or ""),
                context_window=int(window),
                window_source=source,
            )
        )

    await db.commit()
    return p


async def list_providers(db: AsyncSession) -> list[tuple[Provider, int]]:
    providers = list((await db.execute(select(Provider))).scalars())
    out: list[tuple[Provider, int]] = []
    for p in providers:
        models = list(
            (await db.execute(select(Model).where(Model.provider_id == p.id))).scalars()
        )
        out.append((p, len(models)))
    return out


async def get_provider(db: AsyncSession, pid: str) -> Provider:
    p = (await db.execute(select(Provider).where(Provider.id == pid))).scalar_one_or_none()
    if p is None:
        raise NotFoundError("供应商不存在", code="provider_not_found")
    return p


async def delete_provider(db: AsyncSession, pid: str) -> None:
    await get_provider(db, pid)
    await db.execute(delete(Provider).where(Provider.id == pid))
    await db.commit()


async def list_models(db: AsyncSession, provider_id_: str | None = None) -> list[Model]:
    stmt = select(Model)
    if provider_id_:
        stmt = stmt.where(Model.provider_id == provider_id_)
    return list((await db.execute(stmt)).scalars())


async def get_model(db: AsyncSession, pk: str) -> Model:
    m = (await db.execute(select(Model).where(Model.id == pk))).scalar_one_or_none()
    if m is None:
        raise NotFoundError("模型不存在", code="model_not_found")
    return m


async def set_binding(
    db: AsyncSession, *, purpose: str, model_pk: str, agent_name: str = ""
) -> ModelBinding:
    if purpose not in PURPOSES:
        raise NotFoundError(f"未知的功能位：{purpose}", code="bad_purpose")
    await get_model(db, model_pk)

    existing = (
        await db.execute(
            select(ModelBinding).where(
                ModelBinding.agent_name == agent_name, ModelBinding.purpose == purpose
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.model_pk = model_pk
        await db.commit()
        return existing

    b = ModelBinding(
        id=binding_id(), agent_name=agent_name, purpose=purpose, model_pk=model_pk
    )
    db.add(b)
    await db.commit()
    return b


async def list_bindings(db: AsyncSession) -> list[tuple[ModelBinding, Model, Provider]]:
    out: list[tuple[ModelBinding, Model, Provider]] = []
    for b in (await db.execute(select(ModelBinding))).scalars():
        try:
            m = await get_model(db, b.model_pk)
            p = await get_provider(db, m.provider_id)
        except NotFoundError:
            continue
        out.append((b, m, p))
    return out


async def resolve(
    db: AsyncSession,
    *,
    purpose: str = "chat",
    agent_name: str = "",
    override_pk: str = "",
) -> ResolvedModel:
    """
    解析顺序：
      0. override_pk（会话自己选的模型）—— 只对 purpose="chat" 生效
      1. (agent_name, purpose) 精确匹配
      2. ("", purpose) 全局默认
      3. ("", "chat") 兜底
      4. 都没有 → NoModelBoundError

    降级时发 model_fallback 事件 —— 降级必须可见。静默降级会让用户
    以为在用配好的模型，实际用的是另一个（可能贵 10 倍或弱很多）。

    ## override_pk 为什么只管 chat

    用户在对话页切的是"这轮对话用哪个模型"。标题生成和上下文压缩
    是后台动作，用便宜模型是有意的配置 —— 跟着切会让每次压缩都
    烧贵模型的 token，而用户完全看不到这件事发生。

    ## 为什么找不到时静默回落

    模型可能被删了（session.model_pk 没有外键，允许悬空）。
    这时报错会让整个会话打不开，而回落到默认绑定至少能继续用。
    """
    if override_pk and purpose == "chat":
        m = (
            await db.execute(select(Model).where(Model.id == override_pk))
        ).scalars().first()
        # 禁用的也放行：用户可能先在对话里选了它，之后才在设置页禁用。
        # 这时打断正在进行的对话比让它继续用更糟。
        if m is not None:
            p_ = await get_provider(db, m.provider_id)
            return ResolvedModel(
                model_id=m.model_id,
                base_url=p_.base_url,
                api_key=decrypt(p_.api_key_cipher),
                context_window=m.context_window,
                supports_vision=m.supports_vision == "true",
                provider_name=p_.name,
                purpose=purpose,
                price_in_per_1m=m.price_in_per_1m,
                price_out_per_1m=m.price_out_per_1m,
            )

    attempts: list[tuple[str, str]] = []
    if agent_name:
        attempts.append((agent_name, purpose))
    attempts.append(("", purpose))
    if purpose != "chat":
        attempts.append(("", "chat"))

    chosen: ModelBinding | None = None
    used_idx = -1
    for i, (an, pu) in enumerate(attempts):
        b = (
            await db.execute(
                select(ModelBinding).where(
                    ModelBinding.agent_name == an, ModelBinding.purpose == pu
                )
            )
        ).scalar_one_or_none()
        if b is not None:
            chosen = b
            used_idx = i
            break

    if chosen is None:
        raise NoModelBoundError(purpose)

    m = await get_model(db, chosen.model_pk)
    p = await get_provider(db, m.provider_id)

    if used_idx > 0:
        requested, used = attempts[0], attempts[used_idx]
        await emit(
            Ev.MODEL_FALLBACK,
            purpose=purpose,
            requested=f"{requested[0] or '*'}/{requested[1]}",
            used=f"{used[0] or '*'}/{used[1]} → {m.model_id}",
            reason="该功能位未绑定模型，已回落",
        )

    return ResolvedModel(
        model_id=m.model_id,
        base_url=p.base_url,
        api_key=decrypt(p.api_key_cipher),
        context_window=m.context_window,
        supports_vision=m.supports_vision == "true",
        provider_name=p.name,
        purpose=purpose,
        # 带上单价，让 span 能存快照。
        # NULL 表示未配价，不是免费 —— compute_cost 会返回 0，
        # 但报表能靠 price 字段是否为 NULL 区分出"零成本"和"没配价"。
        price_in_per_1m=m.price_in_per_1m,
        price_out_per_1m=m.price_out_per_1m,
    )


async def verify_vision(db: AsyncSession, llm: LLMPort, model_pk: str) -> Model:
    """
    核验模型的图片输入能力，结果落库。

    ## 为什么是显式操作而不是自动跑

    核验要发一次真实的多模态请求 —— 花钱、要几秒、可能失败。用户加了
    10 个模型，只有他打算用图片的那个需要核验。

    所以 `supports_vision` 默认是 `unknown`，用户在设置页点"核验"才跑。

    ## 失败也要写库

    核验失败时把结果写成 `false` 并记时间。不写的话用户每次打开设置页
    都看到"未核验"，会反复点 —— 每次都花一次请求的钱。
    """
    from app.modules.provider import vision

    m = await get_model(db, model_pk)
    p = await get_provider(db, m.provider_id)

    ok, detail = await vision.probe_vision(
        llm, p.base_url, decrypt(p.api_key_cipher), m.model_id
    )
    m.supports_vision = "true" if ok else "false"
    m.vision_checked_at = now_ms()
    await db.commit()
    log.info(
        "vision_verified", model=m.model_id, supports=m.supports_vision, detail=detail[:120]
    )
    # detail 挂在对象上给路由层读。不入库 ——
    # 它是一次性的诊断信息，下次核验会产生新的。
    m._vision_detail = detail  # type: ignore[attr-defined]
    return m


async def has_chat_model(db: AsyncSession) -> bool:
    try:
        await resolve(db, purpose="chat")
        return True
    except (NoModelBoundError, NotFoundError):
        return False
