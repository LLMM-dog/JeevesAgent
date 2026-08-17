"""
技能的增删改查公共逻辑。

## 为什么要单独一个模块

技能的增删改查有两个入口：用户在设置页操作（HTTP API），和模型自己动手
（manage_skill 工具）。两条路必须落到同样的校验和同样的文件格式上 ——
否则会出现"界面建的技能模型读不了"或者反过来。

## 为什么不让模型直接用 write_file

把 skills/ 加进白名单让模型自己写文件是最省事的做法，但有三个问题：

  1. frontmatter 要模型自己拼。少一个 description 字段，技能会被静默跳过
     （只留一条 warning 诊断），而模型以为建好了。
  2. 建完不会 reload。索引是进程内单例，不重扫的话新技能要等重启才出现，
     模型和用户都会以为失败了。
  3. 白名单是会话级的可写目录 —— 给了写 skills/ 的权限，模型也就能
     覆盖任何已有的技能。而它只是想新建一个。

所以走一个受控的写入口：名字校验、必填字段校验、写完自动 reload。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import structlog

from app.core.config import settings
from app.core.exceptions import BadRequestError, ConflictError, NotFoundError
from app.modules.skill import registry as skill_registry
from app.modules.skill.loader import parse_frontmatter

log = structlog.get_logger(__name__)

SKILL_FILE = "SKILL.md"

# 名字直接当目录名用，所以必须限制字符集。
#
# 【不能只防 ../】。Windows 上 CON、NUL 这类保留名建不出目录；冒号和
# 反斜杠在路径里有特殊含义；空格结尾的目录名在 Windows 上会被静默去掉，
# 于是"我的技能 "和"我的技能"指向同一个目录 —— 而用户以为是两个。
#
# 只允许字母数字、连字符、下划线和中文。够表达，且不含任何路径语义。
_NAME_OK = re.compile(r"^[\w\u4e00-\u9fff-]{1,60}$")

# Windows 保留设备名。建这些名字的目录会失败，而错误信息完全不指向原因。
_WIN_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


@dataclass
class AuthorResult:
    name: str
    path: Path
    created: bool


def validate_name(name: str) -> str:
    """
    校验并归一化名字。

    ## 为什么 strip 之后还要再查一次

    " a " strip 成 "a" 是对的，但 "   " strip 成空串 —— 那时错误信息该说
    "名字不能为空"，而不是"名字含非法字符"。两种说法指向不同的修法。
    """
    got = (name or "").strip()
    if not got:
        raise BadRequestError("名字不能为空", code="invalid_name")
    if not _NAME_OK.match(got):
        raise BadRequestError(
            f"名字 {got!r} 含不允许的字符。只能用字母、数字、中文、连字符和下划线",
            code="invalid_name",
        )
    if got.lower() in _WIN_RESERVED:
        # 这条单独报 —— 用户看到"my-skill 不行"会以为是字符问题，
        # 而实际原因是它撞了系统保留名。
        raise BadRequestError(
            f"{got!r} 是系统保留名，换一个", code="reserved_name"
        )
    return got


def build_document(
    *,
    name: str,
    description: str,
    body: str,
    keywords: list[str] | None = None,
    version: str = "1.0",
) -> str:
    """
    拼出带 frontmatter 的完整文档。

    ## 为什么自己拼而不用 yaml.dump

    yaml.dump 会把中文转成 \\uXXXX 转义（除非 allow_unicode=True），
    而这些文件用户要直接看。而且它会给长描述加折行，
    折行后的 frontmatter 我们自己的 parse_frontmatter 能读，
    但用户手工编辑时很容易破坏缩进。

    描述压成一行，所以不需要处理多行 YAML 的引号规则。
    """
    desc = " ".join((description or "").split())
    if not desc:
        raise BadRequestError(
            "description 不能为空 —— 没有它的话这个条目会被加载器跳过，"
            "而且不会有任何报错",
            code="missing_description",
        )

    lines = ["---", f"name: {name}", f"description: {desc}", f"version: {version}"]
    if keywords:
        clean = [" ".join(str(k).split()) for k in keywords if str(k).strip()]
        if clean:
            lines.append("keywords: [" + ", ".join(clean) + "]")
    lines.append("---")
    lines.append("")
    lines.append((body or "").strip())
    lines.append("")
    return "\n".join(lines)


def _target_dir(kind: str, name: str) -> Path:
    if kind != "skill":
        raise BadRequestError(f"kind 只能是 skill，收到 {kind!r}")
    base = settings.skills_dir
    d = (base / name).resolve()
    # 【必须验证解析后仍在 base 下】。
    #
    # validate_name 已经挡掉了 ../ 和路径分隔符，这一层是双保险 ——
    # 名字校验的正则将来若被放宽（比如为了支持带点的名字），
    # 这里仍然拦得住。纵深防御，两道都留着。
    if not (d == base.resolve() or d.is_relative_to(base.resolve())):
        raise BadRequestError("名字解析出目标目录越界", code="path_escape")
    return d


def _file_name(kind: str) -> str:
    if kind != "skill":
        raise BadRequestError(f"kind 只能是 skill，收到 {kind!r}")
    return SKILL_FILE


def upsert(
    *,
    kind: str,
    name: str,
    description: str,
    body: str,
    keywords: list[str] | None = None,
    overwrite: bool = False,
) -> AuthorResult:
    """
    新建或更新。

    ## 为什么默认不覆盖

    模型建技能时用的名字是它自己起的，撞名很常见（"部署流程"这种）。
    默认覆盖的话它会悄悄冲掉用户手写的技能 —— 而用户不会收到任何提示。

    要覆盖必须显式传 overwrite=True，那时调用方（界面上的编辑按钮，
    或模型明确说"更新那个技能"）已经知道自己在改已有的东西。
    """
    if kind != "skill":
        raise BadRequestError(f"kind 只能是 skill，收到 {kind!r}")

    safe = validate_name(name)
    d = _target_dir(kind, safe)
    f = d / _file_name(kind)

    existed = f.is_file()
    if existed and not overwrite:
        raise ConflictError(
            f"{safe} 已经存在。要改它的话传 overwrite=true",
            code="already_exists",
        )

    text = build_document(
        name=safe, description=description, body=body, keywords=keywords
    )
    d.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8", newline="\n")

    # 【必须 reload】。索引是进程内单例，不重扫的话新条目要等重启才出现 ——
    # 而调用方刚刚收到"创建成功"，两者矛盾。
    _reload(kind)

    log.info(
        "authored", kind=kind, name=safe, created=not existed, path=str(f)
    )
    return AuthorResult(name=safe, path=f, created=not existed)


def remove(*, kind: str, name: str) -> None:
    """
    删除整个目录。

    ## 为什么删目录而不是只删 SKILL.md

    技能可以带附件（meta.files）。只删 SKILL.md 会留下一堆孤儿文件，
    而目录仍然存在 —— 下次 reload 时它既不是有效技能也不会被清理，
    只会在诊断里留一条"缺 description"的 warning，看起来像坏了。
    """
    import shutil

    if kind != "skill":
        raise BadRequestError(f"kind 只能是 skill，收到 {kind!r}")

    safe = validate_name(name)
    d = _target_dir(kind, safe)
    if not (d / _file_name(kind)).is_file():
        raise NotFoundError(f"{safe} 不存在", code="not_found")

    shutil.rmtree(d)
    _reload(kind)
    log.info("author_removed", kind=kind, name=safe, path=str(d))


def read_source(*, kind: str, name: str) -> tuple[str, str, list[str], str]:
    """
    读回 (description, body, keywords, raw)。

    编辑界面要用：显示当前内容供用户改。返回拆好的字段而不是原文，
    是因为界面上 description 和正文是两个输入框 ——
    让前端自己解 frontmatter 等于把解析逻辑复制一份。
    """
    safe = validate_name(name)
    f = _target_dir(kind, safe) / _file_name(kind)
    if not f.is_file():
        raise NotFoundError(f"{safe} 不存在", code="not_found")
    raw = f.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(raw)
    kw = meta.get("keywords")
    keywords = [str(x) for x in kw] if isinstance(kw, list) else []
    return (
        " ".join(str(meta.get("description") or "").split()),
        body.strip(),
        keywords,
        raw,
    )


def _reload(kind: str) -> None:
    if kind != "skill":
        raise BadRequestError(f"kind 只能是 skill，收到 {kind!r}")
    skill_registry.reload()


def asset_dir(*, kind: str, name: str) -> Path:
    """
    条目所在目录的绝对路径。

    ## 为什么要暴露这个

    模型建完 SKILL.md 之后想加附件，用的是相对路径
    "skills/xxx/references/detail.md"。而 write_file 的相对路径基准是
    【工作区】—— 文件会写到 workspace/skills/xxx/ 去。

    那里本来就可写，所以【不会报错】：写入成功、模型以为搞定了，
    而 reload 之后附件不在 files 白名单里。用户看到的是
    "技能建好了但读不到附件"。

    给出绝对路径，模型才知道该往哪写。
    """
    return _target_dir(kind, validate_name(name))


def list_extra_files(*, kind: str, name: str) -> list[str]:
    """
    除 SKILL.md 之外的文件，POSIX 相对路径。

    给模型看"这个技能现在有哪些附件"，它才知道该加什么、
    以及有没有重名。
    """
    d = _target_dir(kind, validate_name(name))
    if not d.is_dir():
        return []
    main = _file_name(kind)
    out: list[str] = []
    for f in sorted(d.rglob("*")):
        if not f.is_file() or f.name == main:
            continue
        # 跳过垃圾目录 —— 技能目录里放了依赖时不该刷一屏
        if any(
            part in {"node_modules", ".git", "__pycache__", ".venv"}
            for part in f.relative_to(d).parts
        ):
            continue
        out.append(f.relative_to(d).as_posix())
    return out[:50]
