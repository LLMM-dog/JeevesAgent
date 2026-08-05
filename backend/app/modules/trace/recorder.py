"""
把 span 记进库的上下文管理器。

## 为什么不直接改 trace_context.new_span

`core/trace_context.py` 是纯上下文传递，不依赖任何模块（events、db 都不依赖）。
让它去写库会产生 `core → modules` 的反向依赖 —— 而 core 是被所有模块依赖的
最底层。

所以这里包一层：`new_span` 管上下文，`record_span` 管落库。
需要落库的地方用后者，不需要的地方（比如纯粹为了 span_id 的场景）用前者。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from app.core.time import now_ms
from app.core.trace_context import SpanInfo, SpanKind, current_run_id, new_span
from app.modules.trace.writer import SpanRecord, submit


@dataclass
class SpanSink:
    """
    span 执行期间往里填数据。

    结束时一次性提交 —— 中途多次提交的话，一个 span 会写出多行。
    """

    info: SpanInfo
    input_text: str = ""
    output_text: str = ""
    model_id: str = ""
    provider_name: str = ""
    status: str = "ok"
    error: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    price_in_per_1m: float | None = None
    price_out_per_1m: float | None = None

    def set_usage(
        self,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_read: int | None = None,
        cache_write: int | None = None,
        reasoning: int | None = None,
        total: int | None = None,
    ) -> None:
        self.usage = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "reasoning_tokens": reasoning,
            # total 不传时用 input+output 算，【不加 reasoning】——
            # 它是 output 的子集，加了就重复计费。
            "total_tokens": (
                total
                if total is not None
                else (input_tokens or 0) + (output_tokens or 0)
            ),
        }


def compute_cost(
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    price_in_per_1m: float | None,
    price_out_per_1m: float | None,
) -> float:
    """
    算成本。任一单价缺失时返回 0.0。

    单价 NULL 表示"没配价"，不是免费。返回 0 是无奈之选，
    但报表里能靠 price 字段是否为 NULL 区分出来 ——
    所以【单价必须一起存进 span】，不能只存算好的 cost。
    """
    if price_in_per_1m is None and price_out_per_1m is None:
        return 0.0
    cost = 0.0
    if price_in_per_1m is not None and input_tokens:
        cost += input_tokens / 1_000_000 * price_in_per_1m
    if price_out_per_1m is not None and output_tokens:
        cost += output_tokens / 1_000_000 * price_out_per_1m
    return round(cost, 8)


@contextmanager
def record_span(
    kind: SpanKind,
    name: str,
    *,
    session_id: str,
    agent_name: str = "",
    run_id: str = "",
) -> Iterator[SpanSink]:
    """
    进入一个 span 并在退出时落库。

    异常照常向上抛 —— 追踪不改变控制流。但 span 会先被标成 error 记下来，
    这正是排查时最需要的那条记录。
    """
    with new_span(kind, name) as info:
        sink = SpanSink(info=info)
        started = now_ms()
        try:
            yield sink
        except BaseException as e:
            sink.status = "error"
            if not sink.error:
                sink.error = f"{type(e).__name__}: {e}"
            raise
        finally:
            ended = now_ms()
            u = sink.usage
            cost = compute_cost(
                input_tokens=u.get("input_tokens"),
                output_tokens=u.get("output_tokens"),
                price_in_per_1m=sink.price_in_per_1m,
                price_out_per_1m=sink.price_out_per_1m,
            )
            submit(
                SpanRecord(
                    id=info.span_id,
                    run_id=run_id or current_run_id() or "",
                    session_id=session_id,
                    parent_span_id=info.parent_span_id,
                    depth=info.depth,
                    kind=kind,
                    name=name,
                    agent_name=agent_name,
                    status=sink.status,
                    started_at=started,
                    ended_at=ended,
                    duration_ms=ended - started,
                    input_text=sink.input_text,
                    output_text=sink.output_text,
                    model_id=sink.model_id,
                    provider_name=sink.provider_name,
                    input_tokens=u.get("input_tokens"),
                    output_tokens=u.get("output_tokens"),
                    cache_read_tokens=u.get("cache_read_tokens"),
                    cache_write_tokens=u.get("cache_write_tokens"),
                    reasoning_tokens=u.get("reasoning_tokens"),
                    total_tokens=int(u.get("total_tokens") or 0),
                    price_in_per_1m=sink.price_in_per_1m,
                    price_out_per_1m=sink.price_out_per_1m,
                    cost_usd=cost,
                    error=sink.error,
                )
            )
