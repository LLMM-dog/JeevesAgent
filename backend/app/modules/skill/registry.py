"""
技能索引的进程内单例。

## 为什么不用 lru_cache

SonethoHere 的 `build_system_prompt` 挂了 `@lru_cache(maxsize=1)`，docstring
写着"进程生命周期内只组装一次"。后果是**改任何技能都必须重启进程**。

更隐蔽的问题：它的 `/skills` 端点是实时扫描的，而系统提示词走缓存。
两者会长期不一致 —— 前端列出来的技能和模型实际看到的可能不是同一批，
而这种不一致没有任何提示。

所以这里用显式的 `reload()`，并且**只有一个真源**：前端列表和系统提示词
都读同一个 index 对象。
"""

from __future__ import annotations

import structlog

from app.modules.skill.loader import SkillIndex, load_index

log = structlog.get_logger(__name__)

_index: SkillIndex | None = None


def get_index() -> SkillIndex:
    """
    取当前索引。首次调用时懒加载。

    懒加载而不是要求启动时必须初始化：测试、脚本、SubAgent 都可能在没走
    startup 的情况下用到技能，那时应该能正常工作而不是拿到空索引。
    """
    global _index
    if _index is None:
        _index = load_index()
    return _index


def reload() -> SkillIndex:
    """重扫技能目录。上传新技能包后调用，不需要重启。"""
    global _index
    _index = load_index()
    return _index


def set_index(index: SkillIndex) -> None:
    """测试用：直接注入一个索引。"""
    global _index
    _index = index


def reset() -> None:
    """测试用：清掉索引，下次 get_index 会重新扫。"""
    global _index
    _index = None
