"""
路径守卫。

三道防线里的前两道（白名单 + 拒止锚）。第三道是审批。

## 必须先 resolve() 再比对

字符串前缀匹配可以被 workspace/../../../etc/passwd 绕过；符号链接也能指到
白名单外。提示词加载上同样容易踩：key 来自 HTTP 路径参数直接拼路径，
传 ../../../../Windows/win 能读到目录外任意 .md，实测能逃出去。

## 调用方必须使用返回的 resolved 路径

check() 通过后又用原始 path 去 open，等于没检查。
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from app.core.exceptions import PathDeniedError
from app.core.time import now_ms

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

BLOCKER_FILENAME = ".jeeves_blocker"

# 硬编码拒止列表，【优先级高于白名单】。
# 即使用户把项目根目录加进了白名单（很容易顺手这么做），
# 这些文件名模式也一律拒绝 —— 否则 agent 能读到 .env 里的明文 Key。
HARD_DENY_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    "*.pfx",
    "*.p12",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
)


@dataclass
class AllowedPath:
    path: Path
    can_write: bool = True


@dataclass
class PathGuard:
    allowed: list[AllowedPath] = field(default_factory=list)
    # 拒止锚检查要向上遍历目录树，每次文件操作都做太贵。
    # 缓存 目录 → (是否有锚, 锚位置, 写入时刻)
    _anchor_cache: dict[str, tuple[Path | None, int]] = field(default_factory=dict)
    cache_ttl_ms: int = 60_000

    def clear_cache(self) -> None:
        self._anchor_cache.clear()

    def check(self, raw: str | Path, *, write: bool = False) -> Path:
        """
        返回 resolved 路径。调用方【必须】使用返回值，不能用原始入参。
        """
        p = Path(raw)
        # strict=False：允许校验尚不存在的路径（write_file 新建文件的场景）
        resolved = p.resolve(strict=False)

        self._check_hard_deny(resolved)
        entry = self._match_allowed(resolved)
        if entry is None:
            raise PathDeniedError(
                f"路径不在白名单内：{resolved}",
                hint=(
                    "在设置页添加该目录，或把文件移动到工作区内。"
                    f"当前白名单：{', '.join(str(a.path) for a in self.allowed) or '（空）'}"
                ),
            )
        if write and not entry.can_write:
            raise PathDeniedError(
                f"该路径为只读：{resolved}", code="path_readonly", hint="在设置页把它改为可写"
            )

        anchor = self._find_anchor(resolved)
        if anchor is not None:
            raise PathDeniedError(
                f"路径被拒止锚阻断：{resolved}",
                code="path_blocked_by_anchor",
                hint=f"锚文件位于 {anchor}。删除它可解除阻断",
            )

        return resolved

    @staticmethod
    def _check_hard_deny(resolved: Path) -> None:
        name = resolved.name.lower()
        for pat in HARD_DENY_PATTERNS:
            if fnmatch(name, pat):
                raise PathDeniedError(
                    f"该文件类型被硬性拒止：{resolved.name}",
                    code="path_hard_denied",
                    hint="凭证与密钥文件一律不可访问，白名单也无法放行",
                )

    def _match_allowed(self, resolved: Path) -> AllowedPath | None:
        for entry in self.allowed:
            try:
                if resolved == entry.path or resolved.is_relative_to(entry.path):
                    return entry
            except (OSError, ValueError):
                continue
        return None

    def _find_anchor(self, resolved: Path) -> Path | None:
        """
        从该路径向上逐级检查是否存在 .jeeves_blocker。

        拒止锚是"就地否决"：把标记文件放进去，不用改任何配置，
        而且它跟着目录移动 —— 目录换个位置，保护还在。
        """
        start = resolved if resolved.is_dir() else resolved.parent
        key = str(start)
        now = now_ms()

        cached = self._anchor_cache.get(key)
        if cached is not None and now - cached[1] < self.cache_ttl_ms:
            return cached[0]

        found: Path | None = None
        cur = start
        while True:
            candidate = cur / BLOCKER_FILENAME
            try:
                if candidate.exists():
                    found = candidate
                    break
            except OSError:
                pass
            if cur.parent == cur:
                break
            cur = cur.parent

        self._anchor_cache[key] = (found, now)
        return found


_guard: PathGuard | None = None

# 当前会话的 guard。
#
# ## 为什么用 ContextVar
#
# 白名单是【会话级】的：给 A 会话开了 D:\proj 的写权限，不该让 B 会话
# 也能写。但工具函数拿不到会话上下文 —— `get_guard()` 在 file.py 里
# 被调用 6 次，exec.py 1 次，refs.py 2 次，全都是纯函数式的调用点。
#
# 改成层层传参要动所有工具的签名（而 ToolContext 里塞 guard 也一样要
# 改每个调用点）。ContextVar 让"当前会话是谁"隐式可得，
# 和事件总线用的是同一个模式。
#
# asyncio 里每个 Task 继承创建时的 context，所以子代理（在自己的 Task
# 里跑）会拿到父会话的 guard —— 这是想要的行为：子代理不该有比
# 派它的会话更大的权限。
_session_guard: ContextVar[PathGuard | None] = ContextVar("session_guard", default=None)


def get_guard() -> PathGuard:
    """
    取当前生效的 guard。

    有会话级的就用会话级，否则回落到全局 —— 后者用于启动期、
    定时任务、以及测试里直接调工具的场景。
    """
    scoped = _session_guard.get()
    if scoped is not None:
        return scoped
    global _guard
    if _guard is None:
        _guard = PathGuard()
    return _guard


def set_allowed(entries: list[AllowedPath]) -> None:
    """设置【全局】白名单。启动时调一次。"""
    g = _guard if _guard is not None else get_guard()
    g.allowed = entries
    g.clear_cache()
    log.info("pathguard_updated", count=len(entries))


@contextmanager
def scoped_guard(entries: list[AllowedPath]) -> Iterator[PathGuard]:
    """
    在一段代码里换用会话级白名单。

    用法：

        with scoped_guard(entries):
            ...   # 这里的 get_guard() 返回会话级的

    退出时自动还原 —— 用 reset(token) 而不是设回 None，
    因为嵌套调用（子代理）时设回 None 会让外层也丢掉 guard。
    """
    g = PathGuard()
    g.allowed = entries
    token = _session_guard.set(g)
    try:
        yield g
    finally:
        _session_guard.reset(token)

async def load_session_allowed(db: "AsyncSession", session_id: str) -> list[AllowedPath]:
    """
    取某个会话生效的白名单：会话级条目 + 全局条目。

    ## 为什么要合并而不是只用会话级

    全局条目里有内置的两条 —— 项目的 workspace/ 和 data/uploads。
    只用会话级的话，没设工作目录的会话会一条白名单都没有，
    agent 连上传的图片都读不了。

    ## 为什么每次 run 都查一遍

    用户可能在对话进行中改白名单。缓存的话新加的目录要等下一次
    重启才生效，而界面上已经显示"已添加"—— 那种不一致很难排查。

    一次 run 只查一次，成本是一条 SELECT，可以忽略。
    """
    from sqlalchemy import or_, select

    from app.modules.provider.models import PathWhitelist

    rows = list(
        (
            await db.execute(
                select(PathWhitelist).where(
                    or_(
                        PathWhitelist.session_id == session_id,
                        PathWhitelist.session_id.is_(None),
                    )
                )
            )
        ).scalars()
    )
    return [
        AllowedPath(path=Path(r.path), can_write=bool(r.can_write)) for r in rows
    ]
