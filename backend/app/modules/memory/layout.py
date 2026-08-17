"""
路径换算。scope + schema + 字段值 → 磁盘路径。

## 为什么单独一个模块

旧实现把路径拼接散在 __init__.py / recall.py / commit.py / extract_loop.py
四处（`settings.memory_dir / agent_id / ...`）。后果是越界检查【无处安放】——
每处都要自己记得检查，漏一处就等于没有。

现在所有 memory 路径只在这里产生。越界检查在 resolve() 一处生效，
其它模块拿到的 Path 一定已经过检查。

## 目录形状

    data/memory/
    ├── global/profile.md
    ├── agents/<agent_id>/
    │   ├── soul.md
    │   ├── preferences/<topic>.md
    │   └── peers/<peer_agent_id>/identity.md
    ├── sessions/<session_id>/events/<Y>/<M>/<D>/<name>.md   ← 会话记忆，与 agents 平级
    └── .index/
"""

from __future__ import annotations

import re
from pathlib import Path

from app.core.config import settings
from app.modules.memory.models import MemoryScope
from app.modules.memory.schema import MemoryScopeKind, MemoryTypeSchema

# 目录索引文件名。以 . 开头，因此列举记忆项时天然被排除。
OVERVIEW_FILENAME = ".overview.md"

# 索引缓存目录名。
INDEX_DIRNAME = ".index"

# 记忆变更痕迹目录名。每次提取一个 JSON。
#
# 放在【智能体目录下】而不是全局：痕迹是"这个智能体的记忆怎么变的"，
# 跟着它一起被 drop_agent 删掉是对的。放全局会留下指向已删记忆的孤儿痕迹。
TRACE_DIRNAME = ".trace"

# 文件名里禁止的字符。
#
# 【不只是 Windows 的限制】。LLM 生成的 event_name 里出现过 `/`（想表达层级）、
# `:`（想写时间）、`?`（不确定）。不清理的话：`/` 会凭空创建目录，
# 冒号在 Windows 上直接写入失败，而失败信息是 OSError 22，
# 完全看不出是文件名的问题。
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# 保留的 Windows 设备名。用这些名字建文件在 Windows 上会静默失败。
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
)

# 单段路径的长度上限。
#
# 取 80 而非文件系统上限（255）：记忆的路径可能有 6 层
# （sessions/<id>/events/2026/08/13/<name>.md），
# Windows 的 260 字符总长限制会先撞上。80 × 3 层 + 前缀仍在安全区。
MAX_SEGMENT_LEN = 80


class PathScopeError(PermissionError):
    """越界访问。scope 无权访问目标路径。"""


def memory_root() -> Path:
    return settings.memory_dir


def index_dir() -> Path:
    return memory_root() / INDEX_DIRNAME


def global_root() -> Path:
    return memory_root() / "global"


def agent_root(agent_id: str) -> Path:
    return memory_root() / "agents" / _safe_segment(agent_id, label="agent_id")


def peer_root(agent_id: str, peer_agent_id: str) -> Path:
    return agent_root(agent_id) / "peers" / _safe_segment(peer_agent_id, label="peer_agent_id")


def session_root(session_id: str) -> Path:
    # 会话记忆与智能体记忆【平级】，不嵌在 agents/ 下面。
    #
    # 会话记忆（events、entities）是"这次会话发生了什么"，属于会话本身，
    # 只被第一个智能体代表会话修改（见 registry 的 is_first 说明），
    # 不按智能体隔离。放 agents/<id>/sessions/ 会让它看起来"属于某个智能体"，
    # 且删除会话时要遍历 agent 才能定位 —— 平级后按 session_id 一步定位。
    return memory_root() / "sessions" / _safe_segment(session_id, label="session_id")


def trace_dir(scope: MemoryScope) -> Path:
    """
    记忆变更痕迹的目录。

    有 agent_id 时放智能体目录下（跟着它一起被删）；没有时放全局 ——
    那是只改了 profile 的场景，痕迹不属于任何智能体。
    """
    if scope.agent_id:
        return agent_root(scope.agent_id) / TRACE_DIRNAME
    return memory_root() / TRACE_DIRNAME


def scope_root(scope: MemoryScope, kind: MemoryScopeKind, *, peer_enabled: bool = True) -> Path:
    """
    某个 scope 下、某种作用域的根目录。

    peer_enabled=False 的类型即使在 peer 视角下也写回 agent 自己的目录 ——
    典型是 soul（自述，不存在"A 眼中 B 的自述"）。
    """
    if not scope.allows(kind):
        raise PathScopeError(
            f"scope(agent_id={scope.agent_id!r}, session_id={scope.session_id!r}) 无权访问 {kind.value} 域记忆"
        )

    if kind is MemoryScopeKind.GLOBAL:
        # 全局记忆不分视角 —— peer 视角下读到的用户画像和自己视角下是同一份。
        return global_root()

    if kind is MemoryScopeKind.AGENT:
        if scope.is_peer_view and peer_enabled:
            return peer_root(scope.agent_id, scope.peer_agent_id)
        return agent_root(scope.agent_id)

    # session 域不进 peer 目录（schema 层已强制 peer_enabled=false）。
    return session_root(scope.session_id)


def resolve(
    scope: MemoryScope,
    schema: MemoryTypeSchema,
    rendered_dir: str,
    rendered_filename: str,
) -> Path:
    """
    渲染后的相对路径 → 绝对路径，并做越界检查。

    rendered_dir / rendered_filename 来自 render.py（模板已代入字段值）。
    filename 可以带 `/` —— events 的 `<Y>/<M>/<D>/<name>.md` 就靠这个分层，
    这是 OpenViking 的做法（events.yaml:90），比在 directory 里塞变量更清楚：
    directory 是"这类记忆放哪"，filename 是"这一条放哪"。
    """
    root = scope_root(scope, schema.scope, peer_enabled=schema.peer_enabled)

    parts = [*_split_clean(rendered_dir), *_split_clean(rendered_filename)]
    if not parts:
        raise ValueError(f"{schema.memory_type}: 渲染后的路径为空")

    safe = [_safe_segment(p, label=schema.memory_type) for p in parts]
    if not safe[-1].endswith(".md"):
        safe[-1] += ".md"

    target = root.joinpath(*safe)
    _assert_within(target, root)
    return target


def type_dir(scope: MemoryScope, schema: MemoryTypeSchema) -> Path:
    """
    某个记忆类型的目录。列举与生成 .overview.md 用。

    单文件类型（profile.md）返回它所在的目录，因此列举会拿到同目录下
    其它单文件类型的文件 —— 调用方需要按 memory_type 过滤，file_store 已经做了。
    """
    root = scope_root(scope, schema.scope, peer_enabled=schema.peer_enabled)
    parts = _split_clean(schema.directory)
    if not parts:
        return root
    target = root.joinpath(*(_safe_segment(p, label=schema.memory_type) for p in parts))
    _assert_within(target, root)
    return target


def describe(path: Path) -> tuple[MemoryScopeKind, str, str, str]:
    """
    从绝对路径反推 (scope_kind, agent_id, session_id, peer_agent_id)。

    索引重建时用 —— 那时只有一堆文件路径，没有原始的 MemoryScope。
    无法识别时返回 (GLOBAL, "", "", "")，由调用方按"坏文件跳过"处理。
    """
    try:
        rel = path.resolve().relative_to(memory_root().resolve())
    except (ValueError, OSError):
        return MemoryScopeKind.GLOBAL, "", "", ""

    parts = rel.parts
    if not parts:
        return MemoryScopeKind.GLOBAL, "", "", ""

    if parts[0] == "global":
        return MemoryScopeKind.GLOBAL, "", "", ""

    # 会话记忆：sessions/<session_id>/...，与 agents 平级
    if parts[0] == "sessions" and len(parts) >= 2:
        return MemoryScopeKind.SESSION, "", parts[1], ""

    if parts[0] != "agents" or len(parts) < 2:
        return MemoryScopeKind.GLOBAL, "", "", ""

    agent_id = parts[1]
    rest = parts[2:]

    if len(rest) >= 2 and rest[0] == "peers":
        return MemoryScopeKind.AGENT, agent_id, "", rest[1]

    return MemoryScopeKind.AGENT, agent_id, "", ""


def _split_clean(raw: str) -> list[str]:
    return [seg for seg in (raw or "").replace("\\", "/").split("/") if seg and seg not in (".", "..")]


def _safe_segment(raw: str, *, label: str = "") -> str:
    """
    清理单段路径。

    ## 为什么不直接拒绝

    这些值来自 LLM。拒绝一条记忆的代价是那条信息永久丢失，而清理的代价
    只是文件名不完全等于 LLM 想写的名字。清理更划算。

    唯一的例外是清理后为空 —— 那时没有可用的名字，只能报错。
    """
    seg = _UNSAFE_CHARS.sub("_", (raw or "").strip())
    # 结尾的点和空格在 Windows 上会被静默去掉，导致"写入的路径"和
    # "实际的路径"不一致 —— 之后按原名读就找不到了。
    seg = seg.rstrip(". ")

    if len(seg) > MAX_SEGMENT_LEN:
        stem, _, ext = seg.rpartition(".")
        if stem and ext and len(ext) <= 8:
            keep = MAX_SEGMENT_LEN - len(ext) - 1
            seg = f"{stem[:keep]}.{ext}"
        else:
            seg = seg[:MAX_SEGMENT_LEN]
        seg = seg.rstrip(". ")

    if seg.split(".")[0].lower() in _RESERVED_NAMES:
        seg = f"_{seg}"

    if not seg:
        raise ValueError(f"{label or '路径'}: 清理后为空（原值 {raw!r}）")
    return seg


def _assert_within(target: Path, root: Path) -> None:
    """
    确认 target 在 root 内。

    ## 为什么在 _safe_segment 之外还要这一层

    _safe_segment 挡掉了 `..` 和 `/`，理论上不可能越界。但这是最后一道
    防线，而它的成本是一次 resolve()。记忆路径不在热路径上，值得。

    resolve() 而非 is_relative_to 直接比：符号链接能让一个看起来在 root 内
    的路径实际指向外面。
    """
    try:
        resolved = target.resolve()
        root_resolved = root.resolve()
    except OSError as e:
        raise PathScopeError(f"路径无法解析：{target}（{e}）") from e

    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise PathScopeError(f"路径越界：{target} 不在 {root} 内")
