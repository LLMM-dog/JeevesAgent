"""
技能包 zip 上传与校验。

## 为什么校验要这么严

带上传功能的实现不多，容易漏掉的校验很具体：

| 检查 | | 本项目 |
| --- | --- | --- |
| 扩展名白名单 | 有 | 有 |
| 必须含 SKILL.md | `endswith("SKILL.md")` —— 会匹配 `evilSKILL.md` | basename 精确比较 |
| 体积上限 | **无** | 有 |
| 文件数上限 | **无** | 有 |
| 解压后总体积 | **无（解压炸弹可打满磁盘）** | 有 |
| 路径穿越 | 依赖 `extractall` 的隐式清理 | 显式逐成员校验 |
| 符号链接条目 | **无防护** | 显式拒绝 |

## extractall 为什么不够

用的是 `zf.extractall(skill_dir)`。Python 的 `extractall` 会清理绝对
路径和 `..` 段，但**不阻止 zip 内的符号链接条目** —— 恶意包可以借此建立
指向宿主任意位置的链接，之后 load_skill_file 沿着链接就读出去了。

所以这里逐个成员校验，自己决定写哪些、写到哪里。
"""

from __future__ import annotations

import io
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from app.modules.skill.loader import SKILL_FILE, parse_frontmatter

log = structlog.get_logger(__name__)

# 限额依据：实测最大的真实技能包约 1575 KB / 55 个文件。留约 3 倍余量。
MAX_FILES = 80
MAX_UNCOMPRESSED_BYTES = 5 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 500 * 1024

# 允许写盘的扩展名。判据是"能不能当文本读给模型"或"是不是技能要用的资源"。
_ALLOWED_EXTS = frozenset(
    {
        ".md", ".txt", ".rst", ".json", ".yaml", ".yml", ".toml", ".csv",
        ".html", ".css", ".js", ".mjs", ".ts", ".tsx", ".jsx",
        ".py", ".sh", ".ps1", ".bat", ".sql", ".xml", ".ini", ".cfg",
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
        ".docx", ".xlsx", ".pptx", ".pdf",
    }
)


class SkillPackageError(Exception):
    """包不合法。上传时 fail-fast，不静默接受半个包。"""


@dataclass
class InstallResult:
    name: str
    files: int
    skipped: list[str] = field(default_factory=list)


def _is_symlink_entry(info: zipfile.ZipInfo) -> bool:
    """
    zip 条目是不是符号链接。

    zip 用 external_attr 的高 16 位存 Unix 模式位，S_IFLNK 是 0xA000。
    完全没有这个检查 —— 而这是 extractall 唯一不帮你挡的东西。
    """
    mode = info.external_attr >> 16
    return (mode & 0o170000) == 0o120000


def _safe_members(zf: zipfile.ZipFile) -> tuple[list[zipfile.ZipInfo], list[str]]:
    """
    逐成员校验，返回 (可写入的成员, 被跳过的说明)。

    不用 extractall —— 那样就把"写哪些文件"的决定权交给了 zip 内容。
    """
    members: list[zipfile.ZipInfo] = []
    skipped: list[str] = []
    total = 0

    for info in zf.infolist():
        raw = info.filename
        if info.is_dir():
            continue

        if _is_symlink_entry(info):
            # 符号链接可以指向宿主任意位置，之后 load_skill_file 沿着它
            # 就读出技能目录了。直接拒绝整个包，不是跳过 ——
            # 包含符号链接的技能包本身就值得怀疑。
            raise SkillPackageError(f"包内含符号链接条目：{raw}")

        norm = raw.replace("\\", "/")
        if norm.startswith("/") or ".." in Path(norm).parts:
            raise SkillPackageError(f"包内含非法路径：{raw}")
        # Windows 盘符（C:foo）也要挡
        if len(norm) > 1 and norm[1] == ":":
            raise SkillPackageError(f"包内含绝对路径：{raw}")

        ext = Path(norm).suffix.lower()
        if ext not in _ALLOWED_EXTS:
            # 扩展名不认识就跳过这个文件，但保留整个包 ——
            # 真实技能包常带 .DS_Store、.gitignore 这类无关文件。
            skipped.append(norm)
            continue

        if info.file_size > MAX_SINGLE_FILE_BYTES:
            raise SkillPackageError(
                f"{norm} 超过单文件上限（{info.file_size} > {MAX_SINGLE_FILE_BYTES} 字节）"
            )

        total += info.file_size
        if total > MAX_UNCOMPRESSED_BYTES:
            # 用【声明的解压后大小】判断，不是压缩包大小 ——
            # 这是防解压炸弹的关键。一个 1KB 的 zip 可以声明解压出几个 GB。
            raise SkillPackageError(
                f"解压后总体积超过上限（>{MAX_UNCOMPRESSED_BYTES} 字节）"
            )

        members.append(info)
        if len(members) > MAX_FILES:
            raise SkillPackageError(f"文件数超过上限（>{MAX_FILES}）")

    return members, skipped


def _locate_skill_md(members: list[zipfile.ZipInfo]) -> tuple[str, str]:
    """
    找 SKILL.md，返回 (它的 zip 内路径, 需要剥掉的前缀)。

    ## 为什么用 basename 精确比较

    用 `endswith("SKILL.md")`，会匹配到 `evilSKILL.md`、`notSKILL.md`。
    一个叫 `mySKILL.md` 的普通文档会被当成技能定义。

    ## 为什么要处理前缀

    从 GitHub 下载的 zip 通常多包一层目录（`repo-main/`）。不剥掉的话
    技能目录里会多一层，附件相对路径全都对不上。
    """
    candidates = [
        m.filename.replace("\\", "/")
        for m in members
        if Path(m.filename.replace("\\", "/")).name == SKILL_FILE
    ]
    if not candidates:
        raise SkillPackageError(f"包内没有 {SKILL_FILE}")

    # 取层级最浅的那个
    candidates.sort(key=lambda p: (p.count("/"), len(p)))
    shallowest = candidates[0]

    if len(candidates) > 1:
        # 多个 SKILL.md 说明把好几个技能打成了一个包。
        # 报错而不是随便挑一个 —— 用户的意图不明确。
        raise SkillPackageError(
            f"包内有 {len(candidates)} 份 {SKILL_FILE}（{', '.join(candidates)}）。"
            "一个包只能装一个技能，请分开上传"
        )

    prefix = shallowest[: -len(SKILL_FILE)]
    return shallowest, prefix


def inspect_package(data: bytes) -> tuple[str, str]:
    """
    只做校验并取出 (name, description)，不落盘。

    用于上传前的冲突检查 —— 先知道要装的是哪个技能，才能判断是否要
    提示用户确认覆盖。
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise SkillPackageError(f"不是合法的 zip 文件：{e}") from e

    with zf:
        members, _skipped = _safe_members(zf)
        skill_path, _prefix = _locate_skill_md(members)
        try:
            raw = zf.read(skill_path).decode("utf-8")
        except (UnicodeDecodeError, KeyError) as e:
            raise SkillPackageError(f"{SKILL_FILE} 读取失败：{e}") from e

    meta, _body = parse_frontmatter(raw)
    desc = meta.get("description")
    if not desc or not str(desc).strip():
        # 入口就拒收，不像 pi 那样宽松放行。
        #
        # description 是模型选择技能的唯一依据，没有它这个包装了也用不上。
        # 在上传时 fail-fast 是对的 —— 后续所有环节都能假定它合法。
        raise SkillPackageError(f"{SKILL_FILE} 缺少 description")

    name = meta.get("name")
    resolved = " ".join(str(name).split()) if name else ""
    if not resolved:
        # name 缺失时从 SKILL.md 所在目录名推。都没有就报错 ——
        # 落盘需要一个目录名。
        parent = Path(skill_path).parent.name
        resolved = parent
    if not resolved:
        raise SkillPackageError(f"无法确定技能名，请在 {SKILL_FILE} 里写 name")

    if "/" in resolved or "\\" in resolved or resolved in (".", ".."):
        # 技能名会被当作目录名用，必须挡掉路径字符
        raise SkillPackageError(f"技能名含非法字符：{resolved}")

    return resolved, " ".join(str(desc).split())


def install_package(data: bytes, skills_root: Path, *, overwrite: bool = False) -> InstallResult:
    """
    校验并安装技能包。

    先解压到临时目录再整体移动 —— 中途失败不会留下半个技能。
    """
    name, _desc = inspect_package(data)
    target = skills_root / name

    if target.exists() and not overwrite:
        raise SkillPackageError(f"技能 {name} 已存在")

    staging = skills_root / f".staging_{name}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            members, skipped = _safe_members(zf)
            _skill_path, prefix = _locate_skill_md(members)

            written = 0
            for info in members:
                norm = info.filename.replace("\\", "/")
                if prefix and not norm.startswith(prefix):
                    # 前缀之外的文件（GitHub zip 里的 README、LICENSE 等）
                    # 不属于这个技能，跳过
                    skipped.append(norm)
                    continue
                rel = norm[len(prefix) :] if prefix else norm
                if not rel:
                    continue

                dest = staging / rel
                # 最后一道防线：确认解析后的路径真的在 staging 内。
                # 前面已经逐成员校验过，这里是纵深防御 ——
                # 路径处理的 bug 太容易写出来。
                try:
                    dest.resolve().relative_to(staging.resolve())
                except ValueError as e:
                    raise SkillPackageError(f"路径逃出目标目录：{norm}") from e

                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out, length=64 * 1024)
                written += 1

        if target.exists():
            shutil.rmtree(target)
        staging.replace(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    log.info("skill_installed", name=name, files=written, skipped=len(skipped))
    return InstallResult(name=name, files=written, skipped=skipped)
