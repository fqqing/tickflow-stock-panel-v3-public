"""筹码分布(成本分布)单测 —— 纯数学, 不依赖仓储。"""
from __future__ import annotations

import polars as pl
import pytest

from app.indicators.chips import compute_chips


def _df(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows)


def _one_day(low=10.0, high=12.0, close=11.0, volume=1000.0, amount=None, turnover=10.0):
    amt = amount if amount is not None else volume * 100 * ((low + high) / 2)
    return {
        "date": "2026-09-30", "open": low, "high": high, "low": low,
        "close": close, "volume": volume, "amount": amt, "turnover_rate": turnover,
    }


def test_empty_frame_returns_ok_false():
    out = compute_chips(_df([]))
    assert out["ok"] is False
    assert out["bins"] == []


def test_missing_columns_returns_ok_false():
    out = compute_chips(_df([{"date": "2026-09-30", "close": 10.0}]))
    assert out["ok"] is False


def test_single_day_peak_at_avg_price():
    """单日: 筹码峰必须落在当日均价(而非收盘价)。"""
    df = _df([_one_day(low=10.0, high=20.0, close=10.5, volume=1000.0, amount=1000 * 100 * 15.0)])
    out = compute_chips(df, bins=40)
    assert out["ok"] is True
    peak = max(out["bins"], key=lambda b: b["ratio"])
    assert peak["price"] == pytest.approx(15.0, abs=out["step"])


def test_ratios_sum_to_one():
    out = compute_chips(_df([_one_day(), _one_day(low=11.0, high=13.0, close=12.0)]), bins=30)
    assert sum(b["ratio"] for b in out["bins"]) == pytest.approx(1.0, abs=1e-6)


def test_full_turnover_wipes_old_chips():
    """换手 100% 时老筹码清零 —— 只剩最后一天的筹码。"""
    rows = [
        _one_day(low=10.0, high=11.0, close=10.5, volume=1000.0),
        _one_day(low=20.0, high=21.0, close=20.5, volume=1000.0, turnover=100.0),
    ]
    out = compute_chips(_df(rows), bins=40)
    low_half = sum(b["ratio"] for b in out["bins"] if b["price"] < 15)
    high_half = sum(b["ratio"] for b in out["bins"] if b["price"] >= 15)
    assert low_half < 0.01, f"老筹码没被换手清掉: {low_half}"
    assert high_half > 0.99


def test_zero_turnover_keeps_everything():
    """换手 0 时不衰减: 两天的量都还在。"""
    rows = [
        _one_day(low=10.0, high=11.0, close=10.5, volume=1000.0, turnover=0.0),
        _one_day(low=20.0, high=21.0, close=20.5, volume=1000.0, turnover=0.0),
    ]
    out = compute_chips(_df(rows), bins=40)
    low_half = sum(b["ratio"] for b in out["bins"] if b["price"] < 15)
    assert low_half == pytest.approx(0.5, abs=0.02)


def test_profit_ratio_and_avg_cost():
    """价格都低于现价 => 获利盘 100%; 对称三角的重心 = 峰位(均价)。"""
    df = _df([_one_day(low=10.0, high=20.0, close=20.0, volume=1000.0, amount=1000 * 100 * 15.0)])
    out = compute_chips(df, bins=50)
    assert out["close"] == 20.0
    assert out["profit_ratio"] == pytest.approx(1.0, abs=1e-6)
    assert out["avg_cost"] == pytest.approx(15.0, abs=0.3)


def test_avg_cost_follows_peak_not_center():
    """重心跟着均价走: 峰压到 12 时, 平均成本应落在 12 与区间中点之间(约 13.9)。"""
    df = _df([_one_day(low=10.0, high=20.0, close=20.0, volume=1000.0, amount=1000 * 100 * 12.0)])
    out = compute_chips(df, bins=50)
    assert 12.0 < out["avg_cost"] < 15.0


def test_flat_day_uses_nearest_bin():
    """一字形(low == high): 筹码全压在该价位, 不能除零或全空。"""
    df = _df([_one_day(low=10.0, high=10.0, close=10.0, volume=500.0, amount=500 * 100 * 10.0)])
    out = compute_chips(df, bins=20)
    assert out["ok"] is True
    assert out["peak_price"] == pytest.approx(10.0, abs=out["low"] * 1e-3)
    assert sum(b["ratio"] for b in out["bins"]) == pytest.approx(1.0, abs=1e-6)


def test_turnover_falls_back_to_float_shares():
    """没有 turnover_rate 列时, 用 volume*100/float_shares 折算。"""
    rows = [
        {"date": "2026-09-29", "high": 10.5, "low": 10.0, "close": 10.2,
         "volume": 1000.0, "amount": 1000 * 100 * 10.2},
        {"date": "2026-09-30", "high": 20.5, "low": 20.0, "close": 20.2,
         "volume": 1_000_000.0, "amount": 1_000_000 * 100 * 20.2},
    ]
    # 流通盘 100 万股: 第二天成交 100 万股 = 换手 100%
    out = compute_chips(_df(rows), bins=40, float_shares=1_000_000.0)
    low_half = sum(b["ratio"] for b in out["bins"] if b["price"] < 15)
    assert low_half < 0.02, f"股本折算的换手没生效: {low_half}"


def test_concentration_ranges():
    out = compute_chips(_df([_one_day(low=10.0, high=20.0, close=15.0)]), bins=40)
    p70 = out["concentration"]["p70"]
    assert p70 is not None and p70[0] <= p70[1]
    assert p70[0] >= 10.0 and p70[1] <= 20.0
