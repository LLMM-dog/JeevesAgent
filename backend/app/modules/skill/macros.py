"""
宏：技能的轻量派生。

## 与技能的区别

显式区分 Skill 与 Macro 的实现不多，常见判据
（`macros/macro-creator/MACRO.md:28-32`）很实用：

| | 技能 | 宏 |
| --- | --- | --- |
| 内容 | 知识 + 流程 + 脚本 + 参考文件 | **纯流程描述**，单文件 |
| 触发 | 模型自主判断 | **用户输入 `!` 显式触发** |
| 是否常驻 L1 | 是 | **否** |
| 适用 | 通用能力 | 个人私有工作流 |

不过 区分**只停留在文档**：它的 `_scan_macros` 和
`_scan_anthropic_skills` 是复制粘贴，`type: macro` 字段解析器根本不读，
运行时两者毫无差异 —— 宏照样占常驻上下文位。

本项目让这个区分**在运行时真的成立**：宏不进系统提示词。

## 为什么宏不占常驻位

宏是用户主动触发的，模型不需要"知道它存在"。

而常驻位很贵：实测六个技能的 L1 合计 2156 字符。宏的数量通常比技能多
（个人工作流会攒到几十个），全塞进系统提示词就是纯浪费 —— 用户按 `!`
的时候自己会选。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import structlog

from app.core.config import settings
from app.modules.skill.loader import Diagnostic, parse_frontmatter

log = structlog.get_logger(__name__)

MACRO_FILE = "MACRO.md"


@dataclass
class MacroMeta:
    name: str
    description: str
    path: Path
    keywords: list[str] = field(default_factory=list)


@dataclass
class MacroIndex:
    macros: dict[str, MacroMeta] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def names(self) -> list[str]:
        return sorted(self.macros)

    def get(self, name: str) -> MacroMeta | None:
        return self.macros.get(name)


def _one_line(text: object) -> str:
    return " ".join(str(text).split())


def load_macros(root: Path | None = None) -> MacroIndex:
    """
    扫 `macros/*/MACRO.md`。只看一层子目录 —— 宏是单文件的，不需要递归。

    和技能加载一样：每份文件独立降级，一个坏宏不影响其它宏。
    """
    base = root or settings.macros_dir
    index = MacroIndex()
    if not base.is_dir():
        return index

    for d in sorted(base.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        f = d / MACRO_FILE
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            index.diagnostics.append(Diagnostic("warning", f"读取失败：{e}", str(f)))
            continue

        meta, _body = parse_frontmatter(text)
        desc = meta.get("description")
        if not desc or not str(desc).strip():
            index.diagnostics.append(
                Diagnostic("warning", "缺 description，跳过", str(f))
            )
            continue

        name = _one_line(meta.get("name") or d.name)
        if name in index.macros:
            index.diagnostics.append(
                Diagnostic("collision", f"宏名 {name} 重复，忽略此项", str(f))
            )
            continue

        kw = meta.get("keywords")
        index.macros[name] = MacroMeta(
            name=name,
            description=_one_line(desc),
            path=f,
            keywords=[str(k) for k in kw] if isinstance(kw, list) else [],
        )

    log.info("macros_loaded", count=len(index.macros), root=str(base))
    for d_ in index.diagnostics:
        log.warning("macro_diagnostic", msg=d_.message, path=d_.path)
    return index


def read_macro_body(meta: MacroMeta) -> str:
    """读宏正文。`${MACRO_DIR}` 会被替换成宏所在目录。"""
    text = meta.path.read_text(encoding="utf-8")
    _fm, body = parse_frontmatter(text)
    d = str(meta.path.parent)
    return body.replace("${MACRO_DIR}", d).replace("$MACRO_DIR", d)


_index: MacroIndex | None = None


def get_index() -> MacroIndex:
    global _index
    if _index is None:
        _index = load_macros()
    return _index


def reload() -> MacroIndex:
    global _index
    _index = load_macros()
    return _index


def reset() -> None:
    global _index
    _index = None
