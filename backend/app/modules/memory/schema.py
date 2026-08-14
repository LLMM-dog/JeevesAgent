"""
记忆类型的 Schema 定义。

一个 YAML 文件驱动四件事：发给 LLM 的输出契约、文件路径、字段合并策略、
最终文件内容。本模块只负责【解析与校验】，不碰文件系统，也不渲染模板
（渲染在 render.py，路径换算在 layout.py）。

与 OpenViking 的差异：新增 scope 枚举替代它的 `{{ user_space }}` 占位符。
理由见 docs/architecture/memory-schema.md#scope-是新增的不是-openviking-的字段。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# filename_template / directory 里允许引用、但不需要在 fields 里声明的变量。
# extract_context 是提取阶段注入的辅助对象（提供 get_year(ranges) 这类方法）。
TEMPLATE_BUILTINS = frozenset({"extract_context", "version", "created_at", "updated_at", "memory_type"})

# 匹配 {{ var }} 与 {{ var.method(...) }}，只取最外层的第一个标识符。
_TEMPLATE_VAR_RE = re.compile(r"\{\{-?\s*([a-zA-Z_]\w*)")


class MemoryScopeKind(str, Enum):
    """
    记忆的作用域。决定文件落在哪一层根目录下。

    这是整个记忆系统最重要的一个维度 —— 隔离规则、越界检查、召回范围
    全部从它推导。见 docs/architecture/memory.md#三个层次的隔离。
    """

    GLOBAL = "global"    # 所有智能体共享。目前只有 profile
    AGENT = "agent"      # 单个智能体的全部会话
    SESSION = "session"  # 单个会话内，且限定该智能体


class FieldType(str, Enum):
    STRING = "string"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"


class MergeOp(str, Enum):
    """
    字段合并策略。

    PATCH 对字符串是【SEARCH/REPLACE 块】，不是追加 —— 这是最容易做错的一处，
    见 docs/architecture/memory.md#字段合并patch-是-searchreplace不是追加。
    """

    IMMUTABLE = "immutable"  # 首次写入后永不改
    REPLACE = "replace"      # 全量覆盖，空值不覆盖
    PATCH = "patch"          # 字符串走 SEARCH/REPLACE；非字符串等同 replace
    SUM = "sum"              # 数值累加


class OperationMode(str, Enum):
    UPSERT = "upsert"            # 已存在则合并
    ADD_ONLY = "add_only"        # 只新建，已存在则改名
    UPDATE_ONLY = "update_only"  # 只更新，不存在则跳过


@dataclass(frozen=True)
class MemoryField:
    name: str
    description: str
    type: FieldType = FieldType.STRING
    # 默认 replace 而非 OpenViking 的 patch：patch 要求 LLM 输出 SEARCH/REPLACE
    # 结构，对一个短标量字段（goal / outcome）是过重的契约。忘写 merge_op 时
    # 静默变成 patch 会让模型困惑，而 replace 是安全的默认。
    merge_op: MergeOp = MergeOp.REPLACE
    # 初始值。只对单文件类型有效，init_agent() 建骨架时用。
    init_value: Any = None
    # true 时不出现在 LLM 的输出契约里，由代码填充。
    # 典型是 events.chat_log —— 让 LLM 填它会导致"凭记忆重写对话"，
    # 而重写过的对话不再是证据。
    system: bool = False

    @property
    def in_llm_contract(self) -> bool:
        return not self.system


@dataclass(frozen=True)
class MemoryTypeSchema:
    """一种记忆类型的完整定义。不可变 —— 注册表加载后不允许运行时改。"""

    memory_type: str
    scope: MemoryScopeKind
    description: str
    directory: str
    filename_template: str
    fields: tuple[MemoryField, ...] = ()
    content_template: str = ""
    enabled: bool = True
    operation_mode: OperationMode = OperationMode.UPSERT
    # 是否在 peer 目录（agents/A/peers/B/）下也存一份。
    # scope=session 的类型应设 false：不存在"A 眼中 B 的会话事件"。
    peer_enabled: bool = True
    embedding_template: str = ""
    overview_template: str = ""
    # 来源。"builtin" 或用户 YAML 的路径，供设置页显示与排错。
    source: str = "builtin"

    @property
    def single_file(self) -> bool:
        """
        单文件类型（profile.md / soul.md）vs 多文件类型（preferences/<topic>.md）。

        由 filename_template 是否含变量推导，不让用户手写 —— 两处声明同一件事
        必然会不一致。
        """
        return "{{" not in self.filename_template

    @property
    def field_names(self) -> frozenset[str]:
        return frozenset(f.name for f in self.fields)

    def get_field(self, name: str) -> MemoryField | None:
        for f in self.fields:
            if f.name == name:
                return f
        return None

    def llm_fields(self) -> tuple[MemoryField, ...]:
        """出现在 LLM 输出契约里的字段。"""
        return tuple(f for f in self.fields if f.in_llm_contract)


class SchemaError(ValueError):
    """YAML 定义不合法。加载时抛出，由 registry 转成 diagnostic。"""


def template_variables(template: str) -> set[str]:
    """
    抽出模板里引用的变量名。

    只取最外层标识符：`{{ extract_context.get_year(ranges) }}` 返回
    {"extract_context"}，不返回 ranges —— 那是方法参数，由渲染期的
    上下文提供，不需要在 fields 里声明。
    """
    return set(_TEMPLATE_VAR_RE.findall(template or ""))


def parse_schema(raw: Any, *, source: str = "builtin") -> MemoryTypeSchema:
    """
    从 YAML 解析出的 dict 构造 schema。不合法时抛 SchemaError。

    所有校验都在这里做而不是延后到渲染期：渲染期报错时 Jinja 的
    错误信息指向模板内部，看不出是哪个 YAML 写错了。
    """
    if not isinstance(raw, dict):
        raise SchemaError("顶层必须是映射（mapping）")

    memory_type = str(raw.get("memory_type") or "").strip()
    if not memory_type:
        raise SchemaError("缺少 memory_type")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", memory_type):
        raise SchemaError(f"memory_type 必须是小写下划线标识符：{memory_type!r}")

    scope = _parse_enum(MemoryScopeKind, raw.get("scope"), "scope", memory_type)
    operation_mode = _parse_enum(
        OperationMode, raw.get("operation_mode", "upsert"), "operation_mode", memory_type
    )

    directory = str(raw.get("directory") or "").strip().strip("/")
    filename_template = str(raw.get("filename_template") or "").strip()
    if not filename_template:
        raise SchemaError("缺少 filename_template")
    if filename_template.startswith("/"):
        raise SchemaError("filename_template 不能以 / 开头（它是相对 directory 的）")

    fields = _parse_fields(raw.get("fields"), memory_type)
    schema = MemoryTypeSchema(
        memory_type=memory_type,
        scope=scope,
        description=str(raw.get("description") or "").strip(),
        directory=directory,
        filename_template=filename_template,
        fields=fields,
        content_template=str(raw.get("content_template") or ""),
        enabled=bool(raw.get("enabled", True)),
        operation_mode=operation_mode,
        peer_enabled=bool(raw.get("peer_enabled", True)),
        embedding_template=str(raw.get("embedding_template") or ""),
        overview_template=str(raw.get("overview_template") or ""),
        source=source,
    )
    _validate_path_templates(schema)
    return schema


def _parse_enum(enum_cls: Any, value: Any, key: str, memory_type: str) -> Any:
    if value is None:
        raise SchemaError(f"{memory_type}: 缺少 {key}")
    try:
        return enum_cls(str(value).strip())
    except ValueError:
        allowed = "/".join(m.value for m in enum_cls)
        raise SchemaError(f"{memory_type}: {key} 只能是 {allowed}，实际是 {value!r}") from None


def _parse_fields(raw_fields: Any, memory_type: str) -> tuple[MemoryField, ...]:
    if not isinstance(raw_fields, list) or not raw_fields:
        raise SchemaError(f"{memory_type}: fields 必须是非空列表")

    out: list[MemoryField] = []
    seen: set[str] = set()
    for i, item in enumerate(raw_fields):
        if not isinstance(item, dict):
            raise SchemaError(f"{memory_type}: fields[{i}] 必须是映射")
        name = str(item.get("name") or "").strip()
        if not name:
            raise SchemaError(f"{memory_type}: fields[{i}] 缺少 name")
        if name in seen:
            raise SchemaError(f"{memory_type}: 字段名重复：{name}")
        seen.add(name)

        description = str(item.get("description") or "").strip()
        is_system = bool(item.get("system", False))
        # description 是发给 LLM 的提示词。system 字段不发给 LLM，可以没有。
        if not description and not is_system:
            raise SchemaError(f"{memory_type}.{name}: 缺少 description（它是给 LLM 的提示词）")

        out.append(
            MemoryField(
                name=name,
                description=description,
                type=_parse_enum(FieldType, item.get("type", "string"), f"{name}.type", memory_type),
                merge_op=_parse_enum(
                    MergeOp, item.get("merge_op", "replace"), f"{name}.merge_op", memory_type
                ),
                init_value=item.get("init_value"),
                system=is_system,
            )
        )
    return tuple(out)


def _validate_path_templates(schema: MemoryTypeSchema) -> None:
    """
    路径模板里引用的变量必须在 fields 里声明。

    对应 OpenViking 的 validate_uri_template（utils/uri.py:87）。不校验的后果是
    渲染时才炸 —— 而那时错误指向 Jinja 内部，看不出是哪个 YAML 写错了。
    """
    known = schema.field_names | TEMPLATE_BUILTINS
    for label, template in (("directory", schema.directory), ("filename_template", schema.filename_template)):
        unknown = template_variables(template) - known
        if unknown:
            raise SchemaError(
                f"{schema.memory_type}: {label} 引用了未声明的变量 {sorted(unknown)}；"
                f"已声明的字段：{sorted(schema.field_names)}"
            )

    # session 域 + peer_enabled 是没有意义的组合：peer 是"A 眼中的 B"，
    # 而会话是"我和用户的这次对话"，不存在"A 眼中 B 的会话事件"。
    # 不报错只警告 —— 用户可能有我没想到的用法，但要在 diagnostics 里可见。
    if schema.scope is MemoryScopeKind.SESSION and schema.peer_enabled:
        raise SchemaError(
            f"{schema.memory_type}: scope=session 必须显式设 peer_enabled: false"
            "（不存在「A 眼中 B 的会话事件」）"
        )


@dataclass
class Diagnostic:
    """加载期的问题。一个坏 YAML 不该影响其它，所以不抛异常而是记下来。"""

    level: str  # "warning" | "error"
    message: str
    source: str = ""


@dataclass
class SchemaSet:
    """一批已加载的 schema。registry 的返回值。"""

    schemas: dict[str, MemoryTypeSchema] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def get(self, memory_type: str) -> MemoryTypeSchema | None:
        return self.schemas.get(memory_type)

    def enabled(self) -> list[MemoryTypeSchema]:
        return [s for s in self.schemas.values() if s.enabled]

    def by_scope(self, scope: MemoryScopeKind) -> list[MemoryTypeSchema]:
        return [s for s in self.enabled() if s.scope is scope]

    def names(self) -> list[str]:
        return sorted(self.schemas)
