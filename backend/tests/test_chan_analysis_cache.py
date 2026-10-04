"""单票缠论结果缓存 + 涨停阈值走市场注册表。

缠论: K 线面板每次打开/切周期都会重打 /api/chan/analysis, 缓存按数据指纹失效。
涨停阈值: 板块口径收敛到 app.markets, 港美股无涨跌停 (不能退回 A 股主板 10%)。
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.api import chan as chan_api
from app.pulse.auction import _limit_up_threshold


def _ohlc_df(n: int = 120) -> pl.DataFrame:
    end = date.today()
    return pl.DataFrame(
        {
            "date": [end - timedelta(days=n - 1 - i) for i in range(n)],
            "high": [11.0 + (i % 9) * 0.1 for i in range(n)],
            "low": [9.0 + (i % 5) * 0.1 for i in range(n)],
            "close": [10.0 + (i % 7) * 0.1 for i in range(n)],
        }
    )


def test_chan_analysis_computed_once_per_fingerprint(monkeypatch):
    """同标的同参数 -> 第二次命中缓存, analyze 不再跑。"""
    chan_api.clear_analysis_cache()
    calls: list[int] = []

    def fake_analyze(high, low, close, strict=True):
        calls.append(1)
        return "SENTINEL"

    monkeypatch.setattr(chan_api, "analyze", fake_analyze)

    df = _ohlc_df(120)
    first = chan_api._analyze_cached("600000.SH", 400, True, df)
    second = chan_api._analyze_cached("600000.SH", 400, True, df)
    assert first is second == "SENTINEL"
    assert len(calls) == 1


def test_chan_analysis_cache_key_covers_strict_and_boundary(monkeypatch):
    """笔口径 / 数据边界任一变化都要重算。"""
    chan_api.clear_analysis_cache()
    calls: list[int] = []

    def fake_analyze(high, low, close, strict=True):
        calls.append(1)
        return "SENTINEL"

    monkeypatch.setattr(chan_api, "analyze", fake_analyze)

    df = _ohlc_df(120)
    chan_api._analyze_cached("600000.SH", 400, True, df)
    chan_api._analyze_cached("600000.SH", 400, False, df)  # 宽松笔
    chan_api._analyze_cached("600000.SH", 200, True, df)  # 不同 lookback
    chan_api._analyze_cached("600000.SH", 400, True, df.head(90))  # 数据变短
    assert len(calls) == 4


def test_limit_up_threshold_follows_market_registry():
    """板块阈值来自 markets/price_limits, 与全仓库同源。"""
    assert _limit_up_threshold("600519.SH") == 0.098  # 主板 10%
    assert _limit_up_threshold("300750.SZ") == 0.198  # 创业板 20%
    assert _limit_up_threshold("688001.SH") == 0.198  # 科创板 20%
    assert _limit_up_threshold("920002.BJ") == 0.298  # 北交所 30%


def test_limit_up_threshold_is_infinite_without_limit():
    """港美股无涨跌停: 判定恒 False, 而不是退回 A 股主板阈值。"""
    assert _limit_up_threshold("00700.HK") == float("inf")
    assert _limit_up_threshold("AAPL.US") == float("inf")
