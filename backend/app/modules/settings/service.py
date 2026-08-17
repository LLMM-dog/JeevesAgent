"""
运行时设置的读写与生效。

## 生效方式：启动时 + 每次修改后覆盖 settings 对象

不在每个读取点查数据库 —— 那会让 `settings.memory.max_msg_chars`
这样的表达式变成一次 await，污染所有调用点（包括同步函数）。

做法是把用户设置【应用到内存里的 settings 对象】：启动时加载一次，
用户改动时立即重新应用。代价是多进程部署时其他进程要等下次重启 ——
而这个项目是单进程的（个人工具），那个代价不存在。
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.settings.models import AppSetting

log = structlog.get_logger(__name__)


# 允许用户改的设置白名单。
#
# ## 为什么要白名单
#
# 不能让前端写任意 key —— `security.encryption_key` 或 `db.path`
# 被改掉会直接破坏系统。而且没有白名单时前端无法知道"有哪些可调项"，
# 只能硬编码一份列表，那份列表会和后端不同步。
#
# 每项是 (点分 key, 类型, 最小值, 最大值, 说明)。
# 范围用于校验 —— 用户填 0 或负数会让截断逻辑崩掉。
SETTABLE: dict[str, dict[str, Any]] = {
    "memory.enabled": {
        "type": bool,
        "label": "启用记忆系统",
        "hint": "关掉后不提取、不召回，但已有记忆文件保留",
    },
    "memory.keep_recent_turns": {
        "type": int,
        "min": 0,
        "max": 50,
        "label": "保留最近几轮不提取",
        "hint": "正在进行的对话不该被总结。按 user 消息分轮",
    },
    "memory.extract_max_iterations": {
        "type": int,
        "min": 1,
        "max": 10,
        "label": "提取循环上限",
        "hint": "基础预算。工具调用、格式重试、patch 修复会各自额外 +1",
    },
    "memory.max_items_per_extraction": {
        "type": int,
        "min": 1,
        "max": 100,
        "label": "单次提取最多写几条",
        "hint": "防止模型把一段对话拆成几十个碎片事件",
    },
    "memory.eager_prefetch": {
        "type": bool,
        "label": "全量预取记忆",
        "hint": "开：一次给模型全部记忆正文，不给工具。关：只给索引，模型自己按需 read",
    },
    "memory.prefetch_topn": {
        "type": int,
        "min": 1,
        "max": 50,
        "label": "预取条数上限",
        "hint": "仅在关闭全量预取时生效",
    },
    "memory.max_msg_chars": {
        "type": int,
        "min": 200,
        "max": 20_000,
        "label": "单条消息截断长度",
        "hint": "超长消息保留头尾，中段省略。工具结果的结论通常在结尾",
    },
    "memory.max_conversation_chars": {
        "type": int,
        "min": 2_000,
        "max": 500_000,
        "label": "单次提取的对话总量上限",
        "hint": "超了从最早的轮次开始丢。窗口小的模型要调低",
    },
    "memory.prefetch_preview_chars": {
        "type": int,
        "min": 200,
        "max": 20_000,
        "label": "预取时每条记忆展示长度",
        "hint": "太短会让模型拿不到可匹配的原文，只能新建而不是修改",
    },
    "memory.prefetch_max_chars": {
        "type": int,
        "min": 2_000,
        "max": 200_000,
        "label": "预取总字符预算",
        "hint": "记忆预取和对话内容抢同一个窗口。吃太多就没地方放对话，而对话才是提取的原料",
    },
    "memory.tool_read_max_chars": {
        "type": int,
        "min": 500,
        "max": 50_000,
        "label": "read_memory 返回长度上限",
    },
    "memory.tool_search_limit": {
        "type": int,
        "min": 1,
        "max": 30,
        "label": "search_memories 返回条数",
    },
    "memory.embedding_max_bytes": {
        "type": int,
        "min": 1_000,
        "max": 200_000,
        "label": "嵌入文本字节上限",
        "hint": "按字节而非字符 —— 中文一个字 3 字节。超过模型上限会报 400",
    },
    "memory.embedding_batch_size": {
        "type": int,
        "min": 1,
        "max": 512,
        "label": "嵌入批大小",
        "hint": "供应商限制差异很大，超限会报 400 而不是自动分批",
    },
    "memory.search_min_score": {
        "type": float,
        "min": 0.0,
        "max": 1.0,
        "label": "语义搜索最低相似度",
        "hint": "0 表示不过滤。合适的值强依赖嵌入模型，需要按自己的模型调",
    },
    "memory.auto_commit_pending_token_threshold": {
        "type": int,
        "min": 1_000,
        "max": 1_000_000,
        "label": "待处理 token 触发阈值",
        "hint": "自上次提取后累计的新消息 token 达到这个数就自动提取。模型窗口大（1M）可调大，减少提取频率",
    },
    "memory.auto_commit_message_count_threshold": {
        "type": int,
        "min": 10,
        "max": 10_000,
        "label": "待处理消息数触发阈值",
        "hint": "自上次提取后累计的新消息达到这个数就自动提取",
    },
    "memory.auto_commit_use_context_percentage": {
        "type": bool,
        "label": "启用窗口百分比触发",
        "hint": "开：待处理 token 超过「模型窗口 × 百分比」也触发。大窗口下这是主要触发条件",
    },
    "memory.auto_commit_context_usage_percentage": {
        "type": float,
        "min": 0.1,
        "max": 0.99,
        "label": "窗口百分比触发阈值",
        "hint": "0.8 表示窗口用了 80% 就触发。仅在启用窗口百分比时生效",
    },
    "memory.auto_commit_keep_recent_count": {
        "type": int,
        "min": 0,
        "max": 50,
        "label": "提取时保留最近几条消息",
        "hint": "正在进行的对话不该被总结。保留末尾这几条不参与提取",
    },
    "memory.auto_commit_min_interval_seconds": {
        "type": int,
        "min": 0,
        "max": 86_400,
        "label": "最小提取间隔（秒）",
        "hint": "两次自动提取之间至少间隔这么久，防止频繁提交",
    },
    "websearch.backend": {
        "type": str,
        "label": "网络搜索后端",
        "hint": "none = 关闭，ddg = DuckDuckGo（免费），tavily = Tavily（需要 API Key）",
    },
    "websearch.tavily_api_key": {
        "type": str,
        "label": "Tavily API Key",
        "hint": "使用 Tavily 搜索时必填，从 https://tavily.com 获取",
        "sensitive": True,  # 标记为敏感信息
    },
}


def _coerce(raw: str, target: type) -> Any:
    """
    字符串 → 目标类型。失败时抛 ValueError 由调用方处理。

    bool 单独处理：bool("false") 是 True，直接转会让"关闭"变成"开启"。
    """
    if target is bool:
        low = raw.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"不是合法的布尔值：{raw!r}")
    if target is int:
        return int(raw)
    if target is float:
        return float(raw)
    return raw


def _get_default(key: str) -> Any:
    """从 settings 对象读【当前值】作为默认。"""
    node: Any = settings
    for part in key.split("."):
        node = getattr(node, part, None)
        if node is None:
            return None
    return node


def validate(key: str, value: Any) -> Any:
    """
    校验并归一化一个设置值。不在白名单或超范围时抛 ValueError。

    范围校验是必须的：`max_msg_chars=0` 会让截断函数把所有消息切成空串，
    而那个后果要到"提取产出 0 条记忆"时才显现，根因完全看不出来。
    """
    spec = SETTABLE.get(key)
    if spec is None:
        raise ValueError(f"不允许修改的设置项：{key}")

    target = spec["type"]
    coerced = _coerce(str(value), target) if not isinstance(value, target) else value

    # 特殊校验：websearch.backend 只能是指定值之一
    if key == "websearch.backend" and coerced not in ("none", "ddg", "tavily"):
        raise ValueError(f"websearch.backend 必须是 none、ddg 或 tavily，收到：{coerced}")

    if target is not bool:
        low, high = spec.get("min"), spec.get("max")
        if low is not None and coerced < low:
            raise ValueError(f"{key} 不能小于 {low}（收到 {coerced}）")
        if high is not None and coerced > high:
            raise ValueError(f"{key} 不能大于 {high}（收到 {coerced}）")
    return coerced


async def load_all(db: AsyncSession) -> dict[str, Any]:
    """读出用户改过的设置。未改过的项不在结果里。"""
    rows = list((await db.execute(select(AppSetting))).scalars())
    out: dict[str, Any] = {}
    for row in rows:
        spec = SETTABLE.get(row.key)
        if spec is None:
            # 白名单收缩后残留的旧 key。忽略而非报错 —— 用户不该
            # 因为我们删了一个设置项就打不开设置页。
            continue
        try:
            out[row.key] = _coerce(row.value, spec["type"])
        except ValueError:
            log.warning("app_setting_invalid_value", key=row.key, value=row.value)
    return out


def apply(values: dict[str, Any]) -> None:
    """
    把设置应用到内存里的 settings 对象。

    只改白名单里的 key，且路径必须已存在 —— 不创建新属性，
    那会掩盖拼写错误（写错的 key 会静默变成一个没人读的新字段）。
    """
    for key, value in values.items():
        if key not in SETTABLE:
            continue
        parts = key.split(".")
        node: Any = settings
        for part in parts[:-1]:
            node = getattr(node, part, None)
            if node is None:
                break
        if node is None or not hasattr(node, parts[-1]):
            log.warning("app_setting_unknown_path", key=key)
            continue
        setattr(node, parts[-1], value)


async def reload(db: AsyncSession) -> dict[str, Any]:
    """从库里加载并立即生效。启动时和每次修改后调用。"""
    values = await load_all(db)
    apply(values)
    if values:
        log.info("app_settings_applied", count=len(values), keys=sorted(values))
    return values


async def set_many(db: AsyncSession, updates: dict[str, Any]) -> dict[str, Any]:
    """
    批量写入并立即生效。任一项校验失败则【整批不写】。

    全或无而不是逐项尽力：部分生效会让用户看到一个混合状态
    （改了 3 项，2 项生效），而他不知道是哪 2 项。
    """
    validated = {key: validate(key, value) for key, value in updates.items()}

    try:
        for key, value in validated.items():
            raw = "true" if value is True else "false" if value is False else str(value)
            row = await db.get(AppSetting, key)
            if row is None:
                db.add(AppSetting(key=key, value=raw))
            else:
                row.value = raw

        await db.commit()
        apply(validated)
        log.info("app_settings_updated", keys=sorted(validated))
        return validated
    except Exception as e:
        log.error("app_settings_update_failed", error=str(e), keys=sorted(updates))
        await db.rollback()
        raise


async def reset(db: AsyncSession, keys: list[str] | None = None) -> int:
    """
    恢复默认 = 删行。删完要重新应用 —— 否则内存里还是用户改过的值。

    keys 为 None 时全部恢复。
    """
    stmt = delete(AppSetting)
    if keys:
        stmt = stmt.where(AppSetting.key.in_(keys))
    result = await db.execute(stmt)
    await db.commit()

    # 删行不会让 settings 对象自动回到默认值 —— 那些值是启动时从
    # 环境变量算出来的，被 apply() 覆盖过。重新构造一个默认实例来取值。
    from app.core.config import MemoryConfig

    defaults = MemoryConfig()
    for key in keys or list(SETTABLE):
        if not key.startswith("memory."):
            continue
        field = key.split(".", 1)[1]
        if hasattr(defaults, field):
            setattr(settings.memory, field, getattr(defaults, field))

    await reload(db)
    return int(result.rowcount or 0)


def describe() -> list[dict[str, Any]]:
    """
    可调项的元信息 + 当前值。给前端渲染设置页 ——
    前端不该硬编码一份可调项列表，那份列表会和后端不同步。

    section 由 key 前缀推导（memory / websearch），前端据此把项归到
    不同设置页 —— 比如 websearch 项不该出现在「记忆」设置页，它有自己的
    /api/websearch 页。SETTABLE 里保留 websearch 项是因为 set_many 校验
    需要它，但渲染时要能按 section 分开。
    """
    out: list[dict[str, Any]] = []
    for key, spec in SETTABLE.items():
        out.append(
            {
                "key": key,
                "section": key.split(".")[0],
                "type": spec["type"].__name__,
                "label": spec.get("label", key),
                "hint": spec.get("hint", ""),
                "min": spec.get("min"),
                "max": spec.get("max"),
                "value": _get_default(key),
            }
        )
    return out
