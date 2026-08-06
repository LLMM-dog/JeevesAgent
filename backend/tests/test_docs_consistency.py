"""
文档与代码的一致性守卫。

## 为什么需要这些测试

文档过时不会有任何报错，而**下一个改这块的人（包括我自己）会照着过时的
文档去改代码**。实际发生过的：

  - `docs/02-data/schema.md` 自称"数据结构的唯一真源"，而 3 张表缺失、
    2 张表列名几乎全错、还有 1 张表代码里根本不存在
  - `docs/01-architecture/tools.md` 列了 4 个不存在的记忆工具
    （memory_list / memory_read / memory_write / memory_delete），
    真实的是 remember / recall / update_memory / forget_memory
  - `docs/01-architecture/context.md` 说"引用内容放在用户文字前面"，
    而代码是拼在后面，且注释里写着相反的理由

这些测试只挡"能自动检查的那部分"：表名、工具名、端点路径。写得对不对
还是要人看，但至少"写的东西存不存在"能被机器管住。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
APP = ROOT / "backend" / "app"


def _all_tables() -> set[str]:
    """从 metadata 拿真实表名。"""
    import app.modules.cron.models  # noqa: F401
    import app.modules.memory.models  # noqa: F401
    import app.modules.provider.models  # noqa: F401
    import app.modules.session.models  # noqa: F401
    import app.modules.skill.models  # noqa: F401
    import app.modules.todo.models  # noqa: F401
    import app.modules.trace.models  # noqa: F401
    from app.infra.db.base import Base

    return set(Base.metadata.tables)


def _schema_docs() -> str:
    a = (DOCS / "02-data" / "schema.md").read_text(encoding="utf-8")
    b = (DOCS / "02-data" / "schema-2.md").read_text(encoding="utf-8")
    return a + "\n" + b


class TestSchemaDocsCoverAllTables:
    def test_every_table_documented(self) -> None:
        docs = _schema_docs()
        missing = [t for t in sorted(_all_tables()) if f"## {t}" not in docs]
        assert not missing, (
            f"这些表没写进 schema 文档：{missing}。"
            "那两份文档自称数据结构的唯一真源"
        )

    def test_no_phantom_tables(self) -> None:
        """
        反向：文档里的 `## xxx` 章节如果长得像表名，就必须真实存在。

        删表而忘了删文档，下一个人会照着写查询然后发现表不存在。
        """
        docs = _schema_docs()
        real = _all_tables()
        # 只看带 CREATE TABLE 的章节 —— 那些明确是在描述表
        phantom = []
        for m in re.finditer(r"^## ([a-z_]+)\s*$", docs, re.M):
            name = m.group(1)
            tail = docs[m.end() : m.end() + 400]
            if "CREATE TABLE" in tail and name not in real:
                phantom.append(name)
        assert not phantom, f"文档描述了不存在的表：{phantom}"


class TestToolDocsMatchRegistry:
    def _registered(self) -> set[str]:
        from app.main import build_registry

        reg = build_registry()
        return {
            str((s.get("function") or {}).get("name", "")) for s in reg.to_specs()
        }

    def test_every_tool_documented(self) -> None:
        doc = (DOCS / "01-architecture" / "tools.md").read_text(encoding="utf-8")
        missing = [t for t in sorted(self._registered()) if f"`{t}`" not in doc]
        assert not missing, f"这些工具没写进 tools.md：{missing}"

    def test_no_phantom_tools(self) -> None:
        """
        文档的工具表里不能有不存在的工具。

        实际踩过：tools.md 列了 memory_list / memory_read / memory_write /
        memory_delete 四个不存在的工具，而真实的记忆工具叫
        remember / recall / update_memory / forget_memory。
        照着文档去调的人会得到"工具不存在"。
        """
        doc = (DOCS / "01-architecture" / "tools.md").read_text(encoding="utf-8")
        real = self._registered()
        # web_search 只在配了后端时注册，文档里写了但这里可能拿不到
        allow = real | {"web_search"}
        # 只扫工具表的行：| `xxx` | 是/否 | ...
        listed = set(re.findall(r"^\|\s*`([a-z_]+)`\s*\|\s*(?:是|否|\*\*是\*\*)", doc, re.M))
        phantom = listed - allow
        assert not phantom, f"tools.md 里有不存在的工具：{sorted(phantom)}"


class TestReadmeNumbersAreCurrent:
    """
    README 里的数字会过时，而它是给用户看的第一份材料。

    不检查具体数值（那要求每加一个测试就改 README），只检查
    "别差太远" —— 差一个数量级说明那句话是很久以前写的。
    """

    def test_tool_count_claim(self) -> None:
        from app.main import build_registry

        real = len(build_registry().to_specs())
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        m = re.search(r"(\d+)\s*个内置工具", readme)
        assert m, "README 里找不到工具数量的说法"
        claimed = int(m.group(1))
        assert abs(claimed - real) <= 1, (
            f"README 说 {claimed} 个内置工具，实际注册 {real} 个"
        )

    def test_no_stale_stack_mentions(self) -> None:
        """
        项目从 Java 迁到 Python 过。旧技术栈的字样不该出现在任何文档里 ——
        用户看到会以为要装 JDK。
        """
        bad = ["Spring Boot", "spring-boot", "Maven", "JDK", "ragent"]
        hits = []
        for f in [ROOT / "README.md", *DOCS.rglob("*.md")]:
            text = f.read_text(encoding="utf-8")
            for token in bad:
                if token in text:
                    hits.append(f"{f.relative_to(ROOT).as_posix()}: {token}")
        assert not hits, f"文档里还有旧技术栈的字样：{hits}"


class TestDocLinksResolve:
    """
    文档里的相对链接指向的文件必须存在。

    死链不会报错，只会让人点过去看到 404 —— 而写文档的人当时是有意
    引导他去看那一篇的。
    """

    def test_internal_links_exist(self) -> None:
        broken: list[str] = []
        for f in [ROOT / "README.md", *DOCS.rglob("*.md")]:
            text = f.read_text(encoding="utf-8")
            for m in re.finditer(r"\[[^\]]+\]\(([^)#\s]+\.md)(?:#[^)]*)?\)", text):
                target = (f.parent / m.group(1)).resolve()
                if not target.is_file():
                    broken.append(
                        f"{f.relative_to(ROOT).as_posix()} -> {m.group(1)}"
                    )
        assert not broken, f"死链：{broken}"
