"""
记忆类型注册表。

加载顺序：内置（包内 schemas/）→ 用户覆盖（config/memory/）。同名整体覆盖。

## 为什么同名整体覆盖而不是字段级合并

字段级合并会让"最终生效的定义是什么"难以推断 —— 用户改了 profile 的一个字段
描述，而内置版本之后加了新字段，合并结果是两边都没预期的第三种形状。
整体覆盖时用户拿到的就是他写的那份，可预测。

代价是想改一个字段得复制整个文件。可以接受 —— schema 文件不大，
而且复制出来的那份就成了用户自己的东西，之后升级不会被冲掉。

## 与 skill / agent spec 的一致性

`agents/*.md` 覆盖 BUILTIN_SPECS、`skills/` 扫描目录，都是"内置 + 用户目录"
这个模式。记忆类型用同一套，用户不需要学新规则。
"""

from __future__ import annotations

from pathlib import Path

import structlog
import yaml

from app.core.config import settings
from app.modules.memory.schema import (
    Diagnostic,
    MemoryTypeSchema,
    SchemaError,
    SchemaSet,
    parse_schema,
)

log = structlog.get_logger(__name__)

BUILTIN_SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"

_cache: SchemaSet | None = None


def get_schemas() -> SchemaSet:
    """
    取当前注册表。首次调用时懒加载。

    懒加载而非要求启动时初始化：测试、脚本、子智能体都可能在没走 startup 的
    情况下用到记忆类型，那时应该能正常工作而不是拿到空表。
    与 skill/registry.py 的 get_index() 同一个思路。
    """
    global _cache
    if _cache is None:
        _cache = load_schemas()
    return _cache


def reload() -> SchemaSet:
    """重扫两个目录。用户改了 config/memory/*.yaml 后调用，不需要重启。"""
    global _cache
    _cache = load_schemas()
    return _cache


def reset() -> None:
    """测试用：清缓存。"""
    global _cache
    _cache = None


def set_schemas(schema_set: SchemaSet) -> None:
    """测试用：直接注入。"""
    global _cache
    _cache = schema_set


def user_schemas_dir() -> Path:
    return settings.config_dir / "memory"


def load_schemas(
    *, builtin_dir: Path | None = None, user_dir: Path | None = None
) -> SchemaSet:
    builtin_dir = builtin_dir if builtin_dir is not None else BUILTIN_SCHEMAS_DIR
    user_dir = user_dir if user_dir is not None else user_schemas_dir()

    out = SchemaSet()
    builtin_count = _load_dir(builtin_dir, out, source_label="builtin")

    # 内置一个都没加载成功 = 包装坏了。
    #
    # 【必须报错而不是静默跑起来】：空注册表下所有记忆写入都会以
    # "未知记忆类型"失败，而那个错误信息完全不指向"schema 没加载"。
    # OpenViking 同样在这里 raise（memory_type_registry.py:55）。
    if builtin_count == 0:
        detail = "; ".join(d.message for d in out.diagnostics) or f"目录不存在或为空：{builtin_dir}"
        raise RuntimeError(f"内置记忆类型加载失败，记忆系统不可用。{detail}")

    user_count = _load_dir(user_dir, out, source_label="user", replace=True)

    log.info(
        "memory_schemas_loaded",
        builtin=builtin_count,
        user=user_count,
        total=len(out.schemas),
        enabled=len(out.enabled()),
        diagnostics=len(out.diagnostics),
    )
    for diag in out.diagnostics:
        log.warning("memory_schema_diagnostic", level=diag.level, source=diag.source, message=diag.message)
    return out


def _load_dir(directory: Path, out: SchemaSet, *, source_label: str, replace: bool = False) -> int:
    if not directory.is_dir():
        # 用户目录不存在是常态（大多数人不自定义），不记 diagnostic。
        if source_label == "builtin":
            out.diagnostics.append(
                Diagnostic(level="error", message=f"内置 schema 目录不存在：{directory}", source=str(directory))
            )
        return 0

    count = 0
    # 排序保证加载顺序稳定 —— 否则同一份配置在不同机器上可能产出
    # 不同的 diagnostics 顺序，排错时对不上。
    for path in sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")]):
        schema = _load_one(path, out, source_label=source_label)
        if schema is None:
            continue

        if schema.memory_type in out.schemas and not replace:
            out.diagnostics.append(
                Diagnostic(
                    level="error",
                    message=f"记忆类型重名：{schema.memory_type}（已由 {out.schemas[schema.memory_type].source} 定义）",
                    source=str(path),
                )
            )
            continue

        if schema.memory_type in out.schemas:
            log.info("memory_schema_overridden", memory_type=schema.memory_type, by=str(path))
        out.schemas[schema.memory_type] = schema
        count += 1
    return count


def _load_one(path: Path, out: SchemaSet, *, source_label: str) -> MemoryTypeSchema | None:
    """
    加载单个文件。失败记 diagnostic 返回 None —— 一个坏 YAML 不该影响其它。
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        out.diagnostics.append(Diagnostic(level="error", message=f"{path.name}: 读取失败 {e}", source=str(path)))
        return None

    source = "builtin" if source_label == "builtin" else str(path)
    try:
        return parse_schema(raw, source=source)
    except SchemaError as e:
        out.diagnostics.append(Diagnostic(level="error", message=f"{path.name}: {e}", source=str(path)))
        return None
