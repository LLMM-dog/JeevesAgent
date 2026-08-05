"""
路径守卫测试。

安全测试必须是穷举式的参数化测试。【每发现一个新的绕过方式就加一个 case，
永不删除。】
"""

from pathlib import Path

import pytest
from app.core.exceptions import PathDeniedError
from app.modules.agent.pathguard import BLOCKER_FILENAME, AllowedPath, PathGuard


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("x", encoding="utf-8")
    (root / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("s", encoding="utf-8")
    return root


@pytest.fixture
def guard(ws: Path) -> PathGuard:
    return PathGuard(allowed=[AllowedPath(path=ws.resolve(), can_write=True)])


class TestAllow:
    def test_file_inside(self, guard: PathGuard, ws: Path) -> None:
        assert guard.check(ws / "src" / "main.py") == (ws / "src" / "main.py").resolve()

    def test_root_itself(self, guard: PathGuard, ws: Path) -> None:
        assert guard.check(ws) == ws.resolve()

    def test_nonexistent_path_for_write(self, guard: PathGuard, ws: Path) -> None:
        """write_file 新建文件时路径还不存在，必须允许校验。"""
        assert guard.check(ws / "new" / "f.py", write=True) == (ws / "new" / "f.py").resolve()

    def test_dotdot_that_stays_inside(self, guard: PathGuard, ws: Path) -> None:
        """src/../src/main.py 仍在白名单内，应放行。"""
        assert guard.check(ws / "src" / ".." / "src" / "main.py") == (
            ws / "src" / "main.py"
        ).resolve()


class TestDeny:
    @pytest.mark.parametrize(
        "rel",
        [
            "../outside/secret.txt",
            "../../etc/passwd",
            "src/../../outside/secret.txt",
            "src/../../../",
        ],
    )
    def test_traversal(self, guard: PathGuard, ws: Path, rel: str) -> None:
        with pytest.raises(PathDeniedError):
            guard.check(ws / rel)

    def test_absolute_outside(self, guard: PathGuard, tmp_path: Path) -> None:
        with pytest.raises(PathDeniedError):
            guard.check(tmp_path / "outside" / "secret.txt")

    def test_empty_whitelist_denies_everything(self, ws: Path) -> None:
        """
        白名单为空时必须全拒。反面做法（空=全放行）会在配置丢失时
        静默变成完全无防护。
        """
        g = PathGuard(allowed=[])
        with pytest.raises(PathDeniedError):
            g.check(ws / "src" / "main.py")

    @pytest.mark.parametrize(
        "name",
        [
            ".env",
            ".env.local",
            ".env.production",
            "server.pem",
            "private.key",
            "id_rsa",
            "id_rsa.pub",
            "id_ed25519",
            "cert.pfx",
            ".netrc",
            ".npmrc",
            "credentials.json",
        ],
    )
    def test_hard_deny_beats_whitelist(self, guard: PathGuard, ws: Path, name: str) -> None:
        """
        硬性拒止优先级高于白名单 —— 即使白名单放行了所在目录。
        用户很容易顺手把项目根加进白名单，那时 .env 就暴露了。
        """
        target = ws / name
        target.write_text("x", encoding="utf-8")
        with pytest.raises(PathDeniedError) as ei:
            guard.check(target)
        assert ei.value.code == "path_hard_denied"

    def test_hard_deny_in_subdir(self, guard: PathGuard, ws: Path) -> None:
        target = ws / "src" / ".env"
        target.write_text("x", encoding="utf-8")
        with pytest.raises(PathDeniedError):
            guard.check(target)

    def test_readonly_blocks_write_allows_read(self, ws: Path) -> None:
        g = PathGuard(allowed=[AllowedPath(path=ws.resolve(), can_write=False)])
        g.check(ws / "src" / "main.py")  # 读放行
        with pytest.raises(PathDeniedError) as ei:
            g.check(ws / "src" / "main.py", write=True)
        assert ei.value.code == "path_readonly"


class TestBlocker:
    def test_anchor_blocks_directory_and_children(self, guard: PathGuard, ws: Path) -> None:
        protected = ws / "important"
        (protected / "deep").mkdir(parents=True)
        (protected / BLOCKER_FILENAME).write_text("", encoding="utf-8")
        (protected / "deep" / "f.txt").write_text("x", encoding="utf-8")

        guard.clear_cache()
        for target in (protected, protected / "deep" / "f.txt"):
            with pytest.raises(PathDeniedError) as ei:
                guard.check(target)
            assert ei.value.code == "path_blocked_by_anchor"

    def test_sibling_unaffected(self, guard: PathGuard, ws: Path) -> None:
        protected = ws / "important"
        protected.mkdir()
        (protected / BLOCKER_FILENAME).write_text("", encoding="utf-8")
        guard.clear_cache()
        # 兄弟目录不受影响
        guard.check(ws / "src" / "main.py")

    def test_anchor_at_workspace_root_blocks_all(self, guard: PathGuard, ws: Path) -> None:
        (ws / BLOCKER_FILENAME).write_text("", encoding="utf-8")
        guard.clear_cache()
        with pytest.raises(PathDeniedError):
            guard.check(ws / "src" / "main.py")

    def test_cache_respects_ttl_zero(self, ws: Path) -> None:
        """TTL=0 时每次都重新扫，用于验证缓存不会让新放的锚失效。"""
        g = PathGuard(allowed=[AllowedPath(path=ws.resolve())], cache_ttl_ms=0)
        g.check(ws / "src" / "main.py")
        (ws / "src" / BLOCKER_FILENAME).write_text("", encoding="utf-8")
        with pytest.raises(PathDeniedError):
            g.check(ws / "src" / "main.py")


class TestSymlink:
    def test_symlink_pointing_outside_denied(self, guard: PathGuard, ws: Path, tmp_path: Path) -> None:
        """
        符号链接能指到白名单外。resolve() 会解开它，所以能拦住。
        Windows 上创建符号链接需要权限，无权限则跳过。
        """
        link = ws / "escape"
        try:
            link.symlink_to(tmp_path / "outside", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("当前环境无法创建符号链接（Windows 需要管理员权限或开发者模式）")
        with pytest.raises(PathDeniedError):
            guard.check(link / "secret.txt")
