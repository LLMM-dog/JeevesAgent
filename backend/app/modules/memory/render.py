"""
记忆文件的序列化与模板渲染。

## 格式：YAML frontmatter + Markdown 正文

OpenViking 用文件末尾的 `<!-- MEMORY_FIELDS {json} -->`（memory_file_utils.py:112）。
改用 frontmatter 的三个理由：

1. 本项目已经在用（skills/*/SKILL.md、agents/*.md），有现成的 parse_frontmatter
   和相应约定。再引入第二套元数据格式没有收益。
2. frontmatter 在文件头，`head -20` 就能看清一个记忆是什么。
3. 编辑器和 GitHub 原生渲染 frontmatter，HTML 注释里的 JSON 只是一坨。

代价是 YAML 对多行字符串的转义比 JSON 麻烦。所以 frontmatter 里只放标量和
短列表，长文本进正文（由 content_template 渲染）。
"""

from __future__ import annotations

import re
from typing import Any

import structlog
import yaml
from jinja2 import Environment, StrictUndefined, TemplateError

from app.core.time import now_ms
from app.modules.memory.models import MemoryItem, MemoryScope
from app.modules.memory.schema import MemoryScopeKind, MemoryTypeSchema

log = structlog.get_logger(__name__)

# 系统字段。由存储层写入，LLM 不许碰。
#
# 【必须与 MemoryItem 的字段对应】—— 解析时按这个集合把 frontmatter 拆成
# 业务字段和系统字段两半。
# content 字段原始值的标记。见 serialize 里的说明。
RAW_CONTENT_MARKER = "JEEVES_RAW_CONTENT"

# 原始值在正文中的位置（"起点:长度"）。绝大多数情况用它，省掉一份副本。
RAW_SPAN_KEY = "raw_content_span"

_RAW_CONTENT_RE = re.compile(
    r"\n*<!--\s*" + RAW_CONTENT_MARKER + r"\s*\n(?P<raw>.*?)\n-->\s*$", re.DOTALL
)

SYSTEM_KEYS = frozenset(
    {
        "memory_type",
        "scope",
        "agent_id",
        "session_id",
        "peer_agent_id",
        "version",
        "created_at",
        "updated_at",
        "source_extraction_id",
        "last_update_trace_id",
        # 原始值偏移。系统字段 —— 不能进 business fields，
        # 否则它会被当成业务字段参与合并，还会出现在发给 LLM 的契约里。
        "raw_content_span",
    }
)

# Jinja 环境。
#
# StrictUndefined：模板引用了不存在的变量时【报错】而不是渲染成空串。
# 静默渲染成空串的后果是文件里出现一个空白段落，而根本原因（YAML 写错了
# 字段名）完全看不出来。加载期的 _validate_path_templates 挡的是路径模板，
# content_template 只能在这里挡。
#
# autoescape=False：输出是 Markdown 不是 HTML，转义会把 `<` 变成 `&lt;`。
#
# trim_blocks + lstrip_blocks：块标签（{% for %} / {% if %}）独占一行时
# 不产生空行。没有它们的话 overview 会渲染成"标题 + 两个空行 + 列表"，
# 而 Markdown 里连续空行会把列表和标题拆成两段。
_env = Environment(
    undefined=StrictUndefined,
    autoescape=False,
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


class RenderError(ValueError):
    """模板渲染失败。"""


def render_template(template: str, context: dict[str, Any]) -> str:
    if not template:
        return ""
    try:
        return _env.from_string(template).render(**context)
    except TemplateError as e:
        raise RenderError(str(e)) from e


def render_path(
    schema: MemoryTypeSchema,
    fields: dict[str, Any],
    *,
    extract_context: Any = None,
) -> tuple[str, str]:
    """
    渲染 (directory, filename)。

    extract_context 是提取阶段注入的辅助对象，提供 get_year(ranges) 这类方法
    让 events 能按日期分层。存储层单独调用时传 None —— 那时如果模板确实
    引用了它，StrictUndefined 会报错，而那正是我们要的（说明这个类型
    只能在提取流程里写）。

    ## 字段值里的 / 要先去掉

    路径里的 `/` 只允许来自【模板结构】（events 的 `<Y>/<M>/<D>/<name>.md`），
    不允许来自【字段值】。

    实测：LLM 给出 topic="a/b:c" 时，渲染出 "a/b_c.md"，而 layout.resolve
    会按 `/` 切段 —— 于是凭空多出一个 `a/` 目录。之后按 topic 读回来会失败，
    因为 resolve_path 每次都算出同一个奇怪路径，但列举时它落在子目录里，
    而单文件/多文件的目录假设都被打破了。

    layout._safe_segment 挡不住这个：它是在切段【之后】才对每一段清理的。
    """
    safe_fields = {k: _sanitize_path_value(v) for k, v in fields.items()}
    context: dict[str, Any] = dict(safe_fields)
    if extract_context is not None:
        # extract_context 的方法返回值（年/月/日）来自代码而非 LLM，
        # 它产生的 `/` 是模板结构的一部分，不清理。
        context["extract_context"] = extract_context
    return (
        render_template(schema.directory, context) if schema.directory else "",
        render_template(schema.filename_template, context),
    )


def _sanitize_path_value(value: Any) -> Any:
    """把字段值里的路径分隔符换成下划线。非字符串原样返回。"""
    if not isinstance(value, str):
        return value
    return value.replace("/", "_").replace("\\", "_")


def render_body(
    schema: MemoryTypeSchema,
    fields: dict[str, Any],
    *,
    extract_context: Any = None,
) -> str:
    """
    渲染正文。

    ## 渲染失败时不丢内容

    content_template 抛异常时回落到 fields["content"] 的原值，而不是写一个
    空文件。OpenViking 同样这么做（memory_file_utils.py:93）。

    理由：模板是格式，内容是信息。格式坏了应该保住信息。
    """
    if not schema.content_template:
        return _as_text(fields.get("content"))

    context: dict[str, Any] = dict(fields)
    # 【始终注入 extract_context 这个键】，没有上下文时给 None。
    #
    # StrictUndefined 下 `{% if extract_context %}` 对未定义变量会直接报错，
    # 而模板需要这个判断来兼容"存储层被直接调用"的场景（测试、手工修复、导入）。
    # 注入 None 让 if 判断成立且为假，模板的其余部分正常渲染。
    #
    # 不这么做的话整个模板渲染失败、正文回落成裸 content —— 丢掉标题和结构，
    # 而那个丢失只有一条 warning 日志，文件本身看起来"只是格式简单些"。
    context["extract_context"] = extract_context
    try:
        return render_template(schema.content_template, context).strip()
    except RenderError as e:
        log.warning(
            "memory_content_template_failed",
            memory_type=schema.memory_type,
            error=str(e),
            hint="回落到 content 字段原值",
        )
        return _as_text(fields.get("content"))


def render_overview(schema: MemoryTypeSchema, items: list[MemoryItem], directory_name: str) -> str:
    """
    渲染目录索引 .overview.md。schema 没有 overview_template 时返回空串。

    items 的每项暴露 file_name + file_content（字段字典），与 OpenViking 的
    overview_template 变量名一致，这样它的模板能直接搬过来用。
    """
    if not schema.overview_template:
        return ""

    view = [
        {"file_name": item.uri.rsplit("/", 1)[-1], "file_content": dict(item.fields), "title": item.title}
        for item in items
    ]
    try:
        return render_template(
            schema.overview_template, {"items": view, "directory_name": directory_name}
        ).strip()
    except RenderError as e:
        log.warning("memory_overview_template_failed", memory_type=schema.memory_type, error=str(e))
        return ""


def render_embedding_text(schema: MemoryTypeSchema, item: MemoryItem, *, level: int = 2) -> str:
    """
    向量化用的文本。没有 embedding_template 时用正文。

    对于 L2 详细层级，会截断过长的文本以：
    1. 降低嵌入 API 成本
    2. 提高召回准确性（避免关键信息被稀释）
    3. 避免超过嵌入模型的 token 限制

    截断策略：取开头 + 结尾，保留关键信息和结论。
    """
    from app.core.config import settings

    # 先渲染出完整文本
    if not schema.embedding_template:
        text = item.body
    else:
        context: dict[str, Any] = dict(item.fields)
        context["content"] = item.body
        try:
            text = render_template(schema.embedding_template, context).strip()
        except RenderError:
            text = item.body

    # L2 层级需要截断
    if level == 2 and len(text) > settings.memory.embedding_l2_max_chars:
        limit = settings.memory.embedding_l2_max_chars
        # 取开头 70% + 结尾 30%，保留开头的背景和结尾的结论
        head_len = int(limit * 0.7)
        tail_len = limit - head_len
        head = text[:head_len]
        tail = text[-tail_len:] if tail_len > 0 else ""
        text = f"{head}\n\n[... 中间省略 {len(text) - limit} 字符 ...]\n\n{tail}"

    return text


def serialize(item: MemoryItem, *, source_extraction_id: str = "", trace_id: str = "") -> str:
    """MemoryItem → 文件内容。"""
    front: dict[str, Any] = {
        "memory_type": item.memory_type,
        "scope": item.scope.value,
        "version": item.version,
        "created_at": item.created_at or now_ms(),
        "updated_at": item.updated_at or now_ms(),
    }
    if item.agent_id:
        front["agent_id"] = item.agent_id
    if item.session_id:
        front["session_id"] = item.session_id
    if item.peer_agent_id:
        front["peer_agent_id"] = item.peer_agent_id
    if source_extraction_id:
        front["source_extraction_id"] = source_extraction_id
    if trace_id:
        front["last_update_trace_id"] = trace_id

    # extra 先写：schema 未声明但存在于旧文件里的字段（含旧的
    # source_extraction_id）。业务字段后写，同名时业务字段赢。
    for key, value in item.extra.items():
        if key not in SYSTEM_KEYS and value is not None:
            front[key] = value

    # 长文本不进 frontmatter —— YAML 的多行转义难读，而正文本来就是它的位置。
    # content 字段是正文的原料，它已经渲染进 body 了。
    for key, value in item.fields.items():
        if key == "content":
            continue
        if value is None:
            continue
        front[key] = value

    # content 字段的【原始值】要能被找回，否则下一次合并会拿渲染结果当输入。
    #
    # ## 为什么不能直接用渲染后的正文当 current
    #
    # content_template 会在 content 外面套一层（tool_notes 的 "# 工具：xxx"
    # 加计数行）。如果下一轮合并把渲染结果当 current 再渲染一次，
    # 那层壳会被【重复叠加】—— 实测在试验场里 run_shell.md 出现了两个
    # "# 工具：run_shell" 标题和两组计数行，version 每涨一次多一层。
    #
    # ## 优先存偏移而不是存副本
    #
    # 绝大多数情况下模板只是在 content 外面套壳，原始值【原样出现在正文里】。
    # 这时存一个 "起点:长度" 就够，解析时切片还原。
    #
    # 实测存全文副本的代价：trajectories 的操作契约有 700 字符，
    # 文件总长 2200，其中 33% 是重复内容 —— 而记忆目录是要进 git 的，
    # 每次改动都会在 diff 里出现两遍。
    #
    # 模板对 content 做了变换（过滤器、条件裁剪）时偏移法失效，
    # 那时回落到存副本。判据是"原始值能不能在正文里原样找到"。
    # 偏移必须相对【最终写进文件的正文】算。body 会被 strip()，
    # 用未 strip 的值算偏移会整体错位。
    stripped_body = item.body.strip()

    marker = ""
    raw = item.raw_content
    if raw and raw != stripped_body:
        offset = stripped_body.find(raw)
        if offset >= 0:
            front[RAW_SPAN_KEY] = f"{offset}:{len(raw)}"
        else:
            marker = f"\n\n<!-- {RAW_CONTENT_MARKER}\n{raw}\n-->"

    dumped = yaml.safe_dump(front, allow_unicode=True, sort_keys=False, default_flow_style=False)
    if not stripped_body:
        return f"---\n{dumped}---\n"
    return f"---\n{dumped}---\n\n{stripped_body}{marker}\n"


def _raw_from_span(body: str, span: Any) -> str:
    """
    按 "起点:长度" 从正文里切出原始值。

    任何异常都回落空串 —— 那会让合并把整个正文当 current，
    结果是模板壳被重复叠加一次。比抛异常好：记忆还在，只是格式变丑，
    而抛异常会让这条记忆完全读不出来。

    偏移失效的实际场景：有人手工编辑了记忆文件（这是文件形态的核心卖点，
    必须支持）。那时偏移不再对应原来的位置。
    """
    if not span or not isinstance(span, str) or ":" not in span:
        return ""
    start_raw, _, length_raw = span.partition(":")
    try:
        start, length = int(start_raw), int(length_raw)
    except ValueError:
        return ""
    if start < 0 or length <= 0 or start + length > len(body):
        return ""
    return body[start : start + length]


def split_raw_content(body: str) -> tuple[str, str]:
    """
    切出正文与 content 字段的原始值。

    没有标记时 raw 返回空串，调用方回落到用正文当原始值
    （没有 content_template 的类型就是这种情况）。
    """
    m = _RAW_CONTENT_RE.search(body)
    if m is None:
        return body, ""
    return body[: m.start()].rstrip(), m.group("raw")


def parse(raw: str, *, uri: str = "") -> MemoryItem:
    """
    文件内容 → MemoryItem。

    坏 frontmatter 不抛异常：返回一个正文完整、字段为空的 item。
    调用方（file_store）按"坏文件跳过并记 warning"处理 —— 列举 100 个记忆时
    第 37 个坏了，应该返回 99 个而不是整个失败。
    """
    front, body = split_frontmatter(raw)

    system: dict[str, Any] = {}
    business: dict[str, Any] = {}
    for key, value in front.items():
        (system if key in SYSTEM_KEYS else business)[key] = value

    scope_raw = str(system.get("scope") or MemoryScopeKind.AGENT.value)
    try:
        scope = MemoryScopeKind(scope_raw)
    except ValueError:
        scope = MemoryScopeKind.AGENT

    visible_body, raw_content = split_raw_content(body.strip())
    if not raw_content:
        # 没有副本 → 试偏移法还原。
        raw_content = _raw_from_span(visible_body, system.get(RAW_SPAN_KEY))

    return MemoryItem(
        uri=uri,
        memory_type=str(system.get("memory_type") or ""),
        scope=scope,
        fields=business,
        body=visible_body,
        raw_content=raw_content,
        version=_as_int(system.get("version"), default=1),
        created_at=_as_int(system.get("created_at"), default=0),
        updated_at=_as_int(system.get("updated_at"), default=0),
        agent_id=str(system.get("agent_id") or ""),
        session_id=str(system.get("session_id") or ""),
        peer_agent_id=str(system.get("peer_agent_id") or ""),
        extra={k: v for k, v in system.items() if k not in ("memory_type", "scope", "version")},
    )


def split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """
    切出 frontmatter 与正文。

    没有 frontmatter、或它不是映射、或 YAML 语法错 → 返回 ({}, 全文)。
    与 skill/loader.py 的 parse_frontmatter 行为一致（有起始分隔符但没有
    结束符时当作没有 frontmatter），保持项目内一致。
    """
    text = raw.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}, text

    lines = text.split("\n")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), -1)
    if end < 0:
        return {}, text

    try:
        loaded = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as e:
        log.warning("memory_frontmatter_invalid", error=str(e))
        return {}, text

    front = loaded if isinstance(loaded, dict) else {}
    return front, "\n".join(lines[end + 1 :])


def build_item(
    schema: MemoryTypeSchema,
    scope: MemoryScope,
    merged_fields: dict[str, Any],
    body: str,
    *,
    old: MemoryItem | None,
) -> MemoryItem:
    """
    组装待写入的 item。version 递增与时间戳在这里统一处理。

    raw_content 存合并后的 content 字段值（渲染前）。只在有模板时才与
    body 不同 —— 见 MemoryItem.raw_content 的说明。
    """
    now = now_ms()
    raw = merged_fields.get("content")
    return MemoryItem(
        uri=old.uri if old else "",
        memory_type=schema.memory_type,
        scope=schema.scope,
        fields=merged_fields,
        body=body,
        raw_content=raw if isinstance(raw, str) and schema.content_template else "",
        version=(old.version + 1) if old else 1,
        created_at=old.created_at if old and old.created_at else now,
        updated_at=now,
        agent_id=(
            scope.agent_id if schema.scope is MemoryScopeKind.AGENT else ""
        ),
        session_id=scope.session_id if schema.scope is MemoryScopeKind.SESSION else "",
        peer_agent_id=(
            scope.peer_agent_id
            if scope.is_peer_view and schema.peer_enabled and schema.scope is MemoryScopeKind.AGENT
            else ""
        ),
        extra=dict(old.extra) if old else {},
    )


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _as_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
