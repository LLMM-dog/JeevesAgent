"""
cron 表达式计算。

## 为什么单独一个模块

调度器、接口校验、错过检测三处都要算时间。放一起的话逻辑会各自漂移 ——
而"下一次什么时候触发"这种问题必须只有一个答案。

## 为什么只用 croniter 而不用 APScheduler

APScheduler 功能全（持久化、多种触发器、错过补偿），但它自带一套
job store + executor 模型，与本项目已有的"数据库为准 + asyncio task"
结构重叠 —— 接进来要写适配层，而适配层的 bug 比自己写调度更难查。

croniter 只做一件事：算下一个/上一个匹配时间点。那正是需要的。
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

log = structlog.get_logger(__name__)


def get_tz(name: str) -> dt.tzinfo:
    """
    时区名 → tzinfo。空或非法时回落本地时区。

    ## 为什么不抛异常

    时区名是存在库里的。数据库里有一条非法时区的任务时，抛异常会让
    **整个调度器启动失败** —— 一条坏数据废掉所有任务。

    回落本地时区 + 记 warning 是更好的取舍：那条任务的触发时间可能不对，
    但其它任务照常工作。
    """
    if not name:
        return dt.datetime.now().astimezone().tzinfo or dt.UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as e:
        log.warning("cron_bad_timezone", tz=name, err=str(e)[:120])
        return dt.datetime.now().astimezone().tzinfo or dt.UTC


def validate(expr: str) -> str:
    """
    校验 cron 表达式。返回错误说明，合法时返回空字符串。

    ## 为什么在入口校验

    只在调度时 catch CroniterBadCronError
    —— 所以非法表达式能存进数据库，然后每次调度都抛异常，
    而用户以为任务建好了。

    错误信息要具体到"哪里不对"，不能只说 invalid cron ——
    用户看不出自己写的 `0 9 * *` 少了一段。
    """
    expr = (expr or "").strip()
    if not expr:
        return "cron 表达式不能为空"

    parts = expr.split()
    if len(parts) not in (5, 6):
        return (
            f"cron 表达式应有 5 段（分 时 日 月 周），收到 {len(parts)} 段：{expr!r}。"
            '例如每天 9:00 是 "0 9 * * *"'
        )

    try:
        from croniter import croniter
    except ImportError:
        return "缺少 croniter 依赖，装：uv sync --extra cron"

    if not croniter.is_valid(expr):
        return f"cron 表达式非法：{expr!r}"
    return ""


def next_after(expr: str, tz_name: str, after: dt.datetime | None = None) -> dt.datetime:
    """
    下一个触发时间（tz-aware）。

    ## 为什么必须传 tz-aware 起点给 croniter

    naive datetime 在 DST 切换日会出错：春季跳过的那小时里的任务不触发，
    秋季重复的那小时里的任务触发两次。

    croniter 原生支持 tz-aware，用上几乎没有额外成本 ——
    而另一种做法 全程 naive（全文搜 tz/timezone/astimezone 零命中）。
    """
    from croniter import croniter

    tz = get_tz(tz_name)
    base = (after or dt.datetime.now(tz)).astimezone(tz)
    return croniter(expr, base).get_next(dt.datetime)


def prev_before(expr: str, tz_name: str, before: dt.datetime | None = None) -> dt.datetime:
    """
    上一个应该触发的时间（tz-aware）。

    ## 用途：错过检测

    启动时算出"上一个应该触发的时间点"，如果它晚于 `last_fired_at`，
    说明在服务没运行的那段时间里错过了一次。

    没有这个概念 —— 它重启后直接算下一个时间点，错过的完全消失。
    """
    from croniter import croniter

    tz = get_tz(tz_name)
    base = (before or dt.datetime.now(tz)).astimezone(tz)
    return croniter(expr, base).get_prev(dt.datetime)


def to_ms(d: dt.datetime) -> int:
    """tz-aware datetime → UTC 毫秒。库里一律存毫秒整数。"""
    return int(d.timestamp() * 1000)


def from_ms(ms: int, tz_name: str = "") -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000, tz=get_tz(tz_name))


def describe(expr: str) -> str:
    """
    把常见表达式翻成中文，给界面显示用。

    不做通用的 cron→自然语言（那需要一个专门的库，且中文表达很难做对）。
    只覆盖最常见的几种形状，其余原样返回 —— 显示原表达式比显示
    一句拗口的错误翻译好。
    """
    parts = (expr or "").strip().split()
    if len(parts) != 5:
        return expr
    mi, h, dom, mon, dow = parts

    if mon == "*" and dom == "*" and dow == "*" and mi.isdigit() and h.isdigit():
        return f"每天 {int(h):02d}:{int(mi):02d}"
    if mon == "*" and dom == "*" and mi.isdigit() and h.isdigit() and dow.isdigit():
        names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        # cron 里 0 是周日
        idx = (int(dow) - 1) % 7
        return f"每{names[idx]} {int(h):02d}:{int(mi):02d}"
    if h == "*" and mi.isdigit() and dom == "*" and mon == "*" and dow == "*":
        return f"每小时的第 {int(mi)} 分钟"
    if mi.startswith("*/") and h == "*":
        n = mi[2:]
        if n.isdigit():
            return f"每 {n} 分钟"
    return expr
