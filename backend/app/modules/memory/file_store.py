"""
文件形态的记忆存储。

## 为什么所有 I/O 都走 asyncio.to_thread

项目规则：不在 async 函数里调阻塞 I/O（ruff 的 ASYNC 规则组）。
旧实现的 recall.py 直接用 Path.read_text()，那会在事件循环里阻塞 ——
记忆文件不大，但一次列举可能读几十个文件，累积起来足够让 SSE 心跳断掉。

不用 aiofiles：项目已有的依赖里没有它，而 to_thread 对这个量级的
文件读写足够，且不引入新依赖。

## 为什么写入是"临时文件 + 替换"

直接写目标文件时，进程在写一半时被杀会留下一个截断的文件 ——
而截断的 frontmatter 会让这条记忆永久无法解析。
先写 .tmp 再 os.replace（原子操作）保证目标文件要么是旧的完整版，
要么是新的完整版。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import structlog

from app.modules.memory import layout, render
from app.modules.memory.layout import OVERVIEW_FILENAME
from app.modules.memory.models import MemoryItem, MemoryScope
from app.modules.memory.schema import MemoryScopeKind, MemoryTypeSchema

log = structlog.get_logger(__name__)


class FileMemoryStore:
    """实现 MemoryStore Protocol。"""

    # ── 读 ───────────────────────────────────────

    async def read(self, scope: MemoryScope, schema: MemoryTypeSchema, rel_path: str) -> MemoryItem | None:
        path = layout.resolve(scope, schema, "", rel_path)
        return await self._read_path(path)

    async def read_uri(self, uri: str) -> MemoryItem | None:
        path = self._uri_to_path(uri)
        if path is None:
            return None
        return await self._read_path(path)

    async def _read_path(self, path: Path) -> MemoryItem | None:
        raw = await asyncio.to_thread(_read_text, path)
        if raw is None:
            return None

        uri = self._path_to_uri(path)
        try:
            item = render.parse(raw, uri=uri)
        except Exception as e:  # noqa: BLE001
            # 【不抛异常】。一个坏文件不该让整次列举失败 ——
            # 列举 100 个记忆时第 37 个 frontmatter 坏了，应该返回 99 个。
            log.warning("memory_parse_failed", uri=uri, error=str(e))
            return None

        # frontmatter 里的归属字段可能缺失（手写的文件、或早期版本写的）。
        # 从路径反推补上 —— 路径是可靠的，它由 layout 生成。
        if not item.agent_id or not item.session_id:
            kind, agent_id, session_id, peer_id = layout.describe(path)
            item.agent_id = item.agent_id or agent_id
            item.session_id = item.session_id or session_id
            item.peer_agent_id = item.peer_agent_id or peer_id
            if not item.memory_type:
                item.scope = kind
        return item

    async def list_items(self, scope: MemoryScope, schema: MemoryTypeSchema) -> list[MemoryItem]:
        directory = layout.type_dir(scope, schema)

        if schema.single_file:
            # 单文件类型直接定位，不扫目录 —— 它和别的单文件类型共享同一个
            # 目录（scope 根），扫目录会拿到别人的文件。
            item = await self.read(scope, schema, schema.filename_template)
            return [item] if item else []

        paths = await asyncio.to_thread(_list_md_files, directory)
        items: list[MemoryItem] = []
        for path in paths:
            item = await self._read_path(path)
            if item is None:
                continue
            # 按 memory_type 过滤：多文件类型各有自己的子目录，理论上不会混，
            # 但用户手动放错文件时这层过滤能挡住。
            if item.memory_type and item.memory_type != schema.memory_type:
                continue
            items.append(item)
        items.sort(key=lambda it: (-it.updated_at, it.uri))
        return items

    async def iter_all(self) -> list[tuple[str, MemoryItem]]:
        root = layout.memory_root()
        paths = await asyncio.to_thread(_list_md_files, root, recursive=True)
        out: list[tuple[str, MemoryItem]] = []
        for path in paths:
            item = await self._read_path(path)
            if item is not None and item.memory_type:
                out.append((self._path_to_uri(path), item))
        return out

    # ── 写 ───────────────────────────────────────

    async def resolve_path(
        self,
        scope: MemoryScope,
        schema: MemoryTypeSchema,
        fields: dict[str, Any],
        *,
        extract_context: Any = None,
    ) -> str:
        rel_dir, filename = render.render_path(schema, fields, extract_context=extract_context)
        parts = [p for p in (rel_dir, filename) if p]
        return "/".join(parts)

    async def write(
        self,
        scope: MemoryScope,
        schema: MemoryTypeSchema,
        rel_path: str,
        item: MemoryItem,
        *,
        extraction_id: str = "",
        trace_id: str = "",
    ) -> str:
        path = layout.resolve(scope, schema, "", rel_path)
        content = render.serialize(item, source_extraction_id=extraction_id, trace_id=trace_id)
        await asyncio.to_thread(_write_atomic, path, content)
        return self._path_to_uri(path)

    async def write_overview(
        self, scope: MemoryScope, schema: MemoryTypeSchema, rel_dir: str, content: str
    ) -> str:
        directory = layout.type_dir(scope, schema)
        if rel_dir:
            directory = directory.joinpath(*[p for p in rel_dir.split("/") if p])
        path = directory / OVERVIEW_FILENAME

        if not content:
            await asyncio.to_thread(_unlink, path)
            return ""
        await asyncio.to_thread(_write_atomic, path, content + "\n")
        return self._path_to_uri(path)

    async def delete_uri(self, uri: str) -> bool:
        path = self._uri_to_path(uri)
        if path is None:
            return False
        return await asyncio.to_thread(_unlink, path)

    async def next_available_path(
        self, scope: MemoryScope, schema: MemoryTypeSchema, rel_path: str
    ) -> str:
        """
        add_only 类型的目标已存在时，找一个可用的名字。

        ## 为什么改名而不是覆盖或跳过

        add_only 的语义是"只增不改"。覆盖违反它；跳过会静默丢掉这条记忆 ——
        而两个同名事件通常是两件不同的事（LLM 起名撞车），丢掉一件就是丢信息。

        加数字后缀最多试到 _99。真撞到 99 次说明命名策略有问题，
        那时报错比继续加后缀有用。
        """
        base = layout.resolve(scope, schema, "", rel_path)
        if not await asyncio.to_thread(base.exists):
            return rel_path

        stem = rel_path.removesuffix(".md")
        for i in range(2, 100):
            candidate = f"{stem}_{i}.md"
            path = layout.resolve(scope, schema, "", candidate)
            if not await asyncio.to_thread(path.exists):
                return candidate
        raise FileExistsError(f"{schema.memory_type}: {rel_path} 的同名变体已达 99 个，命名策略需要检查")

    # ── 生命周期 ──────────────────────────────────

    async def init_agent(self, agent_id: str, schemas: list[MemoryTypeSchema]) -> list[str]:
        """
        建目录骨架 + 用 init_value 写单文件类型的初值。

        已存在的文件【不覆盖】—— 这个方法可能被重复调用（重启、修复），
        覆盖会抹掉已积累的记忆。
        """
        created: list[str] = []
        scope = MemoryScope(agent_id=agent_id)

        for schema in schemas:
            if schema.scope is MemoryScopeKind.SESSION:
                # 会话目录在会话创建时才建，不预建 —— 预建会留下一堆空目录。
                continue
            if schema.scope is MemoryScopeKind.GLOBAL and schema.single_file:
                # 全局记忆不属于任何智能体，但要保证它存在。
                uri = await self._init_single_file(MemoryScope(), schema)
                if uri:
                    created.append(uri)
                continue

            directory = layout.type_dir(scope, schema)
            await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)

            if schema.single_file:
                uri = await self._init_single_file(scope, schema)
                if uri:
                    created.append(uri)

        return created

    async def _init_single_file(self, scope: MemoryScope, schema: MemoryTypeSchema) -> str:
        path = layout.resolve(scope, schema, "", schema.filename_template)
        if await asyncio.to_thread(path.exists):
            return ""

        fields = {f.name: f.init_value for f in schema.fields if f.init_value is not None}
        if not fields:
            # 没有 init_value 的单文件类型不预建 —— 空文件和不存在是等价的，
            # 而空文件会让"这条记忆存在但是空的"和"还没学到"混在一起。
            return ""

        body = render.render_body(schema, fields)
        item = render.build_item(schema, scope, fields, body, old=None)
        return await self.write(scope, schema, schema.filename_template, item)

    async def drop_agent(self, agent_id: str) -> int:
        return await asyncio.to_thread(_rmtree_count, layout.agent_root(agent_id))

    async def drop_session(self, session_id: str) -> int:
        return await asyncio.to_thread(_rmtree_count, layout.session_root(session_id))

    # ── uri ↔ path ───────────────────────────────

    @staticmethod
    def _path_to_uri(path: Path) -> str:
        """
        绝对路径 → 相对 data/memory/ 的 POSIX 路径。

        uri 用相对路径的理由见 models_db.py 的注释：绝对路径含项目根目录，
        换机器或移动项目后全表失效。
        """
        try:
            return path.resolve().relative_to(layout.memory_root().resolve()).as_posix()
        except (ValueError, OSError):
            return path.as_posix()

    @staticmethod
    def _uri_to_path(uri: str) -> Path | None:
        """
        uri → 绝对路径，并做越界检查。

        uri 可能来自外部（前端传来的删除请求、LLM 输出的引用），
        所以这里必须挡住 `../` 和绝对路径。
        """
        cleaned = (uri or "").strip().replace("\\", "/")
        if not cleaned:
            return None
        parts = [p for p in cleaned.split("/") if p and p not in (".", "..")]
        if not parts:
            return None

        root = layout.memory_root()
        target = root.joinpath(*parts)
        try:
            layout._assert_within(target, root)
        except layout.PathScopeError:
            log.warning("memory_uri_out_of_scope", uri=uri)
            return None
        return target


# ── 阻塞 I/O。全部在 to_thread 里跑 ──────────────────


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        log.warning("memory_read_failed", path=str(path), error=str(e))
        return None


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    # os.replace 是原子的，且在 Windows 上允许目标已存在（os.rename 不允许）。
    os.replace(tmp, path)


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        log.warning("memory_delete_failed", path=str(path), error=str(e))
        return False


def _list_md_files(directory: Path, *, recursive: bool = True) -> list[Path]:
    if not directory.is_dir():
        return []
    pattern = "**/*.md" if recursive else "*.md"
    return sorted(
        p
        for p in directory.glob(pattern)
        # 以 . 开头的是系统文件（.overview.md），不是记忆项。
        if p.is_file() and not p.name.startswith(".")
    )


def _rmtree_count(directory: Path) -> int:
    """删目录并返回删掉的 .md 文件数。目录不存在返回 0。"""
    import shutil

    if not directory.is_dir():
        return 0
    count = sum(1 for _ in directory.rglob("*.md"))
    shutil.rmtree(directory, ignore_errors=True)
    return count
