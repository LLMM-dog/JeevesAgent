"""
时间函数测试。
"""

from __future__ import annotations

import datetime as _dt

from app.core.time import local_stamp, now_ms, to_datetime


class TestNowMs:
    def test_returns_int(self) -> None:
        assert isinstance(now_ms(), int)

    def test_returns_positive(self) -> None:
        assert now_ms() > 0

    def test_monotonically_increasing(self) -> None:
        t1 = now_ms()
        t2 = now_ms()
        assert t2 >= t1

    def test_reasonable_range(self) -> None:
        """返回值在合理范围内（2020-2099 年）。"""
        ms = now_ms()
        min_2020 = _dt.datetime(2020, 1, 1, tzinfo=_dt.UTC).timestamp() * 1000
        max_2099 = _dt.datetime(2099, 1, 1, tzinfo=_dt.UTC).timestamp() * 1000
        assert min_2020 < ms < max_2099


class TestToDatetime:
    def test_roundtrip(self) -> None:
        ms = now_ms()
        dt = to_datetime(ms)
        assert dt.tzinfo == _dt.UTC

    def test_zero_epoch(self) -> None:
        dt = to_datetime(0)
        assert dt == _dt.datetime(1970, 1, 1, tzinfo=_dt.UTC)

    def test_precision(self) -> None:
        """毫秒精度保留。"""
        ms = 1234567890123
        dt = to_datetime(ms)
        expected = _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.UTC)
        assert dt == expected
        assert dt.microsecond == 123000


class TestLocalStamp:
    def test_returns_non_empty_string(self) -> None:
        s = local_stamp()
        assert len(s) > 0

    def test_contains_chinese_parens(self) -> None:
        """格式如 （2026-08-02 Sun 14:30）。"""
        s = local_stamp()
        assert s.startswith("（")
        assert s.endswith("）")

    def test_contains_weekday(self) -> None:
        s = local_stamp()
        weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        assert any(w in s for w in weekdays)

    def test_contains_hour_minute(self) -> None:
        s = local_stamp()
        import re

        assert re.search(r"\d{2}:\d{2}", s), f"缺少时分: {s}"
