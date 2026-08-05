"""
技能包上传校验的测试。

重点是常见实现的缺口。是唯一有上传的，它缺：体积上限、文件数
上限、解压炸弹防护、符号链接防护，且用 endswith("SKILL.md") 定位（会匹配
evilSKILL.md）。
"""

import io
import zipfile
from pathlib import Path

import pytest
from app.modules.skill.package import (
    MAX_FILES,
    SkillPackageError,
    _is_symlink_entry,
    inspect_package,
    install_package,
)

GOOD_MD = "---\nname: demo\ndescription: 演示技能，用于测试上传校验\n---\n\n# 正文\n步骤\n"


def mkzip(entries: dict[str, bytes | str], *, symlinks: set[str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            info = zipfile.ZipInfo(name)
            # ZipInfo 默认 compress_type=ZIP_STORED，不继承 ZipFile 的设置。
            # 不显式设置的话"高度可压缩内容压完很小"这个前提不成立，
            # 解压炸弹的测试就测不到真实情况。
            info.compress_type = zipfile.ZIP_DEFLATED
            if symlinks and name in symlinks:
                # 0xA1FF0000 = S_IFLNK | 0777，符号链接的 external_attr
                info.external_attr = (0o120777 & 0xFFFF) << 16
            zf.writestr(info, data)
    return buf.getvalue()


class TestInspect:
    def test_valid_package(self) -> None:
        data = mkzip({"SKILL.md": GOOD_MD})
        name, desc = inspect_package(data)
        assert name == "demo"
        assert "演示技能" in desc

    def test_nested_single_dir_ok(self) -> None:
        """GitHub 下载的 zip 通常多包一层目录，要能正常处理。"""
        data = mkzip({"repo-main/SKILL.md": GOOD_MD})
        name, _ = inspect_package(data)
        assert name == "demo"

    def test_not_a_zip(self) -> None:
        with pytest.raises(SkillPackageError, match="zip"):
            inspect_package(b"this is not a zip")

    def test_missing_skill_md(self) -> None:
        with pytest.raises(SkillPackageError, match="SKILL.md"):
            inspect_package(mkzip({"README.md": "# hi"}))

    def test_evil_skill_md_not_matched(self) -> None:
        """
        `evilSKILL.md` 不能被当成 SKILL.md。

        用 endswith("SKILL.md")，会匹配到
        evilSKILL.md、notSKILL.md 这类文件名。这里用 basename 精确比较。
        """
        with pytest.raises(SkillPackageError, match="SKILL.md"):
            inspect_package(mkzip({"evilSKILL.md": GOOD_MD}))

    def test_multiple_skill_md_rejected(self) -> None:
        """
        多个 SKILL.md 说明把好几个技能打成了一个包，报错而不是随便挑一个。
        """
        data = mkzip({"a/SKILL.md": GOOD_MD, "b/SKILL.md": GOOD_MD})
        with pytest.raises(SkillPackageError, match="只能装一个技能"):
            inspect_package(data)

    def test_missing_description_rejected(self) -> None:
        """
        入口 fail-fast。description 是模型选择技能的唯一依据，
        没有它这个包装了也用不上。
        """
        data = mkzip({"SKILL.md": "---\nname: x\n---\n\n正文"})
        with pytest.raises(SkillPackageError, match="description"):
            inspect_package(data)

    def test_name_with_path_chars_rejected(self) -> None:
        """技能名会被当目录名用，必须挡掉路径字符。"""
        data = mkzip(
            {"SKILL.md": "---\nname: ../evil\ndescription: d\n---\n"}
        )
        with pytest.raises(SkillPackageError, match="非法字符"):
            inspect_package(data)


class TestPathTraversal:
    def test_parent_traversal_rejected(self) -> None:
        data = mkzip({"SKILL.md": GOOD_MD, "../escape.md": "x"})
        with pytest.raises(SkillPackageError, match="非法路径"):
            inspect_package(data)

    def test_absolute_path_rejected(self) -> None:
        data = mkzip({"SKILL.md": GOOD_MD, "/etc/passwd": "x"})
        with pytest.raises(SkillPackageError, match="绝对路径|非法路径"):
            inspect_package(data)

    def test_windows_drive_rejected(self) -> None:
        data = mkzip({"SKILL.md": GOOD_MD, "C:evil.md": "x"})
        with pytest.raises(SkillPackageError, match="绝对路径"):
            inspect_package(data)

    def test_symlink_entry_rejected(self) -> None:
        """
        zip 内的符号链接条目要拒绝整个包。

        用 extractall —— Python 会清理 .. 和绝对路径，但【不阻止符号
        链接条目】。恶意包可借此建立指向宿主任意位置的链接，之后
        load_skill_file 沿着它就读出技能目录了。这是 extractall 唯一
        不帮你挡的东西，而另一种做法 完全没有这个检查。
        """
        data = mkzip(
            {"SKILL.md": GOOD_MD, "link.md": "/etc/passwd"},
            symlinks={"link.md"},
        )
        with pytest.raises(SkillPackageError, match="符号链接"):
            inspect_package(data)

    def test_symlink_detection_helper(self) -> None:
        info = zipfile.ZipInfo("x")
        info.external_attr = (0o120777 & 0xFFFF) << 16
        assert _is_symlink_entry(info) is True
        plain = zipfile.ZipInfo("y")
        plain.external_attr = (0o100644 & 0xFFFF) << 16
        assert _is_symlink_entry(plain) is False


class TestLimits:
    def test_too_many_files(self) -> None:
        entries: dict[str, bytes | str] = {"SKILL.md": GOOD_MD}
        for i in range(MAX_FILES + 5):
            entries[f"refs/f{i}.md"] = "x"
        with pytest.raises(SkillPackageError, match="文件数"):
            inspect_package(mkzip(entries))

    def test_single_file_too_large(self) -> None:
        big = "x" * (600 * 1024)
        with pytest.raises(SkillPackageError, match="单文件上限"):
            inspect_package(mkzip({"SKILL.md": GOOD_MD, "refs/big.md": big}))

    def test_zip_bomb_rejected_by_declared_size(self) -> None:
        """
        用【声明的解压后大小】判断，不是压缩包大小。

        高度可压缩的内容：几 MB 的零字节压完只有几 KB。按压缩包大小判断
        的话完全挡不住。只累加 size_bytes 不设限，也没有解压后大小
        检查 —— 解压炸弹可以打满磁盘。
        """
        entries: dict[str, bytes | str] = {"SKILL.md": GOOD_MD}
        # 每个 400KB（低于单文件上限），20 个就超过 5MB 总上限
        for i in range(20):
            entries[f"refs/z{i}.txt"] = "0" * (400 * 1024)
        data = mkzip(entries)
        # 压缩后应该很小，证明按压缩包大小判断挡不住
        assert len(data) < 200 * 1024
        with pytest.raises(SkillPackageError, match="总体积"):
            inspect_package(data)

    def test_unknown_ext_skipped_not_rejected(self) -> None:
        """
        扩展名不认识就跳过该文件，保留整个包 ——
        真实技能包常带 .DS_Store、.gitignore 这类无关文件。
        """
        data = mkzip(
            {"SKILL.md": GOOD_MD, ".DS_Store": b"\x00\x01", "notes.xyz": "x"}
        )
        name, _ = inspect_package(data)
        assert name == "demo"


class TestInstall:
    def test_installs_and_reloads(self, tmp_path: Path) -> None:
        data = mkzip({"SKILL.md": GOOD_MD, "references/a.md": "参考"})
        result = install_package(data, tmp_path)
        assert result.name == "demo"
        assert (tmp_path / "demo" / "SKILL.md").is_file()
        assert (tmp_path / "demo" / "references" / "a.md").is_file()

    def test_strips_github_wrapper_dir(self, tmp_path: Path) -> None:
        """
        多包一层的 zip 要剥掉前缀，否则附件相对路径全都对不上。
        """
        data = mkzip(
            {"repo-main/SKILL.md": GOOD_MD, "repo-main/refs/a.md": "参考"}
        )
        install_package(data, tmp_path)
        assert (tmp_path / "demo" / "SKILL.md").is_file()
        assert (tmp_path / "demo" / "refs" / "a.md").is_file()
        assert not (tmp_path / "demo" / "repo-main").exists()

    def test_existing_rejected_without_overwrite(self, tmp_path: Path) -> None:
        data = mkzip({"SKILL.md": GOOD_MD})
        install_package(data, tmp_path)
        with pytest.raises(SkillPackageError, match="已存在"):
            install_package(data, tmp_path)

    def test_overwrite_replaces(self, tmp_path: Path) -> None:
        install_package(mkzip({"SKILL.md": GOOD_MD, "old.md": "旧"}), tmp_path)
        assert (tmp_path / "demo" / "old.md").is_file()

        install_package(
            mkzip({"SKILL.md": GOOD_MD, "new.md": "新"}), tmp_path, overwrite=True
        )
        assert (tmp_path / "demo" / "new.md").is_file()
        # 覆盖是整体替换，旧文件不该残留
        assert not (tmp_path / "demo" / "old.md").exists()

    def test_failed_install_leaves_no_staging(self, tmp_path: Path) -> None:
        """中途失败不能留下半个技能或 staging 目录。"""
        bad = mkzip({"SKILL.md": GOOD_MD, "../escape.md": "x"})
        with pytest.raises(SkillPackageError):
            install_package(bad, tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_installed_skill_is_loadable(self, tmp_path: Path) -> None:
        """装完的技能要能被索引加载 —— 端到端验证。"""
        from app.modules.skill.loader import load_index

        install_package(
            mkzip({"SKILL.md": GOOD_MD, "references/a.md": "参考"}), tmp_path
        )
        idx = load_index(tmp_path)
        assert "demo" in idx.skills
        assert "references/a.md" in idx.skills["demo"].files
