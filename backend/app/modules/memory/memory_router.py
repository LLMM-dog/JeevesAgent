"""
记忆管理 API。

## 为什么需要一键重算接口

嵌入模型是用户可以随时切换的。换了之后维度变化，旧向量全部失效 ——
但【不自动重算】：那可能是几千次 API 调用，用户没同意就烧钱，
而且期间召回质量是混乱的（一半新向量一半旧向量）。

所以设计成：换模型后旧向量立即停止参与召回（search 里按 model 筛掉），
重算由用户显式触发。最坏情况是"召回暂时没有语义结果"，
而不是"扣了一笔意外的费用"。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.session import get_db
from app.modules.memory import service as memory_service
from app.modules.memory.models import MemoryScope
from app.modules.settings import service as settings_service

router = APIRouter(prefix="/api/memory", tags=["记忆"])


class VectorStatus(BaseModel):
    """向量新鲜度。给设置页显示"有 N 条记忆的向量已失效"。"""

    total: int
    never: int
    model: int
    content: int
    fresh: int
    embedding_configured: bool
    embedding_model: str = ""

    @property
    def stale(self) -> int:
        return self.never + self.model + self.content


class RevectorizeResult(BaseModel):
    attempted: int
    succeeded: int
    skipped: int
    model: str
    dim: int
    errors: list[str]


@router.get("/vectors", response_model=VectorStatus, summary="向量新鲜度统计")
async def vector_status(db: AsyncSession = Depends(get_db)) -> VectorStatus:
    """
    三种失效原因分开报告，因为用户的处理方式不同：

    - never   从没算过 → 点重算
    - model   换了嵌入模型 → 点重算（或清空回落关键词）
    - content 记忆改过但向量没跟上 → 点重算
    """
    stats = await memory_service.vector_status(db)
    model = await memory_service.resolve_embedding_model(db)
    return VectorStatus(
        total=stats["total"],
        never=stats["never"],
        model=stats["model"],
        content=stats["content"],
        fresh=stats["fresh"],
        embedding_configured=model is not None,
        embedding_model=model.model_id if model else "",
    )


@router.post("/vectors/rebuild", response_model=RevectorizeResult, summary="一键重算向量")
async def rebuild_vectors(
    db: AsyncSession = Depends(get_db),
    only_stale: bool = Query(
        True,
        description="只算失效的（默认）。false 表示全量重算，用于怀疑向量算错时",
    ),
) -> RevectorizeResult:
    """
    重算向量。

    ## 为什么是同步接口而不是后台任务

    个人项目的记忆量级下（几千条），一次重算是几十秒到几分钟。
    做成后台任务要引入任务表、进度查询、失败重试 —— 那套复杂度
    只有在"用户可能关掉页面"时才值得。

    量级涨到几万条时该改成后台任务，那时这个接口的语义不变，
    只是返回一个 task_id。
    """
    report = await memory_service.revectorize(db, only_stale=only_stale)
    return RevectorizeResult(
        attempted=report.attempted,
        succeeded=report.succeeded,
        skipped=report.skipped,
        model=report.model,
        dim=report.dim,
        errors=report.errors,
    )


@router.delete("/vectors", summary="清空所有向量")
async def clear_vectors(db: AsyncSession = Depends(get_db)) -> dict[str, int]:
    """
    清空向量，让召回干净地回落关键词搜索。

    用在"换了嵌入模型但暂时不想重算"的场景 —— 留着一批永远不参与
    比较的死数据只是占空间。记忆文件本身不受影响。
    """
    return {"cleared": await memory_service.clear_vectors(db)}


@router.get("/settings", summary="可调设置项与当前值")
async def list_settings(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """
    返回元信息 + 当前值，供前端渲染设置页。

    【前端不该硬编码可调项列表】—— 那份列表会和后端不同步。
    类型、范围、说明都从后端来，加一个设置项只改后端。
    """
    await settings_service.reload(db)
    return {"items": settings_service.describe()}


class SettingsUpdate(BaseModel):
    values: dict[str, Any]


@router.put("/settings", summary="修改设置（立即生效）")
async def update_settings(
    payload: SettingsUpdate, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """
    改完立即生效，不需要重启。

    任一项校验失败则整批不写 —— 部分生效会让用户看到混合状态
    （改了 3 项、2 项生效），而他不知道是哪 2 项。
    """
    try:
        applied = await settings_service.set_many(db, payload.values)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"applied": applied, "items": settings_service.describe()}


@router.post("/settings/reset", summary="恢复默认设置")
async def reset_settings(
    db: AsyncSession = Depends(get_db),
    keys: list[str] | None = None,
) -> dict[str, Any]:
    """keys 为空时全部恢复。恢复 = 删行 + 重新应用默认值。"""
    removed = await settings_service.reset(db, keys)
    return {"removed": removed, "items": settings_service.describe()}


@router.get("/search", summary="语义搜索记忆")
async def search(
    q: str = Query(..., min_length=1, description="查询文本"),
    agent_id: str = Query("", description="限定智能体。留空只搜全局记忆"),
    session_id: str = Query("", description="限定会话。需要同时给 agent_id"),
    memory_type: str = Query("", description="限定记忆类型"),
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    搜索范围按【三层隔离】：

        给了 session_id → 全局 + 该智能体 + 该会话
        只给 agent_id   → 全局 + 该智能体（不含任何会话）
        都不给          → 只有全局

    会话级记忆对其他会话不可见 —— 否则 A 会话的临时上下文会污染 B。
    """
    scope = MemoryScope(agent_id=agent_id, session_id=session_id)
    hits = await memory_service.search_semantic(
        db, scope, q, limit=limit, memory_type=memory_type
    )
    return {
        "query": q,
        "hits": [
            {
                "uri": h.uri,
                "memory_type": h.memory_type,
                "title": h.title,
                "score": round(h.score, 4),
                "scope": h.scope,
            }
            for h in hits
        ],
        # 空结果有两种原因，前端要能区分：没配模型 vs 确实没有相关记忆
        "embedding_configured": (await memory_service.resolve_embedding_model(db)) is not None,
    }
