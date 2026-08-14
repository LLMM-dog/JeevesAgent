"""
LLM 输出契约：从 schema 定义生成 JSON Schema。

## 为什么要动态生成

「加一个记忆字段」只该改 YAML，不该改 prompt。契约由字段的
`(type, merge_op)` 组合推导：

    string + patch              → {"blocks": [{"search", "replace"}]}
    string + replace/immutable  → 裸字符串
    int/float + sum/replace     → 裸数字

对齐 OpenViking 的 `schema_model_generator.py`。差别是它生成 Pydantic 类，
我们生成 JSON Schema dict —— 本项目的 LLM 调用走 `LLMPort.stream_chat` 传原始
dict，多一层 Pydantic 只是转换开销。

## 为什么不用 function calling 而用 JSON 输出

提取是一次性产出结构化结果，不需要多轮工具协商。而 function calling 在
不同供应商间的行为差异比 JSON 输出大得多（参数名、嵌套层数、是否支持
`additionalProperties`）—— 本项目要兼容任意 OpenAI 兼容端点。
"""

from __future__ import annotations

from typing import Any

from app.modules.memory.schema import (
    FieldType,
    MemoryField,
    MemoryTypeSchema,
    MergeOp,
    OperationMode,
)

# SEARCH/REPLACE 的说明。
#
# 写得这么细是必要的：patch 失败最常见的原因是模型把整段贴进 search，
# 或者带上了行号前缀。这两件事都必须显式禁止。
PATCH_RULES = """\
SEARCH/REPLACE 规则（用于 merge_op=patch 的字段）：
- search 必须是原文中【唯一】的最小片段，通常 2-4 行。整段贴进去会匹配失败
- search 必须与原文【逐字符一致】，包括缩进。不要带行号前缀
- replace 是替换后的内容。用空字符串表示删除这段
- 要修正一个过时的事实，就 search 旧句子、replace 新句子 —— 不要只追加新句子，
  那会让两个矛盾的事实并存
- 多处修改用多个 block，每个 block 的 search 各自唯一
- 新建记忆（没有 page_id）时 search 留空，replace 写完整内容"""


def _patch_schema(description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "description": f"{description}\n\n（这是 patch 字段，输出 SEARCH/REPLACE 块）",
        "properties": {
            "blocks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "search": {"type": "string", "description": "原文中唯一的最小片段。新建时留空"},
                        "replace": {"type": "string", "description": "替换后的内容。空串表示删除"},
                    },
                    "required": ["search", "replace"],
                },
            }
        },
        "required": ["blocks"],
    }


def field_schema(field: MemoryField) -> dict[str, Any]:
    """单个字段的 JSON Schema。"""
    if field.type is FieldType.STRING and field.merge_op is MergeOp.PATCH:
        return _patch_schema(field.description)

    base: dict[str, Any] = {"description": field.description}
    if field.type is FieldType.INT:
        base["type"] = "integer"
    elif field.type is FieldType.FLOAT:
        base["type"] = "number"
    elif field.type is FieldType.BOOL:
        base["type"] = "boolean"
    else:
        base["type"] = "string"

    if field.merge_op is MergeOp.SUM:
        base["description"] += "\n（累加字段：填本次【新增】的量，不要填累计总数）"
    elif field.merge_op is MergeOp.IMMUTABLE:
        base["description"] += "\n（不可变字段：一旦写入不会再改）"
    return base


def item_schema(schema: MemoryTypeSchema, *, include_description: bool = True) -> dict[str, Any]:
    """
    一条记忆的 JSON Schema。

    page_id 是【所有类型都有】的字段，且不来自 schema.fields —— 它是
    引用机制而非业务字段。放在这里而不是让每个 YAML 声明它，
    避免十个文件重复同一件事。

    include_description=False 时省掉类型级描述。用在完整契约里 ——
    那些描述已经在系统提示词的「记忆类型」小节里出现过一次，
    重复一遍纯属浪费窗口（events 的描述有 1136 字符）。
    """
    props: dict[str, Any] = {}
    required: list[str] = []

    # add_only 类型不给 page_id：它永远新建，给了只会让模型试图去改，
    # 而那个改动会被 next_available_path 转成另一个新文件 —— 行为对但意图错。
    if schema.operation_mode is not OperationMode.ADD_ONLY:
        props["page_id"] = {
            "type": ["integer", "null"],
            "description": (
                "要修改的已有记忆的 page_id（见「已有记忆」部分）。"
                "新建记忆时填 null。填了不存在的 id 会被当作新建。"
            ),
        }

    for field in schema.llm_fields():
        props[field.name] = field_schema(field)
        # immutable 字段是文件名的来源，必须有值，否则路径渲染不出来
        if field.merge_op is MergeOp.IMMUTABLE:
            required.append(field.name)

    out: dict[str, Any] = {"type": "object", "properties": props, "required": required}
    if include_description:
        out["description"] = schema.description
    return out


def operations_schema(schemas: list[MemoryTypeSchema]) -> dict[str, Any]:
    """
    整个提取输出的 JSON Schema。

    ## 结构：按记忆类型分键，而不是一个扁平的 items 数组

    扁平数组要求模型在每条里写 `"memory_type": "events"`，而它会写错
    （写成不存在的类型、或者写对了但字段用了别的类型的）。
    按类型分键让每个键下的字段约束是确定的，模型填错字段时 schema 能挡住。

    这一点与 OpenViking 一致（它的 create_structured_operations_model
    给每个 memory_type 生成独立字段）。
    """
    props: dict[str, Any] = {
        "reasoning": {
            "type": "string",
            "description": "简述你的判断：哪些内容值得记、哪些不值得、为什么。先想再写",
        }
    }
    for schema in schemas:
        props[schema.memory_type] = {
            "type": "array",
            # 只放一行摘要。完整描述在系统提示词的「记忆类型」小节里，
            # 两处都放会让 events 的 1136 字符说明出现两遍。
            "description": f"{schema.memory_type}：{_first_line(schema.description)}",
            "items": item_schema(schema, include_description=False),
        }

    props["delete_page_ids"] = {
        "type": "array",
        "items": {"type": "integer"},
        "description": (
            "要删除的记忆的 page_id。只在一条记忆【明确过时或错误】时删除，"
            "不要因为想改写就先删再建 —— 改写用 page_id + patch"
        ),
    }

    return {
        "type": "object",
        "properties": props,
        "required": ["reasoning"],
    }


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def empty_operations(schemas: list[MemoryTypeSchema]) -> dict[str, Any]:
    """
    空结果的形状。

    最后一轮要给模型一个"没有记忆要写"的确切模板 —— 否则它会输出
    一段解释文字，那解析不出来。对齐 OpenViking 的
    _build_final_operations_skeleton（extract_loop.py:762）。
    """
    out: dict[str, Any] = {"reasoning": ""}
    for schema in schemas:
        out[schema.memory_type] = []
    out["delete_page_ids"] = []
    return out
