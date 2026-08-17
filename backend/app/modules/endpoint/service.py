"""
端点与模型服务。

核心是 probe_models —— 用户点名要的"不用手动输入模型名"。
"""

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import structlog
from app.core.crypto import decrypt, encrypt, key_hint
from app.core.events import Ev, emit
from app.core.exceptions import NoModelBoundError, NotFoundError
from app.core.ids import binding_id, endpoint_id, model_id
from app.core.time import now_ms
from app.infra.llm.openai_compat import normalize_base_url
from app.infra.llm.port import LLMPort, ResolvedModel
from app.modules.endpoint.models import Endpoint, Model, ModelBinding
from app.modules.endpoint.windows import detect_model_type, detect_vision_support, looks_non_chat, lookup_window
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

PURPOSES = ("chat", "vision", "title", "compact", "embedding", "memory", "memory_rerank")

# 功能位之间的中间回落。
#
# 默认链是 (agent, purpose) → ("", purpose) → ("", "chat")，中间不经过别的功能位。
# memory 是例外：记忆提取和上下文压缩是同一类活儿（后台批处理、要便宜模型、
# 输出结构化），已经配了 compact 的用户不该被要求再配一次 memory。
#
# 【不能反向】—— compact 不回落到 memory。压缩是每次对话都可能触发的高频动作，
# 让它去用一个为提取选的模型会有意想不到的开销。
PURPOSE_FALLBACKS: dict[str, tuple[str, ...]] = {"memory": ("compact",)}

# 禁止回落到 chat 的 purpose。
#
# embedding 和 memory_rerank 必须显式配置，不能回落到 chat 模型：
# - chat 模型通常没有 /embeddings 端点，回落会导致运行时 404
# - rerank 模型的 API 协议与 chat 完全不同
# 没配置时应该抛 NoModelBoundError，由调用方决定降级策略（如跳过向量搜索）。
NO_CHAT_FALLBACK = {"embedding", "memory_rerank"}


@dataclass
class ProbedModel:
    model_id: str
    context_window: int
    window_source: str
    looks_non_chat: bool
    model_type: str


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
                model_type=detect_model_type(name),
            )
        )
    # 对话模型排前面，同类按名字排
    out.sort(key=lambda m: (m.looks_non_chat, m.model_id))
    log.info("probe_ok", base_url=normalized, count=len(out))
    return normalized, out


# 已知主机 → 分组名。探测/添加时用户不填分组名，从地址推断。
#
# 匹配用子串（`"deepseek" in host`）而不是精确相等 —— 中转站常带子域名
# （api.deepseek.com、deepseek.r4ai.cn），精确匹配会漏掉。
_KNOWN_HOSTS = (
    ("openai", "OpenAI"),
    ("anthropic", "Anthropic"),
    ("deepseek", "DeepSeek"),
    ("siliconflow", "SiliconFlow"),
    ("moonshot", "Moonshot"),
    ("minimax", "MiniMax"),
    ("bigmodel", "智谱"),
    ("zhipu", "智谱"),
    ("dashscope", "通义千问"),
    ("aliyuncs", "通义千问"),
    ("mistral", "Mistral"),
    ("generativelanguage", "Gemini"),
    ("groq", "Groq"),
    ("x.ai", "xAI"),
    ("together", "Together"),
    ("openrouter", "OpenRouter"),
    ("fireworks", "Fireworks"),
    ("ollama", "Ollama"),
    ("localhost", "本地"),
    ("127.0.0.1", "本地"),
)

# 主机名里这些段不参与命名推断，直接跳过。
_SKIP_SEGMENTS = frozenset(
    {"api", "openapi", "gateway", "v1", "v2", "www", "com", "cn", "net", "org", "io", "ai", "co"}
)


def guess_endpoint_name(base_url: str) -> str:
    """
    从 base_url 推断分组名，用于"添加模型"时自动分组。

    规则：已知主机直接映射；未知主机取第一个非通用段的单词首字母大写；
    解析不出来回落到"自定义"。
    """
    try:
        host = (urlparse(base_url).hostname or "").lower()
    except ValueError:
        host = ""
    if not host:
        return "自定义"
    for hint, name in _KNOWN_HOSTS:
        if hint in host:
            return name
    for seg in host.split("."):
        if seg and seg not in _SKIP_SEGMENTS:
            return seg.capitalize()
    return "自定义"


async def create_endpoint(
    db: AsyncSession,
    *,
    name: str,
    base_url: str,
    api_key: str,
    models: list[dict[str, Any]],
) -> Endpoint:
    """
    建端点 + 它的 model。同名或同端点则【并入已有分组】。

    ## 为什么不再报"名称已存在"

    原来同名直接 409。但用户的实际操作路径是：先加一个端点，之后想
    再加几个模型，于是又走一次「添加端点」——得到"无法添加"。
    而他并不想新建端点，只是想往这个端点下加模型。

    现在的行为：

      - 同名，或者同 base_url + 同 Key 尾号 → 并入那个分组
      - 并入时更新 Key（用户可能就是来换 Key 的）
      - 模型按 model_id 去重，已有的静默跳过

    判据里带 Key 尾号是有意的：同一个端点用两个不同的 Key
    （比如个人额度和团队额度）是合理场景，那种情况该分成两组。
    """
    norm_url = normalize_base_url(base_url)
    hint = key_hint(api_key)

    # 用户不填分组名时从地址推断（"添加模型"是纯自动分组，见路由层）。
    name = name.strip() or guess_endpoint_name(norm_url)

    # 先按名字找，再按"同端点同 Key"找
    p = (
        await db.execute(select(Endpoint).where(Endpoint.name == name))
    ).scalar_one_or_none()
    if p is None:
        p = (
            await db.execute(
                select(Endpoint).where(
                    Endpoint.base_url == norm_url, Endpoint.key_hint == hint
                )
            )
        ).scalars().first()

    if p is None:
        p = Endpoint(
            id=endpoint_id(),
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
    # 再 INSERT endpoint，在 foreign_keys=ON 下直接 IntegrityError。
    # 报错指向 model 表，完全不提示"顺序不对"。
    await db.flush()

    # 已有的模型 id，用来去重。
    #
    # 不去重的话 (endpoint_id, model_id) 上的唯一索引会抛
    # IntegrityError，而那个报错指向数据库约束，
    # 完全不提示"这个模型你已经加过了"。
    have = {
        m.model_id
        for m in (
            await db.execute(select(Model).where(Model.endpoint_id == p.id))
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

        # 启发式检测视觉能力（默认 unknown）
        vision_hint = str(m.get("supports_vision", "")) or detect_vision_support(mid)

        # 工具能力默认 unknown（暂不启发式检测）
        tools_hint = str(m.get("supports_tools", "")) or "unknown"

        db.add(
            Model(
                id=model_id(),
                endpoint_id=p.id,
                model_id=mid,
                display_name=str(m.get("display_name", "") or ""),
                context_window=int(window),
                window_source=source,
                model_type=str(m.get("model_type") or detect_model_type(mid)),
                supports_vision=vision_hint,
                supports_tools=tools_hint,
            )
        )

    await db.commit()
    return p


async def list_endpoints(db: AsyncSession) -> list[tuple[Endpoint, int]]:
    endpoints = list((await db.execute(select(Endpoint))).scalars())
    out: list[tuple[Endpoint, int]] = []
    for p in endpoints:
        models = list(
            (await db.execute(select(Model).where(Model.endpoint_id == p.id))).scalars()
        )
        out.append((p, len(models)))
    return out


async def get_endpoint(db: AsyncSession, pid: str) -> Endpoint:
    p = (await db.execute(select(Endpoint).where(Endpoint.id == pid))).scalar_one_or_none()
    if p is None:
        raise NotFoundError("端点不存在", code="endpoint_not_found")
    return p


async def delete_endpoint(db: AsyncSession, pid: str) -> None:
    await get_endpoint(db, pid)
    await db.execute(delete(Endpoint).where(Endpoint.id == pid))
    await db.commit()


async def update_endpoint(
    db: AsyncSession,
    pid: str,
    *,
    name: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> Endpoint:
    """
    改分组的名字 / 地址 / Key。

    ## 为什么 Key 传空串等于不改

    前端永远拿不到明文 Key（只回显尾 4 位），编辑分组时 Key 输入框是空的。
    空串表示"保持原 Key"，只有用户真的重新填了才更新。否则每次改个名字
    都会把 Key 清空，端点立刻失效。
    """
    p = await get_endpoint(db, pid)
    if name is not None and name.strip():
        p.name = name.strip()
    if base_url is not None and base_url.strip():
        p.base_url = normalize_base_url(base_url)
    if api_key is not None and api_key.strip():
        p.api_key_cipher = encrypt(api_key)
        p.key_hint = key_hint(api_key)
        p.last_probe_at = now_ms()
    await db.commit()
    return p


async def list_models(db: AsyncSession, endpoint_id_: str | None = None) -> list[Model]:
    stmt = select(Model)
    if endpoint_id_:
        stmt = stmt.where(Model.endpoint_id == endpoint_id_)
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


async def list_bindings(db: AsyncSession) -> list[tuple[ModelBinding, Model, Endpoint]]:
    out: list[tuple[ModelBinding, Model, Endpoint]] = []
    for b in (await db.execute(select(ModelBinding))).scalars():
        try:
            m = await get_model(db, b.model_pk)
            p = await get_endpoint(db, m.endpoint_id)
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
            p_ = await get_endpoint(db, m.endpoint_id)
            return ResolvedModel(
                model_id=m.model_id,
                base_url=p_.base_url,
                api_key=decrypt(p_.api_key_cipher),
                context_window=m.context_window,
                supports_vision=m.supports_vision == "true",
                endpoint_name=p_.name,
                purpose=purpose,
                price_in_per_1m=m.price_in_per_1m,
                price_out_per_1m=m.price_out_per_1m,
            )

    attempts: list[tuple[str, str]] = []
    if agent_name:
        attempts.append((agent_name, purpose))
    attempts.append(("", purpose))
    # 中间回落。memory → compact 让已配 compact 的用户不必再配一次。
    for mid in PURPOSE_FALLBACKS.get(purpose, ()):
        if agent_name:
            attempts.append((agent_name, mid))
        attempts.append(("", mid))
    # 某些 purpose 禁止回落到 chat（如 embedding、memory_rerank）
    if purpose != "chat" and purpose not in NO_CHAT_FALLBACK:
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
    p = await get_endpoint(db, m.endpoint_id)

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
        endpoint_name=p.name,
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
    from app.modules.endpoint import vision

    m = await get_model(db, model_pk)
    p = await get_endpoint(db, m.endpoint_id)

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
