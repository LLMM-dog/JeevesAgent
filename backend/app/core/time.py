"""
统一时间源。

全项目只允许通过 now_ms() 取当前时间。散落的 datetime.now() 会出现三种写法
（本地时区/UTC/秒），排查时间相关 bug 时无从下手。测试里也只需 patch 这一处。

所有时间戳都是 UTC 毫秒整数，字段名以 _at 结尾。
不用 SQLite 的 DATETIME（它实际是文本，时区语义模糊），
不用秒（前端 JS 天然用毫秒，转换是多余的出错点）。
"""

import datetime as _dt

_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def now_ms() -> int:
    return int(_dt.datetime.now(_dt.UTC).timestamp() * 1000)


def to_datetime(ms: int) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.UTC)


def to_local(ms: int | None = None) -> _dt.datetime:
    """
    UTC 毫秒 → 本地时区的 datetime。ms 为 None 或 0 时取当前时间。

    ## 为什么记忆的日期路径必须用本地时间

    事件按 <年>/<月>/<日> 分目录。用 UTC 的话，本地时间晚上 8 点之后
    发生的事会被归到"明天"的目录里（UTC+8 时区）—— 用户按日期找事件时
    在他记得的那天里找不到。

    时区相关的判断都该走这个函数，不要在业务模块里自己做时区换算。
    """
    if not ms:
        return _dt.datetime.now()
    return to_datetime(ms).astimezone()


def weekday_cn(dt: _dt.datetime) -> str:
    """中文星期。给记忆正文用 —— 事件里标"星期几"比标日期更容易对上记忆。"""
    return "星期" + "一二三四五六日"[dt.weekday()]


def local_stamp() -> str:
    """
    给模型看的当前本地时间，如「（2026-08-02 Sun 14:30）」。

    追加在用户消息末尾，让模型能感知当前时间 —— 否则它会用训练截止日期
    来推算"今天"，在涉及日期的任务上必然出错。

    用本地时间而非 UTC：用户说"今天"指的是他所在时区的今天。
    """
    now = _dt.datetime.now()
    wd = _WEEKDAYS[now.weekday()]
    return f"（{now.year:04d}-{now.month:02d}-{now.day:02d} {wd} {now.hour:02d}:{now.minute:02d}）"
