"""
追踪落库：run（一次执行）与 span（一步操作）。

## 常见实现没有真正的 span 表

| | 现状 |
| --- | --- |
| | 把 model/duration/total_tokens 塞进 `messages.info` JSON 列 |
| | 只有 ContextVar 传的 `trace_id` + 滚动日志，**零持久化** |
| pi | span 设计写满两份文档，`src` 下**一行未实现** |

所以这块基本是自己设计。但有三条是从它们的缺陷里直接抄来的结论。

## 一、token 必须从第一天就按维度拆开

只存 `total_tokens`，后果是**成本永远算不出、历史无法回溯** ——
这个错误不可逆，因为原始数据没留下。 同类实现拆了六个维度，直接照搬。
它踩过的三个坑也照抄结论：

- `reasoning` 是 `output` 的**子集**，不要重复加进 total
- 供应商不报的字段用 `NULL` 而非 `0` —— 要能区分"未知"和"确实是零"
- 缓存写入按保留期分档计价，长保留可达 input 单价的 2 倍

## 二、单价存快照

价格会变。历史成本必须可复算，所以每行 span 存下当时用的单价，
不是查表现算。

## 三、duration 缺失就写 NULL

相关实现 用 `or current_timestamp` 兜底，
缺少起始时间时会**静默产出 0**。0 和"不知道"是两件事。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.infra.db.base import Base, TimestampMixin


class Run(Base, TimestampMixin):
    """
    一次执行 = 一条用户输入 + 由它触发的全部 AI 活动。

    run_id 的语义取 generation_id 定义（`init_mysql.sql:108`）。
    """

    __tablename__ = "run"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(32), nullable=False)

    # 子代理的 run 挂在父 run 下。用于成本上卷 ——
    # 常见实现答不出"这次任务总共花了多少钱"，因为它们要么在前端
    # 内存累加，要么在展示层重建（pi），要么没实现。
    parent_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    agent_name: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    stop_reason: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    started_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ended_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 缺失写 NULL，不写 0
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    turns: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ── token：六个维度，不报的字段留 NULL ──
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # reasoning 是 output 的子集，【不要】再加进 total
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # 含子 run 的累计。子 run 结束时原子累加上来。
    rollup_total_tokens: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    rollup_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    error: Mapped[str] = mapped_column(Text, default="", nullable=False)

    __table_args__ = (
        Index("ix_run_session_started", "session_id", "started_at"),
        Index("ix_run_parent", "parent_run_id"),
        # 供 TTL 清理用
        Index("ix_run_created", "created_at"),
    )


class Span(Base, TimestampMixin):
    """
    一步操作。llm / tool / agent / compaction 四类。

    ## 为什么 preview 要连"被截断过"一起存

    只存截断后的内容的话，读的人无法判断"这就是全部"还是"还有更多"。 同类实现 把这个信息显式带出来，照抄。
    """

    __tablename__ = "span"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False)
    session_id: Mapped[str] = mapped_column(String(32), nullable=False)
    parent_span_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    depth: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    agent_name: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    status: Mapped[str] = mapped_column(String(16), default="ok", nullable=False)
    started_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ended_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── 输入输出：截断后存，同时记录截断事实 ──
    input_preview: Mapped[str] = mapped_column(Text, default="", nullable=False)
    input_truncated: Mapped[bool] = mapped_column(default=False, nullable=False)
    input_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_preview: Mapped[str] = mapped_column(Text, default="", nullable=False)
    output_truncated: Mapped[bool] = mapped_column(default=False, nullable=False)
    output_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ── llm 类 span 专用 ──
    model_id: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    provider_name: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # 单价快照。价格会变，历史成本必须可复算 ——
    # 查表现算的话改一次价格，所有历史账都变了。
    price_in_per_1m: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_out_per_1m: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    error: Mapped[str] = mapped_column(Text, default="", nullable=False)

    __table_args__ = (
        Index("ix_span_run_started", "run_id", "started_at"),
        Index("ix_span_parent", "parent_span_id"),
        Index("ix_span_session", "session_id"),
        Index("ix_span_created", "created_at"),
    )
