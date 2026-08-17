"""
自动记忆提取。

就做两件事：
1. 检查是否该触发了（token 数或消息数）
2. 触发 commit_session
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

if TYPE_CHECKING:
    pass

log = structlog.get_logger(__name__)


async def check_and_trigger(db: AsyncSession, session_id: str) -> bool:
    """
    检查是否需要自动提取，需要就触发。

    在每次 add_message 后调用。

    Returns:
        True 表示触发了提取，False 表示不需要
    """
    if not settings.memory.enabled:
        return False

    # 读取会话
    from app.modules.session.models import Session
    stmt = select(Session).where(Session.id == session_id)
    session = (await db.execute(stmt)).scalar_one_or_none()
    if not session:
        return False

    # ── 计算待处理的消息和 token 数 ──
    from app.modules.memory.commit import get_latest_archive_summary
    from app.modules.session import repo

    # 从归档获取 watermark
    latest_archive = await get_latest_archive_summary(session_id)
    last_seq = latest_archive.last_seq if latest_archive else -1

    # 读取新消息
    rows = await repo.load_messages(db, session_id, agent_name=None, after_seq=last_seq if last_seq >= 0 else None)

    if not rows:
        return False  # 没有新消息

    pending_messages = len(rows)

    # 粗略估算 token 数（每条消息平均 200 tokens）
    # TODO: 精确计算需要调用 tokenizer
    pending_tokens = sum(len(r.content) // 4 for r in rows)  # 1 token ≈ 4 字符

    # ── 判断是否触发 ──
    cfg = settings.memory

    # 条件1：固定 token 阈值
    if pending_tokens >= cfg.auto_commit_pending_token_threshold:
        log.info(
            "auto_commit_trigger_token_threshold",
            session_id=session_id,
            pending_tokens=pending_tokens,
            threshold=cfg.auto_commit_pending_token_threshold,
        )
        await _do_commit(db, session_id)
        return True

    # 条件2：上下文窗口百分比（可选）
    if cfg.auto_commit_use_context_percentage:
        # 获取所有智能体的最小窗口
        min_context_limit = await _get_min_context_limit(session)
        threshold = int(min_context_limit * cfg.auto_commit_context_usage_percentage)

        if pending_tokens >= threshold:
            log.info(
                "auto_commit_trigger_context_percentage",
                session_id=session_id,
                pending_tokens=pending_tokens,
                threshold=threshold,
                percentage=cfg.auto_commit_context_usage_percentage,
            )
            await _do_commit(db, session_id)
            return True

    # 条件3：消息数量阈值
    if pending_messages >= cfg.auto_commit_message_count_threshold:
        log.info(
            "auto_commit_trigger_message_count",
            session_id=session_id,
            pending_messages=pending_messages,
            threshold=cfg.auto_commit_message_count_threshold,
        )
        await _do_commit(db, session_id)
        return True

    # 条件4：最小间隔保护（防止频繁提交）
    # TODO: 从归档读取 last_commit_time

    return False


async def _do_commit(db: AsyncSession, session_id: str) -> None:
    """执行记忆提取。"""
    from app.core.ids import extraction_id as new_extraction_id
    from app.core.runtime_state import set_memory_extracting
    from app.modules.memory.commit import commit_session

    extraction_id = new_extraction_id()

    try:
        # 标记开始提取
        set_memory_extracting(session_id, True, extraction_id)

        # 获取记忆提取用的 LLM 调用函数（未配置时返回 None）
        from app.modules.llm import get_memory_extraction_llm_call

        llm_call = await get_memory_extraction_llm_call(db)

        if llm_call is None:
            log.debug("auto_commit_skipped_no_llm_model", session_id=session_id)
            return

        report = await commit_session(
            db,
            session_id=session_id,
            llm_call=llm_call,
            keep_recent_turns=settings.memory.auto_commit_keep_recent_count,
        )

        log.info("auto_commit_completed", session_id=session_id, summary=report.summary())
    except Exception as e:
        # hint 里通常是上游的具体错误（如模型不支持工具、参数非法），
        # 只记 str(e) 会丢掉真正原因。
        hint = getattr(e, "hint", None)
        log.error(
            "auto_commit_failed",
            session_id=session_id,
            error=str(e),
            hint=hint,
            exc_info=True,
        )
    finally:
        # 标记提取结束
        set_memory_extracting(session_id, False)


async def trigger_manual_extraction(db: AsyncSession, session_id: str) -> dict[str, Any]:
    """
    手动触发记忆提取。

    与自动触发的区别：
    1. 不检查阈值，立即提取
    2. 返回详细结果
    3. 防止重复提取（如果正在提取中，返回错误）

    Returns:
        {
            "success": bool,
            "message": str,
            "extraction_id": str,
            "summary": {...}  # 提取结果摘要（如果成功）
        }
    """
    from app.core.exceptions import ConflictError
    from app.core.ids import extraction_id as new_extraction_id
    from app.core.runtime_state import is_memory_extracting, set_memory_extracting
    from app.modules.memory.commit import commit_session

    # 检查是否正在提取
    if is_memory_extracting(session_id):
        raise ConflictError("该会话正在进行记忆提取，请稍后再试")

    # 检查记忆功能是否启用
    if not settings.memory.enabled:
        return {
            "success": False,
            "message": "记忆功能未启用",
            "extraction_id": "",
        }

    extraction_id = new_extraction_id()

    try:
        # 标记开始提取
        set_memory_extracting(session_id, True, extraction_id)

        # 获取记忆提取用的 LLM 调用函数（未配置时返回 None）
        from app.modules.llm import get_memory_extraction_llm_call

        llm_call = await get_memory_extraction_llm_call(db)

        if llm_call is None:
            return {
                "success": False,
                "message": "未配置记忆提取模型",
                "extraction_id": extraction_id,
            }

        # 执行提取
        report = await commit_session(
            db,
            session_id=session_id,
            llm_call=llm_call,
            keep_recent_turns=settings.memory.auto_commit_keep_recent_count,
        )

        log.info("manual_extraction_completed", session_id=session_id, summary=report.summary())

        return {
            "success": True,
            "message": "记忆提取完成",
            "extraction_id": extraction_id,
            "summary": report.summary(),
        }

    except Exception as e:
        log.error("manual_extraction_failed", session_id=session_id, error=str(e), exc_info=True)
        return {
            "success": False,
            "message": f"记忆提取失败: {str(e)}",
            "extraction_id": extraction_id,
        }
    finally:
        # 标记提取结束
        set_memory_extracting(session_id, False)


async def _get_min_context_limit(session: Any) -> int:
    """获取所有智能体中最小的上下文窗口。"""
    # TODO: 从 agent 配置读取 LLM 的 max_context_tokens
    # 目前返回默认值
    return 128000  # 128K
