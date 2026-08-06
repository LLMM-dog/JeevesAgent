"""
技能开关的读写。

## 为什么过滤要放在"进提示词"这一步

技能的三级披露里，只有 L1（名字+描述清单）是常驻上下文的。关掉一个技能
的意义就是"别把它的名字和描述发给模型"—— 所以过滤点在 l1() 的调用处。

不在 load_index 里过滤：那个索引同时供设置页的列表用，
过滤掉的话用户在界面上也看不到被关掉的技能，就没法再打开它了。
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id
from app.modules.skill.models import SkillState

log = structlog.get_logger(__name__)


async def disabled_names(db: AsyncSession) -> set[str]:
    """
    被关掉的技能名。

    表里没有记录 = 启用，所以这里只查 enabled=0 的行。
    """
    rows = (
        await db.execute(select(SkillState).where(SkillState.enabled == 0))
    ).scalars()
    return {r.name for r in rows}


async def set_enabled(db: AsyncSession, name: str, enabled: bool) -> None:
    """
    开关一个技能。

    ## 为什么 upsert 而不是只 insert

    用户可能反复开关同一个技能。每次都 insert 会撞唯一约束，
    而那个错误（IntegrityError）完全不指向"你已经设置过这个技能了"。
    """
    row = (
        await db.execute(select(SkillState).where(SkillState.name == name))
    ).scalars().first()
    if row is None:
        db.add(SkillState(id=new_id("skl"), name=name, enabled=1 if enabled else 0))
    else:
        row.enabled = 1 if enabled else 0
    await db.commit()
    log.info("skill_toggled", name=name, enabled=enabled)


async def filter_l1(
    db: AsyncSession, pairs: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """
    从 L1 清单里去掉被关掉的技能。

    ## 为什么容错

    查库失败时【返回完整清单】而不是空清单。开关是个便利功能，
    而技能清单缺失会让模型完全不知道有哪些技能可用 ——
    宁可多发几百 token，也不要让功能静默消失。
    """
    try:
        off = await disabled_names(db)
    except Exception as e:  # noqa: BLE001
        log.warning("skill_filter_failed", err=str(e))
        return pairs
    if not off:
        return pairs
    return [(n, d) for (n, d) in pairs if n not in off]
