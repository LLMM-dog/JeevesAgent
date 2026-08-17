"""
记忆类型 schema 与字段合并的单元测试。

重点覆盖三处最容易做错的地方：
1. patch 是 SEARCH/REPLACE 而非追加（旧实现的 bug）
2. 无原文时 patch 直接取 replace（漏了会导致新文件永远写不进内容）
3. 加载期的模板变量校验（不校验会在渲染时才炸）
"""

from __future__ import annotations

import pytest
from app.modules.memory.merge import (
    ExactMatcher,
    MergeError,
    SearchReplaceBlock,
    StrPatch,
    apply_merge,
    apply_str_patch,
)
from app.modules.memory.schema import (
    FieldType,
    MemoryField,
    MemoryScopeKind,
    MergeOp,
    OperationMode,
    SchemaError,
    parse_schema,
    template_variables,
)


def _field(name: str = "content", **kw: object) -> MemoryField:
    kw.setdefault("description", "d")
    return MemoryField(name=name, **kw)  # type: ignore[arg-type]


def _patch(*pairs: tuple[str, str]) -> StrPatch:
    return StrPatch(blocks=tuple(SearchReplaceBlock(search=s, replace=r) for s, r in pairs))


# ── merge_op：immutable / replace / sum ─────────────


def test_immutable_keeps_old_value() -> None:
    f = _field("event_name", merge_op=MergeOp.IMMUTABLE)
    assert apply_merge(f, "旧事件", "新事件") == "旧事件"
    # 新文件（current=None）时用新值 —— 否则第一次写入就写不进去
    assert apply_merge(f, None, "新事件") == "新事件"


def test_replace_does_not_overwrite_with_empty() -> None:
    """
    空值不覆盖。LLM 不确定一个字段时倾向于输出空串而不是省略它，
    写进去会抹掉已有的有效值 —— 那是一次不可见的信息丢失。
    """
    f = _field("summary", merge_op=MergeOp.REPLACE)
    assert apply_merge(f, "已有摘要", "") == "已有摘要"
    assert apply_merge(f, "已有摘要", None) == "已有摘要"
    assert apply_merge(f, "已有摘要", "新摘要") == "新摘要"


def test_sum_accumulates_and_clamps_at_zero() -> None:
    f = _field("total_calls", type=FieldType.INT, merge_op=MergeOp.SUM)
    assert apply_merge(f, 7, 3) == 10
    assert apply_merge(f, None, 3) == 3
    # 计数器不该为负。LLM 偶尔输出负数表示"减少"，累加语义下那是错的。
    assert apply_merge(f, 2, -10) == 0


def test_sum_rejects_non_numeric() -> None:
    f = _field("total_calls", type=FieldType.INT, merge_op=MergeOp.SUM)
    with pytest.raises(MergeError):
        apply_merge(f, 1, "很多次")


# ── patch：SEARCH/REPLACE 语义 ──────────────────────


def test_patch_replaces_in_place_not_appends() -> None:
    """
    这是与旧实现最关键的差异。

    旧实现是"字符串追加"，于是"用户从 flake8 换成 ruff"这件事无法表达 ——
    两条矛盾的事实会并存。
    """
    f = _field(merge_op=MergeOp.PATCH)
    old = "# 用户\n- 语言：Python\n- 代码风格：flake8 + black\n- 编辑器：PyCharm"

    got = apply_merge(f, old, _patch(("- 代码风格：flake8 + black", "- 代码风格：ruff（2026-08 换）")))

    assert "ruff" in got
    assert "flake8" not in got, "旧事实必须被替换掉，不能与新事实并存"
    # 未涉及的行原样保留
    assert "- 语言：Python" in got
    assert "- 编辑器：PyCharm" in got


def test_patch_without_original_uses_first_replace() -> None:
    """
    没有原文时不做匹配，直接取第一个块的 replace。

    漏掉这条的后果：新建文件永远写不进内容 —— search 匹配不上空串。
    """
    f = _field(merge_op=MergeOp.PATCH)
    assert apply_merge(f, None, _patch(("", "# 用户\n- 新建"))) == "# 用户\n- 新建"
    assert apply_merge(f, "", _patch(("任意", "正文"))) == "正文"


def test_patch_multiple_blocks_applied_in_sequence() -> None:
    f = _field(merge_op=MergeOp.PATCH)
    old = "a\nb\nc"
    got = apply_merge(f, old, _patch(("a", "A"), ("c", "C")))
    assert got == "A\nb\nC"


def test_patch_empty_search_is_skipped_when_original_exists() -> None:
    """
    有原文时空 search 非法（空串能匹配任意位置）。跳过而不报错 ——
    LLM 偶尔用空 search 表达"追加"。
    """
    f = _field(merge_op=MergeOp.PATCH)
    got = apply_merge(f, "原文", _patch(("", "被忽略"), ("原文", "新文")))
    assert got == "新文"


def test_patch_missing_search_raises_with_field_name() -> None:
    """
    匹配不上要显式失败，因为调用方要把失败信息回给 LLM 重试。
    静默保留原值会让 LLM 以为写成功了，下一轮基于想象的内容继续改。
    """
    f = _field("content", merge_op=MergeOp.PATCH)
    with pytest.raises(MergeError) as err:
        apply_merge(f, "实际内容", _patch(("不存在的片段", "x")))
    assert err.value.field_name == "content"
    assert "找不到" in str(err.value)


def test_patch_ambiguous_search_raises() -> None:
    """
    search 出现多次时不猜第一个 —— 那有 50% 概率改错地方。
    """
    f = _field(merge_op=MergeOp.PATCH)
    with pytest.raises(MergeError, match="多次"):
        apply_merge(f, "- 待办\n- 待办", _patch(("- 待办", "- 完成")))


def test_patch_tolerates_trailing_whitespace_difference() -> None:
    """
    LLM 复制原文时常丢掉行尾空格。这个差异对内容毫无意义，
    不容忍会产生大量假失败。
    """
    f = _field(merge_op=MergeOp.PATCH)
    old = "## 小节   \n- 一条内容\t\n## 另一节"
    got = apply_merge(f, old, _patch(("## 小节\n- 一条内容", "## 小节\n- 改过的内容")))
    assert "改过的内容" in got
    assert "## 另一节" in got


def test_patch_leading_whitespace_is_not_normalized() -> None:
    """
    行尾空白归一化，行首【不】归一化 —— 后者是 Markdown 列表层级，有语义。

    多行 search 的缩进对不上时必须匹配失败，否则会把一条嵌套项当成顶层项改掉。
    """
    with pytest.raises(MergeError, match="找不到"):
        apply_str_patch("## 节\n    - 深缩进项", _patch(("## 节\n- 深缩进项", "x")))


def test_patch_replaces_only_matched_span_keeping_indent() -> None:
    """
    单行 search 命中缩进行内部时只替换匹配到的那一段，缩进原样保留。
    这是 SEARCH/REPLACE 相对整行替换的好处。
    """
    got = apply_str_patch("- 顶层\n  - 嵌套", _patch(("- 嵌套", "- 改过")))
    assert got == "- 顶层\n  - 改过"


def test_patch_on_non_string_field_behaves_as_replace() -> None:
    f = _field("count", type=FieldType.INT, merge_op=MergeOp.PATCH)
    assert apply_merge(f, 1, 5) == 5


def test_patch_accepts_bare_string_from_llm() -> None:
    """
    LLM 对 patch 字段直接给字符串时当整体替换 —— 拒绝它没有好处。
    """
    f = _field(merge_op=MergeOp.PATCH)
    assert apply_merge(f, "旧", "全新内容") == "全新内容"


def test_str_patch_from_raw_skips_block_without_search() -> None:
    parsed = StrPatch.from_raw({"blocks": [{"replace": "只有 replace"}, {"search": "a", "replace": "b"}]})
    assert parsed is not None
    assert len(parsed.blocks) == 1


def test_str_patch_from_raw_returns_none_for_non_patch_shape() -> None:
    assert StrPatch.from_raw("裸字符串") is None
    assert StrPatch.from_raw({"content": "x"}) is None


def test_exact_matcher_returns_offsets_into_original() -> None:
    """归一化匹配命中时，偏移必须指向原文 —— 替换作用在原文上。"""
    content = "第一行  \n目标行\t\n第三行"
    span = ExactMatcher().find(content, "目标行")
    assert span is not None
    start, end = span
    assert content[start:end] == "目标行"


# ── schema 解析与校验 ──────────────────────────────


MINIMAL = {
    "memory_type": "notes",
    "scope": "agent",
    "description": "d",
    "directory": "notes",
    "filename_template": "{{ topic }}.md",
    "fields": [{"name": "topic", "description": "主题", "merge_op": "immutable"}],
}


def test_parse_minimal_schema() -> None:
    s = parse_schema(dict(MINIMAL))
    assert s.memory_type == "notes"
    assert s.scope is MemoryScopeKind.AGENT
    assert s.operation_mode is OperationMode.UPSERT
    assert s.single_file is False
    # 默认 merge_op 是 replace 而非 OpenViking 的 patch：
    # patch 对短标量字段是过重的契约。
    assert parse_schema({**MINIMAL, "fields": [{"name": "topic", "description": "d"}]}).fields[0].merge_op is (
        MergeOp.REPLACE
    )


def test_single_file_inferred_from_template() -> None:
    s = parse_schema({**MINIMAL, "filename_template": "notes.md"})
    assert s.single_file is True


def test_unknown_template_variable_rejected_at_load() -> None:
    """
    不校验的后果是渲染时才炸，而那时 Jinja 的错误信息指向模板内部，
    看不出是哪个 YAML 写错了。
    """
    with pytest.raises(SchemaError, match="未声明的变量"):
        parse_schema({**MINIMAL, "filename_template": "{{ nonexistent }}.md"})


def test_extract_context_is_a_builtin_not_a_field() -> None:
    """events 的路径靠 extract_context.get_year(ranges)，它不需要在 fields 里声明。"""
    s = parse_schema(
        {
            **MINIMAL,
            "filename_template": "{{ extract_context.get_year(ranges) }}/{{ topic }}.md",
            "fields": [
                {"name": "topic", "description": "d", "merge_op": "immutable"},
                {"name": "ranges", "description": "d"},
            ],
        }
    )
    assert "extract_context" in template_variables(s.filename_template)


def test_session_scope_must_disable_peer() -> None:
    """不存在"A 眼中 B 的会话事件"。"""
    with pytest.raises(SchemaError, match="peer_enabled"):
        parse_schema({**MINIMAL, "scope": "session"})
    ok = parse_schema({**MINIMAL, "scope": "session", "peer_enabled": False})
    assert ok.peer_enabled is False


def test_bad_memory_type_name_rejected() -> None:
    with pytest.raises(SchemaError, match="小写下划线"):
        parse_schema({**MINIMAL, "memory_type": "MyNotes"})


def test_missing_description_rejected_for_llm_field() -> None:
    """description 是发给 LLM 的提示词，没有它这个字段对模型是无意义的。"""
    with pytest.raises(SchemaError, match="description"):
        parse_schema({**MINIMAL, "fields": [{"name": "topic"}]})


def test_system_field_may_omit_description() -> None:
    """system 字段不发给 LLM，可以没有 description。"""
    s = parse_schema(
        {
            **MINIMAL,
            "fields": [
                {"name": "topic", "description": "d", "merge_op": "immutable"},
                {"name": "chat_log", "system": True},
            ],
        }
    )
    assert [f.name for f in s.llm_fields()] == ["topic"]


def test_duplicate_field_name_rejected() -> None:
    with pytest.raises(SchemaError, match="重复"):
        parse_schema(
            {**MINIMAL, "fields": [{"name": "topic", "description": "a"}, {"name": "topic", "description": "b"}]}
        )


def test_unknown_scope_rejected() -> None:
    with pytest.raises(SchemaError, match="scope"):
        parse_schema({**MINIMAL, "scope": "workspace"})
