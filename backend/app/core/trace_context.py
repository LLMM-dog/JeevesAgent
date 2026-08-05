"""
span 上下文传递。

span 三件套（span_id / parent_span_id / depth）通过 ContextVar 传递，
不需要调用方一层层传参。任意深度的代码 emit 事件时自动带上正确的 span 信息。

落库的 trace 树与推给前端的气泡树【结构同源】—— 同一套 span 数据，
一份写 span 表，一份走 SSE。不维护两套。
"""

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

from app.core.ids import span_id as new_span_id

SpanKind = Literal["llm", "tool", "agent", "compaction"]


@dataclass(frozen=True)
class SpanInfo:
    span_id: str
    parent_span_id: str | None
    depth: int
    kind: SpanKind
    name: str


_current_span: contextvars.ContextVar[SpanInfo | None] = contextvars.ContextVar(
    "current_span", default=None
)
_current_run: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_run", default=None
)


def current_span() -> SpanInfo | None:
    return _current_span.get()


def current_run_id() -> str | None:
    return _current_run.get()


@contextmanager
def run_scope(run_id: str) -> Iterator[None]:
    token = _current_run.set(run_id)
    try:
        yield
    finally:
        _current_run.reset(token)


@contextmanager
def new_span(kind: SpanKind, name: str) -> Iterator[SpanInfo]:
    """
    进入一个新的执行单元。里面 emit 的所有事件自动带上新 span_id 和正确的 parent。

    depth 从父 span 递增。agent 类型的 span 的 depth 就是 subagent 嵌套深度。
    """
    parent = _current_span.get()
    info = SpanInfo(
        span_id=new_span_id(),
        parent_span_id=parent.span_id if parent else None,
        depth=(parent.depth + 1) if parent else 0,
        kind=kind,
        name=name,
    )
    token = _current_span.set(info)
    try:
        yield info
    finally:
        _current_span.reset(token)
