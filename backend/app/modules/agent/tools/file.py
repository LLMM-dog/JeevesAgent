"""
文件工具。

全部经 PathGuard 校验，且【使用 check() 的返回值】而非原始入参 ——
用原始 path 去 open 等于没检查。

工具描述的写法（三条，见 docs/01-architecture/tools.md#工具描述怎么写）：
1. 写"什么时候用"，不只写"是什么"
2. 写清约束
3. 参数描述里给例子
"""

import fnmatch
import re
from pathlib import Path
from typing import Any

import structlog

from app.core.config import settings
from app.modules.agent.pathguard import get_guard
from app.modules.agent.tools.base import ArtifactPayload, ToolContext, ToolResult

log = structlog.get_logger(__name__)

MAX_READ_CHARS = 200_000
MAX_GREP_MATCHES = 200
MAX_LIST_ENTRIES = 500

# 这些目录不该进入搜索结果。.jeeves 是 agent 自己的临时目录 ——
# 不排除的话 agent 会在自己刚写的临时脚本里搜到自己的代码，产生混乱。
SKIP_DIRS = frozenset(
    {
        ".jeeves",
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".idea",
        ".vscode",
    }
)

_BINARY_SNIFF = 8192


def _is_binary(p: Path) -> bool:
    """
    用是否含 NUL 字节判断，不看扩展名（扩展名可伪造，且很多文本文件没扩展名）。
    """
    try:
        with p.open("rb") as f:
            return b"\x00" in f.read(_BINARY_SNIFF)
    except OSError:
        return False


def _rel(p: Path, root: Path) -> str:
    try:
        return str(p.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


def _skip(p: Path) -> bool:
    return any(part in SKIP_DIRS for part in p.parts)


class ReadFileTool:
    name = "read_file"
    description = (
        "读取文本文件内容，返回带行号的文本。"
        "修改任何文件之前必须先用本工具读一遍——不要凭猜测写。"
        "二进制文件会被拒绝。文件很大时可用 offset/limit 分段读。"
    )
    requires_approval = False

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": '文件路径，如 "src/main.py"'},
                "offset": {"type": "integer", "description": "起始行号（1 开始），默认 1"},
                "limit": {"type": "integer", "description": "最多读多少行，默认 2000"},
            },
            "required": ["path"],
        }

    async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
        raw = str(kw.get("path", "")).strip()
        if not raw:
            return ToolResult(content="path 不能为空", is_error=True)
        offset = max(1, int(kw.get("offset") or 1))
        limit = max(1, int(kw.get("limit") or 2000))

        target = Path(raw)
        if not target.is_absolute():
            target = ctx.workspace / target
        # 必须用 check 的返回值
        resolved = get_guard().check(target)

        if not resolved.exists():
            return ToolResult(content=f"文件不存在：{_rel(resolved, ctx.workspace)}", is_error=True)
        if resolved.is_dir():
            return ToolResult(
                content=f"这是一个目录，请用 list_dir：{_rel(resolved, ctx.workspace)}",
                is_error=True,
            )
        if _is_binary(resolved):
            return ToolResult(
                content=f"这是二进制文件，无法作为文本读取：{_rel(resolved, ctx.workspace)}",
                is_error=True,
            )

        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return ToolResult(content=f"读取失败：{e}", is_error=True)

        lines = text.splitlines()
        total = len(lines)
        chunk = lines[offset - 1 : offset - 1 + limit]
        numbered = "\n".join(f"{offset + i}: {line}" for i, line in enumerate(chunk))
        if len(numbered) > MAX_READ_CHARS:
            numbered = numbered[:MAX_READ_CHARS] + "\n…（内容过长已截断，用 offset/limit 分段读）"

        tail = ""
        end = offset - 1 + len(chunk)
        if end < total:
            tail = f"\n\n（共 {total} 行，已显示 {offset}-{end}）"

        return ToolResult(
            content=numbered + tail,
            # 前端只需一张"读取了 xxx.py（120 行）"的卡片，
            # 不需要把文件内容再渲染一遍
            display={
                "path": _rel(resolved, ctx.workspace),
                "total_lines": total,
                "shown": [offset, end],
                "suffix": resolved.suffix.lstrip("."),
            },
        )


class WriteFileTool:
    name = "write_file"
    description = (
        "把内容写入文件（整体覆盖），父目录不存在会自动创建。"
        "修改已有文件应优先用 edit_file 做精确替换——"
        "整体覆盖会丢掉你没读到的部分。本工具主要用于新建文件。"
    )
    requires_approval = True

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": '文件路径，如 "src/new.py"'},
                "content": {"type": "string", "description": "完整文件内容"},
            },
            "required": ["path", "content"],
        }

    async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
        raw = str(kw.get("path", "")).strip()
        content = kw.get("content")
        if not raw:
            return ToolResult(content="path 不能为空", is_error=True)
        if content is None:
            return ToolResult(content="content 不能为空（要清空文件请传空字符串）", is_error=True)

        target = Path(raw)
        if not target.is_absolute():
            target = ctx.workspace / target
        resolved = get_guard().check(target, write=True)

        existed = resolved.exists()
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            # newline="" 避免 Windows 上把 \n 自动转成 \r\n ——
            # 模型生成的代码里混入 CRLF 会让 git diff 显示整文件变更
            resolved.write_text(str(content), encoding="utf-8", newline="")
        except OSError as e:
            return ToolResult(content=f"写入失败：{e}", is_error=True)

        rel = _rel(resolved, ctx.workspace)
        text = str(content)
        lines = text.count("\n") + 1
        return ToolResult(
            content=f"{'已覆盖' if existed else '已创建'} {rel}（{lines} 行）",
            display={"path": rel, "lines": lines, "created": not existed},
            # 写出的完整文件是工作成果：压缩后用户说"把刚才那份代码改一下"
            # 时它必须还在上下文里。
            #
            # 超过一定大小就不当 artifact —— artifact 常驻上下文，
            # 一个几万行的文件会把窗口占满，反而挤掉真正需要的历史。
            artifact=(
                ArtifactPayload(kind="file", content=text, path=rel)
                if len(text) <= settings.agent.artifact_max_chars
                else None
            ),
        )


def _no_match_hint(text: str, old: str, *, context: int = 2) -> str:
    """
    old_string 没匹配上时，给出【最接近的真实内容】。

    ## 为什么值得花这个功夫

    实测：模型连续三次用几乎相同的 old_string 调 edit_file，三次都
    "未找到"，白烧三轮。它记忆里的缩进与文件真实内容差了一点，而错误
    信息只说"没找到，请确认缩进"—— 它无法据此定位差异，只能继续猜。

    贴出真实内容后模型能直接看出差在哪（少个空格、多个空行、换行位置），
    一轮就改对。

    ## 怎么找"最接近"

    用 difflib 按行找相似度最高的窗口。不用编辑距离算全文 —— 那对大文件
    太慢，而且我们要的是"给人看的定位信息"，不需要最优解。
    """
    import difflib

    old_lines = old.splitlines()
    file_lines = text.splitlines()
    if not old_lines or not file_lines:
        return (
            "old_string 在文件中未找到（文件为空或 old_string 为空）。"
            "请先用 read_file 确认当前内容。"
        )

    # 用 old_string 的第一行去找候选位置 —— 它通常是最有辨识度的锚点
    anchor = old_lines[0].strip()
    best_idx = -1
    best_ratio = 0.0
    window = len(old_lines)
    for i in range(len(file_lines)):
        # 先用便宜的判断筛掉明显不相关的行
        if anchor and anchor not in file_lines[i] and not file_lines[i].strip():
            continue
        candidate = "\n".join(file_lines[i : i + window])
        ratio = difflib.SequenceMatcher(None, old, candidate).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_idx = i

    # 阈值 0.5：实测 0.35 太松 —— 完全不相关的 old_string（"class Foo: pass"
    # 对一个函数体）也能拿到 39% 的相似度，贴出来只会误导模型去改错地方。
    # 缩进差异这类真实的近似匹配通常在 80% 以上，0.5 有足够余量。
    if best_idx < 0 or best_ratio < 0.5:
        # 差得太远，贴片段只会误导。给出文件规模让模型自己去读。
        return (
            f"old_string 在文件中未找到，且没有相近的内容"
            f"（文件共 {len(file_lines)} 行）。"
            "请用 read_file 读取当前内容后重新构造 old_string。"
        )

    start = max(0, best_idx - context)
    end = min(len(file_lines), best_idx + window + context)
    numbered = "\n".join(
        f"{i + 1:>5}| {file_lines[i]}" for i in range(start, end)
    )
    return (
        "old_string 在文件中未找到。\n\n"
        f"文件里最接近的一段（第 {start + 1}~{end} 行，相似度 {best_ratio:.0%}）：\n"
        f"{numbered}\n\n"
        "请对照上面的真实内容修正 old_string —— 缩进、空行、换行位置都必须完全一致。"
        "行号前缀（`  12| `）不是文件内容，不要包含进去。"
    )


class EditFileTool:
    name = "edit_file"
    description = (
        "在文件中做精确字符串替换。这是修改已有文件的首选方式。"
        "old_string 必须在文件中【唯一命中】——命中 0 次或多次都会失败并告诉你实际次数。"
        "所以 old_string 要带足够的上下文（前后各多带几行）来保证唯一。"
        "调用前必须先用 read_file 读过该文件。"
    )
    requires_approval = True

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": '文件路径，如 "src/main.py"'},
                "old_string": {"type": "string", "description": "要被替换的原文，必须唯一命中"},
                "new_string": {"type": "string", "description": "替换成的新内容"},
            },
            "required": ["path", "old_string", "new_string"],
        }

    async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
        raw = str(kw.get("path", "")).strip()
        old = kw.get("old_string")
        new = kw.get("new_string")
        if not raw or old is None or new is None:
            return ToolResult(content="path / old_string / new_string 都是必需的", is_error=True)
        if old == new:
            return ToolResult(content="old_string 与 new_string 相同，无需修改", is_error=True)

        target = Path(raw)
        if not target.is_absolute():
            target = ctx.workspace / target
        resolved = get_guard().check(target, write=True)

        if not resolved.exists():
            return ToolResult(content=f"文件不存在：{_rel(resolved, ctx.workspace)}", is_error=True)

        try:
            text = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return ToolResult(content=f"读取失败：{e}", is_error=True)

        count = text.count(str(old))
        if count == 0:
            # 只说"没找到"是不够的 —— 实测模型会连续三次用几乎一样的
            # old_string 重试，每次都失败，白烧三轮。
            #
            # 原因很实际：模型记忆里的缩进/空白与文件真实内容有细微差异，
            # 而错误信息没告诉它差在哪，它只能靠猜。
            #
            # 所以把【最接近的那段真实内容】贴给它。这样它能直接看出是
            # 少了个空格还是换行位置不对，一轮就能改对。
            return ToolResult(
                content=_no_match_hint(text, str(old)),
                is_error=True,
            )
        if count > 1:
            return ToolResult(
                content=(
                    f"old_string 命中 {count} 次，必须唯一。"
                    "请在 old_string 前后多带几行上下文来保证唯一。"
                ),
                is_error=True,
            )

        try:
            resolved.write_text(text.replace(str(old), str(new), 1), encoding="utf-8", newline="")
        except OSError as e:
            return ToolResult(content=f"写入失败：{e}", is_error=True)

        rel = _rel(resolved, ctx.workspace)
        return ToolResult(
            content=f"已修改 {rel}",
            display={
                "path": rel,
                "old_lines": str(old).count("\n") + 1,
                "new_lines": str(new).count("\n") + 1,
                # 前端渲染 diff 用
                "old_string": str(old)[:2000],
                "new_string": str(new)[:2000],
            },
        )


class ListDirTool:
    name = "list_dir"
    description = "列出目录内容。子目录带 / 后缀。不递归——需要递归查找用 glob。"
    requires_approval = False

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": '目录路径，默认工作区根目录。如 "src"'}
            },
        }

    async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
        raw = str(kw.get("path", "") or ".").strip()
        target = Path(raw)
        if not target.is_absolute():
            target = ctx.workspace / target
        resolved = get_guard().check(target)

        if not resolved.exists():
            return ToolResult(content=f"目录不存在：{_rel(resolved, ctx.workspace)}", is_error=True)
        if not resolved.is_dir():
            return ToolResult(content=f"这不是目录：{_rel(resolved, ctx.workspace)}", is_error=True)

        try:
            entries = sorted(
                resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
            )
        except OSError as e:
            return ToolResult(content=f"读取目录失败：{e}", is_error=True)

        lines: list[str] = []
        for p in entries[:MAX_LIST_ENTRIES]:
            if p.name in SKIP_DIRS:
                continue
            if p.is_dir():
                lines.append(f"{p.name}/")
            else:
                try:
                    size = p.stat().st_size
                    lines.append(f"{p.name}  ({size} B)")
                except OSError:
                    lines.append(p.name)

        rel = _rel(resolved, ctx.workspace) or "."
        body = "\n".join(lines) or "（空目录）"
        if len(entries) > MAX_LIST_ENTRIES:
            body += f"\n…（共 {len(entries)} 项，已显示前 {MAX_LIST_ENTRIES} 项）"
        return ToolResult(
            content=f"{rel}:\n{body}",
            display={"path": rel, "count": len(lines)},
        )


class GlobTool:
    name = "glob"
    description = (
        "按 glob 模式递归查找文件，返回相对路径列表。"
        "自动跳过 .git/node_modules/__pycache__/.venv 等目录。"
    )
    requires_approval = False

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": '模式，如 "**/*.py"、"src/**/*.ts"、"*.md"',
                },
                "path": {"type": "string", "description": "搜索起点，默认工作区根目录"},
            },
            "required": ["pattern"],
        }

    async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
        pattern = str(kw.get("pattern", "")).strip()
        if not pattern:
            return ToolResult(content="pattern 不能为空", is_error=True)

        base = Path(str(kw.get("path", "") or "."))
        if not base.is_absolute():
            base = ctx.workspace / base
        root = get_guard().check(base)

        if not root.is_dir():
            return ToolResult(content=f"起点不是目录：{_rel(root, ctx.workspace)}", is_error=True)

        try:
            found = [p for p in root.glob(pattern) if p.is_file() and not _skip(p)]
        except (OSError, ValueError) as e:
            return ToolResult(content=f"模式不合法或搜索失败：{e}", is_error=True)

        rels = sorted(_rel(p, ctx.workspace) for p in found)
        if not rels:
            return ToolResult(
                content=f"没有匹配 {pattern} 的文件", display={"pattern": pattern, "count": 0}
            )
        shown = rels[:MAX_LIST_ENTRIES]
        body = "\n".join(shown)
        if len(rels) > len(shown):
            body += f"\n…（共 {len(rels)} 个，已显示前 {len(shown)} 个）"
        return ToolResult(
            content=body, display={"pattern": pattern, "count": len(rels)}
        )


class GrepTool:
    name = "grep"
    description = (
        "用正则在文件内容中搜索，返回 路径:行号: 内容 的列表。"
        "用 include 限定文件类型可大幅提速，如 include=\"*.py\"。"
    )
    requires_approval = False

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": '正则，如 "def \\\\w+_handler"'},
                "path": {"type": "string", "description": "搜索起点，默认工作区根目录"},
                "include": {
                    "type": "string",
                    "description": '只搜匹配此 glob 的文件，如 "*.py"、"*.{ts,tsx}"',
                },
                "ignore_case": {"type": "boolean", "description": "忽略大小写，默认 false"},
            },
            "required": ["pattern"],
        }

    async def run(self, ctx: ToolContext, **kw: Any) -> ToolResult:
        pattern = str(kw.get("pattern", ""))
        if not pattern:
            return ToolResult(content="pattern 不能为空", is_error=True)
        try:
            flags = re.IGNORECASE if kw.get("ignore_case") else 0
            rx = re.compile(pattern, flags)
        except re.error as e:
            return ToolResult(content=f"正则不合法：{e}", is_error=True)

        base = Path(str(kw.get("path", "") or "."))
        if not base.is_absolute():
            base = ctx.workspace / base
        root = get_guard().check(base)

        include = str(kw.get("include", "") or "").strip()
        # 支持 *.{ts,tsx} 这种花括号写法（fnmatch 本身不支持）
        includes: list[str] = []
        if include:
            m = re.match(r"^(.*)\{([^}]+)\}(.*)$", include)
            if m:
                pre, mid, post = m.groups()
                includes = [f"{pre}{alt.strip()}{post}" for alt in mid.split(",")]
            else:
                includes = [include]

        hits: list[str] = []
        scanned = 0
        truncated = False

        for p in root.rglob("*"):
            if not p.is_file() or _skip(p):
                continue
            if includes and not any(fnmatch.fnmatch(p.name, pat) for pat in includes):
                continue
            if _is_binary(p):
                continue
            scanned += 1
            try:
                with p.open("r", encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, 1):
                        if rx.search(line):
                            hits.append(f"{_rel(p, ctx.workspace)}:{lineno}: {line.rstrip()[:300]}")
                            if len(hits) >= MAX_GREP_MATCHES:
                                truncated = True
                                break
            except OSError:
                continue
            if truncated:
                break

        if not hits:
            return ToolResult(
                content=f"没有匹配 {pattern} 的内容（扫描了 {scanned} 个文件）",
                display={"pattern": pattern, "count": 0, "scanned": scanned},
            )
        body = "\n".join(hits)
        if truncated:
            body += f"\n…（已达 {MAX_GREP_MATCHES} 条上限，缩小范围或用 include 限定文件类型）"
        return ToolResult(
            content=body,
            display={"pattern": pattern, "count": len(hits), "scanned": scanned},
        )
