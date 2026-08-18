"""
会话导出。

## 为什么值得做

对话记录是用户真正在意的数据 —— 里面有他和模型一起理清的思路、
调试出的结论。而现在这些内容【只存在于 SQLite 里】，除了在界面上翻，
没有任何办法拿出来。

常见实现没有这个功能（全仓搜导出相关关键词，只有 有实现有个
render 阶段的 markdown transform，与导出无关）。

## 两种格式的分工

- **Markdown**：给人读、贴进笔记或 issue。工具调用折叠、去掉内部 id
- **JSON**：备份和迁移。保留全部字段，能还原

不做 HTML/PDF：Markdown 已经能满足"贴到别处"的需求，
而 PDF 需要额外的渲染依赖。
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from app.core.time import now_ms
from app.modules.session.models import Message, Session

# 单条工具结果在 Markdown 里的展示上限。
#
# 工具结果可能很长（read_file 读一个大文件、grep 匹配几百行）。
# 导出的目的是让人读懂对话脉络，不是复现全部数据 ——
# 完整内容在 JSON 格式里。
MAX_TOOL_CHARS = 2000

# 被截断的工具输出会在正文里留一句「完整输出：<宿主绝对路径>」
# （local.py 的 _truncate_tail 拼进 content 的）。
#
# 导出时必须把这个路径去掉，两个理由：
#
# 1. 它是服务端宿主的绝对路径（形如 D:\...\data\tmp\jeeves_output_x.txt）。
#    把导出贴进 issue 或发给同事，等于泄露目录结构和用户名。
# 2. 对拿到导出文件的人【毫无用处】—— 文件在别人机器上不存在，
#    而且就算是本机，启动时会清理 data/tmp 里超过 24 小时的文件，
#    所以那个路径大概率已经失效。指着一个不存在的路径比不给更误导。
_FULL_PATH_RE = re.compile(r"。完整输出：[^\]]+\]")

# 角色显示名。用中文是因为导出结果是给人读的。
_ROLE_LABEL = {
    "user": "我",
    "assistant": "助手",
    "system": "系统",
    "summary": "上下文摘要",
}


def _ts(ms: int) -> str:
    """毫秒时间戳 → 本地可读时间。"""
    if not ms:
        return ""
    return dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")

def _loads(v: str | None, default: Any) -> Any:
    if not v:
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def to_markdown(session: Session, messages: list[Message]) -> str:
    """
    导出成 Markdown。

    ## 取舍

    **工具调用折叠成 `<details>`**：一轮对话里工具调用可能比正文多得多，
    平铺会让人找不到对话主线。GitHub / VS Code 都支持折叠块。

    **reasoning 不导出**：它是模型的思考过程，通常很长且对回看没用。
    需要的话看 JSON 格式。

    **agent_name 非空的消息单独标注**：子智能体的输出混在主线里会让人
    以为是主模型说的。
    """
    lines: list[str] = []
    title = session.title or "未命名会话"
    lines.append(f"# {title}")
    lines.append("")

    # 元信息表。放在最前 —— 回看一份旧导出时，第一个问题总是"这是什么时候的"
    meta = [
        ("会话 ID", session.id),
        ("创建时间", _ts(session.created_at)),
        ("最后活动", _ts(session.last_message_at)),
        ("消息数", str(session.message_count)),
    ]
    if session.private_mode:
        meta.append(("隐私模式", "开（本会话内容未写入长期记忆）"))
    if session.vision_mode:
        meta.append(("视觉模式", "开"))
    for k, v in meta:
        if v:
            lines.append(f"- **{k}**：{v}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for m in messages:
        if m.role == "tool":
            lines.extend(_md_tool(m))
            continue
        if m.role == "assistant" and not (m.content or "").strip():
            # 只有 tool_calls 没有正文的 assistant 消息 ——
            # 它的内容会体现在紧跟的 tool 消息里，单独渲染是个空标题
            calls = _loads(m.tool_calls, [])
            if calls:
                names = ", ".join(str(c.get("name") or "?") for c in calls)
                lines.append(f"> 调用工具：{names}")
                lines.append("")
            continue

        label = _ROLE_LABEL.get(m.role, m.role)
        if m.agent_name:
            # 子智能体的输出要标出来，否则人会以为是主模型说的
            label += f"（子智能体 {m.agent_name}）"
        lines.append(f"## {label}")
        lines.append("")

        # 引用和附件要标注 —— 不标的话回看时看不懂
        # "帮我改一下这个文件"指的是哪个文件
        refs = _loads(m.refs, [])
        if refs:
            desc = ", ".join(
                f"{r.get('kind', '?')}:{r.get('path') or r.get('name') or r.get('href') or '?'}"
                for r in refs
            )
            lines.append(f"*引用：{desc}*")
            lines.append("")
        atts = _loads(m.attachments, [])
        imgs = [a for a in atts if isinstance(a, str) and a.startswith("data:image/")]
        if imgs:
            # 不内联 base64 —— 一张图能让 Markdown 文件涨几 MB，
            # 而且大多数编辑器不会渲染 data URI
            lines.append(f"*（附带 {len(imgs)} 张图片，未包含在导出中）*")
            lines.append("")

        lines.append((m.content or "").strip())
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def scrub_paths(text: str) -> str:
    """去掉正文里的宿主绝对路径。见 _FULL_PATH_RE 的说明。"""
    return _FULL_PATH_RE.sub("]", text or "")


def _fence(body: str) -> str:
    """
    选一个不会被正文内容闭合的代码围栏。

    ## 为什么不能固定用三个反引号

    工具输出里含 ``` 极其常见 —— 读 markdown 文件、grep 代码块都会。
    一旦出现，围栏提前闭合，后面的内容和 </details> 一起串味，
    整个导出文件的结构从那里开始崩。

    做法是数出正文里最长的连续反引号，用比它更长的围栏。
    """
    longest = 0
    run = 0
    for ch in body:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return "`" * max(3, longest + 1)


def _md_tool(m: Message) -> list[str]:
    """工具结果折叠成 details 块。"""
    name = m.tool_name or "工具"
    flag = "（失败）" if m.is_error else ""
    # 子智能体的工具调用要标出来。
    #
    # 不标的话父代理和子代理的 details 块长得一模一样，而子智能体的
    # 消息按时序【插在】父代理的 tool_calls 和 tool 结果之间 ——
    # 读者面对连续几个 details 块完全无法判断哪个是谁调的。
    who = f"（子智能体 {m.agent_name}）" if m.agent_name else ""
    body = scrub_paths((m.content or "").strip())
    if len(body) > MAX_TOOL_CHARS:
        body = (
            body[:MAX_TOOL_CHARS]
            + f"\n…（已截断，完整内容 {len(m.content or '')} 字符，见 JSON 导出）"
        )
    fence = _fence(body)
    return [
        f"<details><summary>🔧 {name}{who}{flag}</summary>",
        "",
        fence,
        body,
        fence,
        "",
        "</details>",
        "",
    ]


def _scrub_display(d: Any) -> Any:
    """
    去掉 tool_display 里的 full_output_path。

    那是宿主绝对路径，对拿到导出的人没用（文件不在他机器上，
    而且本机也会被 24 小时清理规则删掉），却泄露目录结构。
    """
    if isinstance(d, dict) and "full_output_path" in d:
        out = dict(d)
        out.pop("full_output_path", None)
        return out
    return d


def to_json(session: Session, messages: list[Message]) -> dict[str, Any]:
    """
    导出成 JSON。

    ## 为什么保留全部字段

    这个格式的用途是**备份和迁移** —— 丢字段就等于丢数据。包括
    reasoning、token 计数、run_id/span_id：它们对"事后分析这轮为什么慢/贵"
    有用，而那正是备份该保留的东西。

    ## 为什么带 schema_version

    将来消息结构变了（加字段、改语义），导入方需要知道这份数据是哪个
    版本产生的。不带版本号的话，两年后拿到一份导出文件没法判断能不能读。
    """
    return {
        "schema_version": 1,
        "exported_at": _ts(now_ms()),
        "session": {
            "id": session.id,
            "title": session.title,
            "workspace_id": session.workspace_id,
            "pinned": bool(session.pinned),
            "approval_mode": session.approval_mode,
            "private_mode": bool(session.private_mode),
            "amnesia_mode": bool(session.amnesia_mode),
            "vision_mode": bool(session.vision_mode),
            "created_at": session.created_at,
            "last_message_at": session.last_message_at,
            "message_count": session.message_count,
        },
        "messages": [
            {
                "id": m.id,
                "seq": m.seq,
                "role": m.role,
                "agent_name": m.agent_name,
                # 同样去掉宿主绝对路径 —— JSON 是拿去备份/迁移的，
                # 更可能被发给别人
                "content": scrub_paths(m.content or ""),
                "reasoning": m.reasoning,
                "tool_calls": _loads(m.tool_calls, None),
                "tool_call_id": m.tool_call_id,
                "tool_name": m.tool_name,
                "tool_display": _scrub_display(_loads(m.tool_display, None)),
                "is_error": bool(m.is_error),
                "refs": _loads(m.refs, None),
                # attachments 里的 base64 图片【不导出】——
                # 一张图几百 KB，几张就让 JSON 文件大到无法处理。
                # 只留个数，让导入方知道原来有图。
                "attachment_count": len(_loads(m.attachments, []) or []),
                "run_id": m.run_id,
                "span_id": m.span_id,
                "prompt_tokens": m.prompt_tokens,
                "completion_tokens": m.completion_tokens,
                "created_at": m.created_at,
            }
            for m in messages
        ],
    }


def safe_filename(title: str, session_id: str, ext: str) -> str:
    """
    生成下载文件名。

    ## 为什么要清理标题

    标题是模型生成的，可能含 `/` `\\` `:` `*` `?` `"` `<` `>` `|` ——
    Windows 上这些字符会让保存失败，而浏览器不会提示原因，
    表现是"点了下载什么都没发生"。

    ## 为什么保留 session_id

    标题可能重复（模型对相似问题会起相似标题），加 id 保证唯一。
    也方便回头对照数据库。
    """
    name = (title or "").strip() or "会话"
    # 控制字符和 Windows 保留字符一起清掉
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(". ")
    # 文件名总长要留出 id 和扩展名的余量。
    # 按字符截断在这里是对的 —— 文件名限制是字符数不是字节数，
    # 但保守起点取 60 避免多字节路径超限
    name = name[:60] or "会话"
    return f"{name}_{session_id}{ext}"
