"""
模型个体管理与人格/偏好编辑。

## 为什么模型要能单独增删

原来添加供应商时必须一次性填好所有模型，之后要加一个模型只能
把整个供应商删掉重建 —— 而删供应商会级联删掉它下面所有模型的
功能位绑定，用户等于要把配置重做一遍。

供应商仍然存在，但它的角色降为【分组】：模型属于哪个端点、用哪个
API Key。增删的单位是模型。

## 为什么人格要能在界面里编辑

SOUL.md（性格）和 USER.md（自述）直接进系统提示词，是影响输出最
明显的两个文件。但它们在 personas/ 目录下，用户要用编辑器打开、
还得知道有这两个文件存在。

prompts.py 的 _read 没有缓存，所以存盘即生效，不用重启。
"""

from __future__ import annotations

import structlog
from app.api.schemas import (
    ModelCreate,
    ModelOut,
    ModelPatch,
    PersonaFile,
    PersonaUpdate,
)
from app.core.config import PROJECT_ROOT
from app.core.exceptions import BadRequestError, ConflictError, NotFoundError
from app.core.ids import model_id as new_model_id
from app.infra.db.session import get_db
from app.modules.provider.models import Model, ModelBinding, Provider
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)
router = APIRouter(tags=["模型与人格"])


def _out(m: Model, provider_name: str = "") -> ModelOut:
    return ModelOut(
        id=m.id,
        provider_id=m.provider_id,
        provider_name=provider_name,
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
    往已有供应商下加一个模型。

    不需要重建供应商 —— 那会级联删掉所有功能位绑定，
    用户要把配置重做一遍。
    """
    p = (
        await db.execute(select(Provider).where(Provider.id == body.provider_id))
    ).scalars().first()
    if p is None:
        raise NotFoundError("供应商不存在", code="provider_not_found")

    mid = body.model_id.strip()
    if not mid:
        raise BadRequestError("模型 ID 不能为空", code="empty_model_id")

    dup = (
        await db.execute(
            select(Model).where(
                Model.provider_id == body.provider_id, Model.model_id == mid
            )
        )
    ).scalars().first()
    if dup is not None:
        raise ConflictError(f"这个供应商下已经有 {mid} 了", code="model_exists")

    m = Model(
        id=new_model_id(),
        provider_id=body.provider_id,
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
        m.enabled = 1 if data["enabled"] else 0
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
        await db.execute(select(Provider).where(Provider.id == m.provider_id))
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
        names = {"chat": "对话", "vision": "看图", "title": "标题", "compact": "压缩"}
        used = "、".join(names.get(b.purpose, b.purpose) for b in bound)
        raise ConflictError(
            f"这个模型正被【{used}】使用，先在功能位绑定里换成别的",
            code="model_in_use",
        )

    await db.delete(m)
    await db.commit()
    log.info("model_deleted", model_id=m.model_id)
    return {"ok": True}


# ── 人格与个人偏好 ──

# 可编辑的文件白名单。
#
# 【必须是白名单】而不是拼路径。用参数拼的话传
# ../../.env 就能读写任意文件 —— 这个坑在技能加载上踩过。
_PERSONA_FILES = {
    "soul": (
        "SOUL.md",
        "性格设定",
        "决定它怎么说话：语气、详略、要不要反驳你。直接进系统提示词。",
    ),
    "user": (
        "USER.md",
        "关于你",
        "你的技术栈、习惯、偏好。它据此调整默认假设，少问几个来回。",
    ),
    "behavior": (
        "AGENTS.md",
        "行为规则",
        "工作流程与硬性约束。改坏了会明显影响可用性，建议先备份。",
    ),
}
_PERSONA_DIR = PROJECT_ROOT / "personas"
_MAX_PERSONA_BYTES = 64 * 1024


@router.get("/personas", response_model=dict, summary="人格与偏好")
async def list_personas() -> dict[str, object]:
    items = []
    for key, (fname, label, hint) in _PERSONA_FILES.items():
        f = _PERSONA_DIR / fname
        try:
            content = f.read_text(encoding="utf-8") if f.is_file() else ""
        except (OSError, UnicodeDecodeError):
            content = ""
        items.append(
            PersonaFile(
                key=key,
                filename=fname,
                label=label,
                hint=hint,
                content=content,
                exists=f.is_file(),
            )
        )
    return {"items": items}


@router.put("/personas/{key}", response_model=PersonaFile, summary="保存人格文件")
async def save_persona(key: str, body: PersonaUpdate) -> PersonaFile:
    """
    存盘即生效，不用重启 —— prompts.py 的 _read 没有缓存。

    这是有意的：加了缓存的话改完要重启才生效，
    而用户会以为"改了没用"。
    """
    if key not in _PERSONA_FILES:
        raise NotFoundError(f"未知的人格文件：{key}", code="unknown_persona")

    raw = body.content
    if len(raw.encode("utf-8")) > _MAX_PERSONA_BYTES:
        raise BadRequestError(
            f"内容超过 {_MAX_PERSONA_BYTES // 1024} KB。"
            "人格文件每轮对话都会进提示词，太长会挤掉上下文预算",
            code="persona_too_long",
        )

    fname, label, hint = _PERSONA_FILES[key]
    f = _PERSONA_DIR / fname
    _PERSONA_DIR.mkdir(parents=True, exist_ok=True)
    # newline="\n" 统一行尾。不统一的话 Windows 上写出 CRLF，
    # 而 git 里存的是 LF，每次保存都产生一个全文件 diff。
    f.write_text(raw, encoding="utf-8", newline="\n")
    log.info("persona_saved", key=key, bytes=len(raw.encode("utf-8")))
    return PersonaFile(
        key=key,
        filename=fname,
        label=label,
        hint=hint,
        content=raw,
        exists=True,
    )


@router.post("/personas/{key}/reset", response_model=PersonaFile, summary="恢复示例内容")
async def reset_persona(key: str) -> PersonaFile:
    """
    从 .example.md 恢复。

    改坏了要有退路 —— 尤其 AGENTS.md，写错会明显影响可用性，
    而用户不一定记得原来是什么。
    """
    if key not in _PERSONA_FILES:
        raise NotFoundError(f"未知的人格文件：{key}", code="unknown_persona")
    fname, label, hint = _PERSONA_FILES[key]
    example = _PERSONA_DIR / fname.replace(".md", ".example.md")
    if not example.is_file():
        raise NotFoundError(
            f"没有 {example.name}，无法恢复", code="no_example"
        )
    content = example.read_text(encoding="utf-8")
    (_PERSONA_DIR / fname).write_text(content, encoding="utf-8", newline="\n")
    log.info("persona_reset", key=key)
    return PersonaFile(
        key=key, filename=fname, label=label, hint=hint, content=content, exists=True
    )
