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

from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

import structlog

from app.core.exceptions import PathDeniedError
from app.core.time import now_ms

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


def get_guard() -> PathGuard:
    global _guard
    if _guard is None:
        _guard = PathGuard()
    return _guard


def set_allowed(entries: list[AllowedPath]) -> None:
    g = get_guard()
    g.allowed = entries
    g.clear_cache()
    log.info("pathguard_updated", count=len(entries))
