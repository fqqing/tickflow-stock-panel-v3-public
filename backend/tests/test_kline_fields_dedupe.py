"""fields 白名单去重回归：重复字段曾触发 polars DuplicateError 500。

前端「信号」开关打开时, klineChartFields(true) 的信号列清单本就含
signal_limit_up / signal_broken_limit_up, 再拼上 LIMIT_UP_FIELDS 会交来
重复列名 —— polars select 直接 DuplicateError(实测 500)。后端在
_select_fields_df 里去重保序兜底(前端也已同步去重, 见 api.ts)。
"""

from __future__ import annotations

import polars as pl

from app.api.kline import _select_fields_df


def _df() -> pl.DataFrame:
    return pl.DataFrame({
        "date": ["2026-09-29", "2026-09-30"],
        "close": [31.33, 31.13],
        "signal_limit_up": [False, False],
        "consecutive_limit_ups": [0, 0],
    })


def test_duplicate_fields_do_not_raise():
    df = _df()
    out = _select_fields_df(df, "close,signal_limit_up,close,signal_limit_up")
    assert out.columns == ["close", "signal_limit_up"]
    assert out.height == 2


def test_duplicate_fields_keep_first_occurrence_order():
    df = _df()
    out = _select_fields_df(df, "signal_limit_up,close,signal_limit_up,consecutive_limit_ups")
    assert out.columns == ["signal_limit_up", "close", "consecutive_limit_ups"]


def test_unknown_fields_are_dropped_before_dedupe():
    df = _df()
    out = _select_fields_df(df, "close,nope,close,nope")
    assert out.columns == ["close"]
