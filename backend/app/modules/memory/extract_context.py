"""
提取期的模板上下文。

## 它解决什么

events 的路径模板是：

    {{ extract_context.get_year(ranges) }}/{{ extract_context.get_month(ranges) }}/...

日期【不能由 LLM 提供】—— 它会写"昨天"、写错年份、或者用今天的日期标注三天前
发生的事。日期必须由系统从消息的真实时间戳推导。

所以路径渲染需要一个能按 ranges 回查消息的对象，而不只是字段字典。
这个类就是那个对象。对齐 OpenViking 的 ExtractContext
（memory_updater.py:192），但只保留 Jeeves 需要的部分：它那个类还管
消息分片、资源引用、链接摘要，那些属于提取编排。

## 为什么 ranges 而不是让 LLM 直接给日期

ranges 是"这件事对应哪几条消息"，是 LLM 真正知道的信息。
日期是从那几条消息的 created_at 推出来的，是系统知道的信息。
各自提供自己知道的那部分。

顺带好处：ranges 让 chat_log 能取到对话原文，而不是让 LLM 凭记忆重写 ——
重写过的对话不再是证据。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

import structlog

from app.core.time import to_local, weekday_cn
from app.modules.agent.messages import Msg

log = structlog.get_logger(__name__)

# "0-3,7,15-20" 这类范围表达式里的单项
_RANGE_ITEM = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+)\s*)?$")

# chat_log 的长度上限。
#
# 一条事件的对话原文超过这个长度就截断 —— 事件是"摘要 + 证据"，
# 证据不该长到把摘要淹没。实测一次代码修改任务的工具输出能有几万字符。
MAX_CHAT_LOG_CHARS = 4_000


@dataclass
class ExtractContext:
    """
    一次提取的上下文。传给 render 让路径与正文模板能回查消息。

    messages 是【按 seq 升序】的整段对话。ranges 里的索引就是这个列表的下标 ——
    不是 message.seq，也不是 message.id。

    ## 为什么用下标而不是 seq

    发给 LLM 的对话是【裁剪过】的（压缩会删中段）。让它输出 seq 的话，
    它看到的编号和库里的 seq 对不上。用它实际看到的列表下标最不容易错。
    """

    messages: list[Msg] = field(default_factory=list)
    # 每条消息的 created_at（UTC 毫秒），与 messages 一一对应。
    # 空列表时所有日期回落到"现在"。
    timestamps: list[int] = field(default_factory=list)
    # 会话开始时间，作为拿不到具体消息时间时的兜底。
    session_started_at: int = 0

    # ── 日期，供 filename_template 用 ──────────────

    def get_year(self, ranges: str = "") -> str:
        return f"{self._dt(ranges).year:04d}"

    def get_month(self, ranges: str = "") -> str:
        return f"{self._dt(ranges).month:02d}"

    def get_day(self, ranges: str = "") -> str:
        return f"{self._dt(ranges).day:02d}"

    def get_date(self, ranges: str = "") -> str:
        return self._dt(ranges).strftime("%Y-%m-%d")

    def get_timestamp(self, ranges: str = "") -> str:
        """本地时间戳，带中文星期。事件正文里标注"这事什么时候说的"。"""
        dt = self._dt(ranges)
        return f"{dt.strftime('%Y-%m-%d %H:%M')} {weekday_cn(dt)}"

    # ── 对话原文，供 content_template 用 ───────────

    def get_chat_log(self, ranges: str = "", max_chars: int = MAX_CHAT_LOG_CHARS) -> str:
        """
        ranges 指向的对话原文。

        ## 为什么工具调用也要渲染出来

        "他让我改文件，我改了"这件事的证据是 edit_file 的调用与结果。
        只渲染 user/assistant 的文本会让事件的证据链断掉 ——
        而 agent 会话里大部分动作都发生在工具调用里。
        """
        picked = self.slice(ranges)
        if not picked:
            return "（无对应对话）"

        lines: list[str] = []
        for msg in picked:
            lines.extend(self._render_msg(msg))

        text = "\n".join(lines)
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip() + f"\n…（截断，共 {len(text)} 字符）"

    def slice(self, ranges: str) -> list[Msg]:
        """按 ranges 取消息。越界索引被忽略而不是报错。"""
        if not self.messages:
            return []
        indices = self.parse_ranges(ranges)
        if not indices:
            return []
        return [self.messages[i] for i in indices if 0 <= i < len(self.messages)]

    def parse_ranges(self, ranges: str) -> list[int]:
        """
        "0-3,7,15-20" → [0,1,2,3,7,15,...,20]

        ## 为什么容错而不是报错

        ranges 来自 LLM。它会写 "0-3, 7"（带空格）、"3-0"（反着写）、
        "5"（单条）。拒绝一条记忆的代价是那条信息永久丢失，
        而宽容解析的代价只是取到的消息范围略有偏差。

        无法解析的部分被跳过并记 debug 日志。全部无法解析时返回空列表，
        由调用方决定怎么办（get_chat_log 会返回"无对应对话"）。
        """
        out: list[int] = []
        for part in str(ranges or "").replace("，", ",").split(","):
            if not part.strip():
                continue
            m = _RANGE_ITEM.match(part)
            if m is None:
                log.debug("memory_ranges_unparsable", part=part, ranges=ranges)
                continue
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else start
            if start > end:
                start, end = end, start
            # 上限保护：LLM 偶尔写 "0-99999"，那会生成十万个下标。
            end = min(end, len(self.messages) - 1 if self.messages else start)
            out.extend(range(start, end + 1))
        # 去重并保序
        return list(dict.fromkeys(out))

    # ── 内部 ─────────────────────────────────────

    def _dt(self, ranges: str) -> datetime:
        """
        ranges 首条消息的本地时间。拿不到时回落会话开始时间，再回落现在。

        ## 为什么要三级回落而不是报错

        日期决定文件路径。拿不到日期就写不进这条记忆 —— 而"时间戳缺失"
        （手工构造的上下文、被压缩掉的消息）不该导致信息丢失。
        归到今天的目录里略有偏差，但记忆还在。
        """
        for i in self.parse_ranges(ranges):
            if 0 <= i < len(self.timestamps) and self.timestamps[i] > 0:
                return to_local(self.timestamps[i])
        return to_local(self.session_started_at or None)

    @staticmethod
    def _render_msg(msg: Msg) -> list[str]:
        speaker = {"user": "用户", "assistant": "助手", "tool": "工具", "system": "系统"}.get(
            msg.role, msg.role
        )
        lines: list[str] = []

        if msg.content:
            lines.append(f"{speaker}：{msg.content}")

        for call in msg.tool_calls:
            lines.append(f"助手→调用 {call.name}({_short(call.arguments)})")

        if msg.role == "tool" and msg.tool_name:
            mark = "失败" if msg.is_error else "结果"
            lines.append(f"工具 {msg.tool_name} {mark}：{_short(msg.content or '', 300)}")
            # tool 消息的 content 已经在上面渲染过，去掉重复的那一行
            if len(lines) > 1 and lines[0].startswith("工具："):
                lines.pop(0)

        return lines


def _short(text: str, limit: int = 160) -> str:
    one_line = " ".join(str(text or "").split())
    return one_line if len(one_line) <= limit else one_line[:limit] + "…"


def from_messages(messages: list[Msg], timestamps: list[int] | None = None) -> ExtractContext:
    return ExtractContext(
        messages=list(messages),
        timestamps=list(timestamps or []),
        session_started_at=(timestamps or [0])[0] if timestamps else 0,
    )
