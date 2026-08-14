"""
提取的输入准备：决定"哪些消息参与本次提取"。

## 为什么要截断

三个独立的原因，各自需要不同的处理：

1. **正在进行的对话不该被总结。** 最近几轮还没结束，提取出来的事件
   会是"他开始做 X"而不是"他做完了 X"。按 user 消息分轮，保留末尾几轮。
2. **单条消息可能极长。** 一次 read_file 的结果能有几万字符。整段塞给
   提取模型会把窗口占满，而记忆需要的是"读了哪个文件"而不是文件内容。
3. **总量有上限。** 一个长会话的全部消息可能超过提取模型的窗口。

## 为什么不复用上下文压缩的 fit_to_budget

压缩的目标是"让对话能继续",所以它保留最近的、丢弃最早的。
提取的目标是"从已结束的部分学习",它要的恰好是**被压缩丢弃的那部分**。
两者的取舍方向相反，共用一个函数会让其中一个的语义被另一个带歪。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from app.core.config import settings
from app.modules.agent.messages import Msg

log = structlog.get_logger(__name__)

# 单条消息的正文上限的【兜底值】。实际取 settings.memory.max_msg_chars。
#
# 保留头尾而非只保留开头：工具结果的【结尾】往往是结论
# （"3 passed" / "error: xxx"），只留开头会把结论切掉。
MAX_MSG_CHARS = 1_200

# 截断标记。让提取模型知道这里有省略，不要以为文件就这么短。
TRUNCATE_MARK = "\n…（省略 {n} 字符）…\n"


@dataclass
class Turn:
    """
    一轮对话：一条 user 消息 + 它引发的全部 assistant/tool 消息。

    以 user 消息为边界而非以 assistant 为边界：用户的一句话是一个意图，
    而 assistant 可能为了这个意图调十次工具。按 assistant 分轮会把
    一个完整任务切碎。
    """

    messages: list[Msg] = field(default_factory=list)
    # 在原始列表中的下标区间（闭区间），用于生成 ranges
    start: int = 0
    end: int = 0

    @property
    def chars(self) -> int:
        return sum(len(m.content or "") for m in self.messages)


def build_turns(messages: list[Msg]) -> list[Turn]:
    """
    切分成轮。第一条 user 之前的消息（system / 历史摘要）归入第 0 轮。

    空列表返回空列表 —— 不造一个空轮，那会让下游的"有没有内容"判断失效。
    """
    turns: list[Turn] = []
    current: Turn | None = None

    for i, msg in enumerate(messages):
        if msg.role == "user":
            if current is not None:
                turns.append(current)
            current = Turn(messages=[msg], start=i, end=i)
            continue

        if current is None:
            # 第一条 user 之前的内容。归入一个先导轮。
            current = Turn(messages=[msg], start=i, end=i)
        else:
            current.messages.append(msg)
            current.end = i

    if current is not None:
        turns.append(current)
    return turns


@dataclass
class ExtractInput:
    """一次提取的输入。"""

    # 参与提取的消息（已截断长消息）
    messages: list[Msg] = field(default_factory=list)
    # 对应的时间戳，与 messages 一一对应
    timestamps: list[int] = field(default_factory=list)
    # 被保留（不参与本次提取）的轮数
    held_back_turns: int = 0
    # 因总量超限被丢弃的轮数
    dropped_turns: int = 0
    # 被截断的消息条数
    truncated_messages: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.messages

    @property
    def total_chars(self) -> int:
        return sum(len(m.content or "") for m in self.messages)


def prepare(
    messages: list[Msg],
    timestamps: list[int] | None = None,
    *,
    keep_recent_turns: int | None = None,
    max_total_chars: int | None = None,
    max_msg_chars: int | None = None,
) -> ExtractInput:
    """
    准备提取输入。

    ## 顺序很重要

    先按轮保留末尾 → 再按总量从【最早】开始丢 → 最后截断长消息。

    为什么按总量丢弃时丢最早的：一次提取如果装不下全部历史，
    较新的内容更可能还没被提取过。最早的部分很可能上次已经提取了。

    ## 为什么截断放最后

    截断改变的是字符数。放在总量裁剪之前的话，"总量"算的是截断后的值，
    于是能塞进更多轮 —— 听起来是好事，但那让"这次提取覆盖了哪几轮"
    变得依赖于消息长度，不可预测。先定轮次范围，再压缩每条内容。
    """
    keep = settings.memory.keep_recent_turns if keep_recent_turns is None else keep_recent_turns
    # 参数为 None 时读配置 —— 让用户能在前端调，同时测试仍可显式覆盖。
    if max_total_chars is None:
        max_total_chars = settings.memory.max_conversation_chars
    if max_msg_chars is None:
        max_msg_chars = settings.memory.max_msg_chars
    stamps = list(timestamps or [])

    turns = build_turns(messages)
    if not turns:
        return ExtractInput()

    # 1. 末尾几轮不参与 —— 它们还没结束
    usable = turns[:-keep] if keep > 0 else list(turns)
    held_back = len(turns) - len(usable)
    if not usable:
        log.info("extract_input_all_held_back", total_turns=len(turns), keep_recent=keep)
        return ExtractInput(held_back_turns=held_back)

    # 2. 总量超限时从最早开始丢
    dropped = 0
    while usable and sum(t.chars for t in usable) > max_total_chars:
        usable.pop(0)
        dropped += 1

    if not usable:
        log.warning("extract_input_all_dropped_by_budget", max_total_chars=max_total_chars)
        return ExtractInput(held_back_turns=held_back, dropped_turns=dropped)

    # 3. 截断长消息
    picked: list[Msg] = []
    picked_stamps: list[int] = []
    truncated = 0
    for turn in usable:
        for offset, msg in enumerate(turn.messages):
            new_msg, was_cut = _truncate(msg, max_msg_chars)
            picked.append(new_msg)
            idx = turn.start + offset
            picked_stamps.append(stamps[idx] if idx < len(stamps) else 0)
            truncated += int(was_cut)

    log.info(
        "extract_input_prepared",
        turns_total=len(turns),
        turns_used=len(usable),
        held_back=held_back,
        dropped=dropped,
        messages=len(picked),
        truncated=truncated,
        chars=sum(len(m.content or "") for m in picked),
    )
    return ExtractInput(
        messages=picked,
        timestamps=picked_stamps,
        held_back_turns=held_back,
        dropped_turns=dropped,
        truncated_messages=truncated,
    )


def _truncate(msg: Msg, limit: int) -> tuple[Msg, bool]:
    """
    截断单条消息的正文，保留头尾。

    返回【新对象】而不是改原对象：原始 Msg 可能还被别处引用
    （调用方拿它做别的事），原地改会造成难查的副作用。
    """
    content = msg.content or ""
    if len(content) <= limit:
        return msg, False

    head = limit * 2 // 3
    tail = limit - head
    omitted = len(content) - head - tail
    new_content = content[:head] + TRUNCATE_MARK.format(n=omitted) + content[-tail:]

    return (
        Msg(
            role=msg.role,
            content=new_content,
            reasoning=msg.reasoning,
            tool_calls=list(msg.tool_calls),
            tool_call_id=msg.tool_call_id,
            tool_name=msg.tool_name,
            is_error=msg.is_error,
            agent_name=msg.agent_name,
            message_id=msg.message_id,
        ),
        True,
    )
