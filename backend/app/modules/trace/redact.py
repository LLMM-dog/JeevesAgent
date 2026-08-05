"""
入库前脱敏。

## 常见实现没有这个

甚至把密钥明文过 IPC。

span 的 input/output 会捕获工具参数和模型配置 —— 密钥泄漏是**必然**而非
可能。举几个真会发生的场景：

- 用户让 agent `curl -H "Authorization: Bearer sk-xxx" ...`
- agent 读了一个 `.env` 文件，内容进了 span 的 output
- 工具参数里带了数据库连接串（含密码）

追踪数据的生命周期比会话长得多（它是为了事后排查而存在的），
所以泄漏窗口也长得多。

## 为什么用正则而不是白名单字段

白名单适合结构化的 attributes，但 input/output 是自由文本 ——
密钥可能出现在任何位置。这里两种都用：文本走正则，attributes 走白名单。

注意 pi 那三个 `sanitize*` 函数（`shell-output.ts:30`、
`sanitize-unicode.ts:21`、相关实现）都不是干这个的，
名字容易误导。
"""

from __future__ import annotations

import re

# 每条规则保留少量前缀，好让人能判断"是哪个 key"而不暴露它。
#
# 保留 4 位不是随手定的：够区分不同的 key，又不足以被利用。
# 全遮掉的话排查时无法确认"用的是不是我以为的那把钥匙"。
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # OpenAI 风格：sk-xxx / sk-proj-xxx
    (re.compile(r"\b(sk-(?:proj-)?)[A-Za-z0-9_\-]{8,}"), r"\1***"),
    # Anthropic
    (re.compile(r"\b(sk-ant-)[A-Za-z0-9_\-]{8,}"), r"\1***"),
    # Google / 通用 AIza
    (re.compile(r"\b(AIza)[A-Za-z0-9_\-]{10,}"), r"\1***"),
    # GitHub
    (re.compile(r"\b(gh[pousr]_)[A-Za-z0-9]{10,}"), r"\1***"),
    # Bearer token
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{12,}", re.I), r"\1***"),
    # key=value 形式。注意 value 可能带引号
    (
        re.compile(
            r"""\b(api[_-]?key|apikey|secret|password|passwd|token|access[_-]?key)"""
            r"""(\s*[=:]\s*)(["']?)([^\s"',;&]{6,})(["']?)""",
            re.I,
        ),
        r"\1\2\3***\5",
    ),
    # JSON 形式 "api_key": "xxx"
    (
        re.compile(
            r'"(api[_-]?key|secret|password|token|access[_-]?key)"(\s*:\s*)"([^"]{6,})"',
            re.I,
        ),
        r'"\1"\2"***"',
    ),
    # 数据库连接串里的密码：scheme://user:pass@host
    (re.compile(r"(://[^:/\s]+:)([^@/\s]{3,})(@)"), r"\1***\3"),
    # AWS
    (re.compile(r"\b(AKIA)[0-9A-Z]{12,}"), r"\1***"),
)

# attributes 里允许原样存的字段。白名单而非黑名单 ——
# 新增一个字段时默认不存，比默认存了才发现泄漏好。
ATTR_WHITELIST = frozenset(
    {
        "tool_name",
        "call_id",
        "model_id",
        "provider_name",
        "agent_name",
        "path",
        "pattern",
        "command_head",
        "exit_code",
        "turns",
        "stop_reason",
        "skill",
        "depth",
    }
)


def redact(text: str) -> str:
    """
    脱敏。永不抛异常 —— 脱敏失败不该让追踪写入失败，
    但也绝不能因此写入未脱敏的原文。
    """
    if not text:
        return text
    out = text
    for pat, repl in _PATTERNS:
        try:
            out = pat.sub(repl, out)
        except re.error:  # pragma: no cover
            # 正则出错时【返回占位符而不是原文】。
            # 宁可丢失可观测性，也不能泄漏密钥。
            return "[脱敏失败，内容已丢弃]"
    return out


def redact_attrs(attrs: dict[str, object]) -> dict[str, object]:
    """按白名单过滤 attributes，值仍走一遍文本脱敏。"""
    out: dict[str, object] = {}
    for k, v in attrs.items():
        if k not in ATTR_WHITELIST:
            continue
        out[k] = redact(v) if isinstance(v, str) else v
    return out
