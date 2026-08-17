"""
记忆系统能力检测和降级策略。

四层降级架构：
- Level 4 (Full): LLM + Embedding + Rerank，完整功能
- Level 3 (Standard): LLM + Embedding，向量搜索
- Level 2 (Basic): 仅 LLM，关键词搜索
- Level 1 (None): 无配置，循环压缩
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any

import structlog

from app.core.config import settings

log = structlog.get_logger(__name__)


class MemoryCapabilityLevel(IntEnum):
    """记忆系统能力等级"""

    NONE = 1  # 无配置，循环压缩模式
    BASIC = 2  # 仅 LLM，关键词召回
    STANDARD = 3  # LLM + Embedding，向量召回
    FULL = 4  # LLM + Embedding + Rerank，完整功能


@dataclass
class MemoryCapability:
    """记忆系统当前能力"""

    level: MemoryCapabilityLevel
    has_llm: bool
    has_embedding: bool
    has_rerank: bool

    # 功能开关
    can_extract: bool  # 能否提取记忆
    can_vector_search: bool  # 能否向量搜索
    can_keyword_search: bool  # 能否关键词搜索
    can_rerank: bool  # 能否 Rerank
    can_recursive: bool  # 能否递归搜索
    can_hybrid: bool  # 能否混合搜索
    can_hotness: bool  # 能否热度评分

    # 降级说明
    degradation_reason: str = ""


async def detect_capability(db: Any) -> MemoryCapability:
    """
    检测当前记忆系统能力等级。

    Args:
        db: 数据库会话，用于解析模型绑定

    Returns:
        MemoryCapability: 当前能力和可用功能
    """
    # 检测 LLM 模型（memory purpose）
    has_llm = False
    try:
        from app.modules.endpoint import service as endpoint_service

        llm_model = await endpoint_service.resolve(db, purpose="memory")
        has_llm = llm_model is not None
    except Exception as e:
        log.debug("detect_llm_failed", error=str(e))

    # 检测 Embedding 模型
    has_embedding = False
    try:
        from app.modules.memory import service as memory_service

        embedding_model = await memory_service.resolve_embedding_model(db)
        has_embedding = embedding_model is not None
    except Exception as e:
        log.debug("detect_embedding_failed", error=str(e))

    # 检测 Rerank 模型【绑定】（不依赖开关）。
    # 绑定了就算"已配置"，开关（recall_enable_rerank）只决定"实际用不用"。
    rerank_bound = False
    try:
        from app.modules.memory import service as memory_service

        rerank_model = await memory_service.resolve_rerank_model(db)
        rerank_bound = rerank_model is not None
    except Exception as e:
        log.debug("detect_rerank_failed", error=str(e))

    has_rerank = rerank_bound
    can_rerank = rerank_bound and settings.memory.recall_enable_rerank

    # 决定能力等级和降级原因
    if has_llm and has_embedding and can_rerank:
        level = MemoryCapabilityLevel.FULL
        reason = ""
    elif has_llm and has_embedding:
        level = MemoryCapabilityLevel.STANDARD
        if rerank_bound and not settings.memory.recall_enable_rerank:
            reason = "Rerank 模型已绑定，但功能开关（recall_enable_rerank）未启用"
        else:
            reason = "Rerank 未配置，召回质量降低约 15%"
    elif has_llm:
        level = MemoryCapabilityLevel.BASIC
        reason = "Embedding 未配置，降级到关键词搜索，召回质量降低约 40%"
    else:
        level = MemoryCapabilityLevel.NONE
        reason = "LLM 未配置，记忆系统不可用，降级到循环压缩模式"

    # 确定功能开关
    can_extract = has_llm
    can_vector_search = has_embedding
    can_keyword_search = True  # 总是可用
    can_recursive = has_embedding  # 递归搜索需要向量
    can_hybrid = has_embedding  # 混合搜索需要向量
    can_hotness = has_embedding  # 热度评分需要访问记录（向量搜索才有）

    capability = MemoryCapability(
        level=level,
        has_llm=has_llm,
        has_embedding=has_embedding,
        has_rerank=has_rerank,
        can_extract=can_extract,
        can_vector_search=can_vector_search,
        can_keyword_search=can_keyword_search,
        can_rerank=can_rerank,
        can_recursive=can_recursive,
        can_hybrid=can_hybrid,
        can_hotness=can_hotness,
        degradation_reason=reason,
    )

    log.info(
        "memory_capability_detected",
        level=level.name,
        has_llm=has_llm,
        has_embedding=has_embedding,
        has_rerank=has_rerank,
        reason=reason,
    )

    return capability


def get_recommendations(capability: MemoryCapability) -> list[dict[str, str]]:
    """
    根据当前能力生成配置建议。

    Args:
        capability: 当前能力

    Returns:
        建议列表，每条包含 type, message, action
    """
    recommendations = []

    if not capability.has_llm:
        recommendations.append(
            {
                "type": "critical",
                "message": "未配置 LLM 模型，记忆系统不可用",
                "action": "配置 JEEVES_LLM__DEFAULT_MODEL 环境变量",
            }
        )

    if not capability.has_embedding:
        recommendations.append(
            {
                "type": "warning",
                "message": "未配置 Embedding 模型，无法使用向量搜索",
                "action": "在设置页绑定 Embedding 模型到 'embedding' purpose",
            }
        )

    if not capability.has_rerank:
        recommendations.append(
            {
                "type": "info",
                "message": "未配置 Rerank 模型，召回质量会降低约 15%（可选）",
                "action": "在设置页绑定 Rerank 模型到 'memory_rerank' purpose",
            }
        )

    return recommendations
