"""
定时任务的表。

## 两张表

- `cron_task`：任务定义
- `cron_run`：执行历史

## 为什么执行历史必须落库

定时任务是**无人值守**的 —— 触发时用户可能根本没打开浏览器。
出问题时唯一的线索就是历史记录。

只用 `logger.write_log` 写文件，
查起来要翻日志文件，而且没有"这次触发对应哪个会话"的关联。

## 为什么不做 execute 字段

任务有个 `execute` 字段，存一段 Python 代码，用 `exec()` 加完整
`__builtins__` 在 agent 进程里跑（`_compile_execute`）——
`import os; os.system(...)` 完全可用。

它比 `run_python` 更危险：`run_python` 的代码是模型当场生成、用户在审批框里
能看到；`execute` 是**存在库里的**，创建时看一眼，之后每天自动跑，
**没有任何审批环节**。

本项目不做这个字段。需要"先取数据再分析"的话，让 agent 自己在对话里调
`run_shell` / `web_fetch` —— 那些已经过沙箱和审批。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.db.base import Base, TimestampMixin

# 错过窗口的处理策略。
#
# 默认 skip：服务停了三天的话，run_once 会在启动瞬间触发一次 ——
# 而如果是每小时的任务，补偿逻辑一不小心就变成触发 72 次。
# 而且昨天的日报今天补出来意义不大。
ON_MISSED_SKIP = "skip"
ON_MISSED_RUN = "run_once"

RUN_OK = "ok"
RUN_FAILED = "failed"
RUN_MISSED = "missed"
RUN_RUNNING = "running"


class CronTask(Base, TimestampMixin):
    __tablename__ = "cron_task"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    # 到点后发给 agent 的这条消息。
    #
    # 定时任务只有这一种行为：创建一个会话、把这条消息发进去、
    # 跑完整的 agent 循环。不做 execute 代码字段（见模块 docstring）。
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    # 标准五段 cron 表达式。
    #
    # 【创建时必须校验】—— 只在调度时 catch CroniterBadCronError
    # ，所以非法表达式能存进库，
    # 然后每次调度都抛异常，而用户以为任务建好了。
    cron: Mapped[str] = mapped_column(String(120), nullable=False)
    # IANA 时区名，如 Asia/Shanghai。
    #
    # 必须存时区而不是用服务器本地时区：naive datetime 在 DST 切换日会出错
    # —— 春季跳过的那小时任务不触发，秋季重复的那小时触发两次。
    # 全程 naive（全文搜 tz/timezone 零命中）。
    timezone: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    # 在哪个工作区执行。任务触发的会话绑到它。
    workspace_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    enabled: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    on_missed: Mapped[str] = mapped_column(
        String(16), default=ON_MISSED_SKIP, nullable=False
    )
    # 上一次实际触发的时间（毫秒）。
    #
    # 【这是错过检测的依据】：启动时算出"上一个应该触发的时间点"，
    # 如果它晚于 last_fired_at，说明在服务没运行的那段时间里错过了。
    #
    # 没有这个字段 —— 它重启后直接算"下一个"时间点，
    # 错过的那次完全消失且无任何记录。用户视角是"我的日报今天没发"，
    # 而没有线索指向"服务当时没在跑"。
    last_fired_at: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    # 冗余存下一次触发时间，只为了列表页显示。
    # 调度器不依赖它（每次都用 croniter 现算），所以它过期也不影响正确性。
    next_fire_at: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    run_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fail_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        # 启动时要拉"所有启用的任务"
        Index("ix_cron_task_enabled", "enabled"),
    )


class CronRun(Base, TimestampMixin):
    """
    一次执行的记录。

    ## 为什么要关联 session_id

    任务触发的对话是一个正常会话 —— 用户要能点进去看 agent 到底做了什么。

    没有这个关联的话，用户看到"任务执行成功"但不知道结果在哪，
    而会话列表里会莫名多出一个他没发起过的对话。
    """

    __tablename__ = "cron_run"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("cron_task.id", ondelete="CASCADE"), nullable=False
    )
    # 计划触发时间 vs 实际开始时间。
    #
    # 两个都记：差值能看出调度延迟（比如被别的任务挤了、或者是补偿执行）。
    scheduled_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    started_at: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    finished_at: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    # ok | failed | missed | running
    status: Mapped[str] = mapped_column(String(16), default=RUN_RUNNING, nullable=False)
    # 失败原因，或 missed 的说明。给用户看的，要能读懂。
    detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 不加外键：会话可能被用户删掉，而执行历史应该留着。
    # 加了 CASCADE 的话删会话会连带删掉历史记录，
    # 而那正是用户想查"上周的任务跑了吗"时需要的东西。
    session_id: Mapped[str] = mapped_column(String(32), default="", nullable=False)

    __table_args__ = (
        # 列表页按任务查历史，按时间倒序
        Index("ix_cron_run_task_time", "task_id", "scheduled_at"),
    )
