"""信号函数统一层 — 15 个纯组合策略迁移对拍测试。

对拍基准：把迁移前各策略的原始 numpy 逻辑硬编码为「黄金基准」，与迁移后的
SignalComboStrategy 逐元素对比，证明声明式信号组合能精确复现手写策略信号。

覆盖策略：oversold_bounce / n_day_low_reversal / volume_price_surge /
high_turnover_surge / bullish_alignment / oversold_reversal / pullback_ma20_bounce /
strong_open / trend_breakout / low_volatility_leader / broken_board_recovery /
consecutive_limit_ups / near_limit_up / limit_up_momentum / pullback_to_support。
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl

from app.backtest.matrix import (
    build_market_data_matrix,
    matrix_feature,
    valid_shift,
)
from app.strategy.signals.backend import SignalComboStrategy

# 涨停/连板/换手等字段需要显式声明才进入矩阵 fields。
_FIELD_COLUMNS = {"turnover_rate", "consecutive_limit_ups", "price_limit_pct"}


def _build_rich_panel() -> pl.DataFrame:
    """含涨停/连板/换手字段的合成日线，能触发各类信号。"""
    rows = []
    start = date(2024, 1, 1)
    for offset in range(140):
        for asset_id, symbol in enumerate(("000001.SZ", "600000.SH", "300001.SZ")):
            if symbol == "300001.SZ" and offset == 60:
                continue  # 制造缺失 bar
            base = 10.0 + asset_id * 6.0
            close = (
                base
                + np.sin(offset / 5.0) * 2.0
                + np.sin(offset / 13.0) * 1.0
                + offset * 0.003 * (asset_id + 1)
            )
            open_ = close - 0.15
            high = close + 0.35
            low = close - 0.3
            volume = (
                1000.0 + asset_id * 300.0 + (offset % 11) * 120.0
                + np.abs(np.sin(offset / 7.0)) * 800.0
            )
            rows.append({
                "symbol": symbol,
                "date": start + timedelta(days=offset),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": volume * close,
                "signal_limit_up": bool(asset_id == 0 and offset % 20 == 5),
                "consecutive_limit_ups": (offset % 4) if asset_id == 0 else 0,
                "turnover_rate": 2.0 + (offset % 8) * 0.7 + asset_id * 0.5,
            })
    return pl.DataFrame(rows)


def _market() -> "object":
    return build_market_data_matrix(_build_rich_panel(), field_columns=_FIELD_COLUMNS)


def _ma20_breakdown(market) -> np.ndarray:
    ma20 = matrix_feature(market, "ma20")
    return (market.close < ma20) & (valid_shift(market.close, 1) >= valid_shift(ma20, 1))


# ===== 黄金基准（迁移前原策略 numpy 逻辑） =====


def _orig_oversold_bounce(market, p):
    entry = np.ones(market.shape, dtype=bool)
    if p.get("use_rsi_filter", True):
        entry &= matrix_feature(market, "rsi_14") < float(p.get("rsi_max", 30.0))
    if p.get("require_bullish_candle", True):
        entry &= market.close > market.open
    if p.get("use_volume_filter", True):
        entry &= matrix_feature(market, "vol_ratio_5d") >= float(p.get("vol_ratio_min", 1.2))
    return entry.astype(np.uint8), _ma20_breakdown(market).astype(np.uint8)


def _orig_n_day_low_reversal(market, p):
    entry = np.ones(market.shape, dtype=bool)
    if p.get("require_n_day_low", True):
        entry &= market.close <= matrix_feature(market, "low_60d")
    if p.get("require_bullish_candle", True):
        entry &= market.close > market.open
    if p.get("use_volume_filter", True):
        entry &= matrix_feature(market, "vol_ratio_5d") >= float(p.get("vol_ratio_min", 1.5))
    return entry.astype(np.uint8), _ma20_breakdown(market).astype(np.uint8)


def _orig_volume_price_surge(market, p):
    ma20 = matrix_feature(market, "ma20")
    breakout = (market.close > ma20) & (valid_shift(market.close, 1) <= valid_shift(ma20, 1))
    breakdown = (market.close < ma20) & (valid_shift(market.close, 1) >= valid_shift(ma20, 1))
    entry = np.ones(market.shape, dtype=bool)
    if p.get("require_ma20_breakout", True):
        entry &= breakout
    if p.get("use_volume_filter", True):
        entry &= matrix_feature(market, "vol_ratio_5d") >= float(p.get("vol_ratio_min", 2.0))
    if p.get("require_bullish_candle", True):
        entry &= market.close > market.open
    return entry.astype(np.uint8), breakdown.astype(np.uint8)


def _orig_high_turnover_surge(market, p):
    entry = np.ones(market.shape, dtype=bool)
    if p.get("use_turnover_filter", True):
        entry &= matrix_feature(market, "turnover_rate") > float(p.get("min_turnover", 5.0))
    if p.get("use_change_filter", True):
        entry &= matrix_feature(market, "change_pct") > float(p.get("min_change", 3.0)) / 100.0
    return entry.astype(np.uint8), _ma20_breakdown(market).astype(np.uint8)


def _orig_bullish_alignment(market, p):
    ma5 = matrix_feature(market, "ma5")
    ma10 = matrix_feature(market, "ma10")
    ma20 = matrix_feature(market, "ma20")
    ma60 = matrix_feature(market, "ma60")
    entry = np.ones(market.shape, dtype=bool)
    if p.get("require_ma_alignment", True):
        entry &= (ma5 > ma10) & (ma10 > ma20) & (ma20 > ma60)
    if p.get("require_positive_momentum", True):
        entry &= matrix_feature(market, "momentum_20d") > 0
    ma_dead = (ma5 < ma20) & (valid_shift(ma5, 1) >= valid_shift(ma20, 1))
    exit_ = ma_dead | _ma20_breakdown(market)
    return entry.astype(np.uint8), exit_.astype(np.uint8)


def _orig_oversold_reversal(market, p):
    entry = np.ones(market.shape, dtype=bool)
    if p.get("use_rsi_filter", True):
        entry &= matrix_feature(market, "rsi_14") < float(p.get("rsi_max", 30.0))
    if p.get("use_change_filter", True):
        entry &= matrix_feature(market, "change_pct") > float(p.get("min_change", 1.0)) / 100.0
    if p.get("require_above_ma5", True):
        entry &= market.close > matrix_feature(market, "ma5")
    return entry.astype(np.uint8), _ma20_breakdown(market).astype(np.uint8)


def _orig_pullback_ma20_bounce(market, p):
    ma5 = matrix_feature(market, "ma5")
    ma20 = matrix_feature(market, "ma20")
    ma60 = matrix_feature(market, "ma60")
    entry = np.ones(market.shape, dtype=bool)
    if p.get("use_ma20_proximity", True):
        proximity = float(p.get("ma_proximity", 2.0)) / 100.0
        entry &= (market.close > ma20 * (1.0 - proximity)) & (
            market.close < ma20 * (1.0 + proximity)
        )
    if p.get("require_ma_alignment", True):
        entry &= (ma5 > ma20) & (ma20 > ma60)
    if p.get("require_positive_change", True):
        entry &= matrix_feature(market, "change_pct") > 0
    ma_dead = (ma5 < ma20) & (valid_shift(ma5, 1) >= valid_shift(ma20, 1))
    exit_ = _ma20_breakdown(market) | ma_dead
    return entry.astype(np.uint8), exit_.astype(np.uint8)


def _orig_strong_open(market, p):
    entry = np.ones(market.shape, dtype=bool)
    if p.get("use_open_gap_filter", True):
        entry &= market.open > valid_shift(market.close, 1) * (
            1.0 + float(p.get("min_open_gap", 3.0)) / 100.0
        )
    if p.get("require_bullish_candle", True):
        entry &= market.close > market.open
    if p.get("use_change_filter", True):
        entry &= matrix_feature(market, "change_pct") > float(p.get("min_change", 3.0)) / 100.0
    return entry.astype(np.uint8), _ma20_breakdown(market).astype(np.uint8)


def _orig_trend_breakout(market, p):
    entry = np.ones(market.shape, dtype=bool)
    if p.get("require_above_ma60", True):
        entry &= market.close > matrix_feature(market, "ma60")
    if p.get("require_n_day_high", True):
        entry &= market.close >= matrix_feature(market, "high_60d")
    if p.get("use_volume_filter", True):
        entry &= matrix_feature(market, "vol_ratio_5d") >= float(p.get("vol_ratio_min", 2.0))
    return entry.astype(np.uint8), _ma20_breakdown(market).astype(np.uint8)


def _orig_low_volatility_leader(market, p):
    ma20 = matrix_feature(market, "ma20")
    entry = np.ones(market.shape, dtype=bool)
    if p.get("require_positive_momentum", True):
        entry &= matrix_feature(market, "momentum_20d") > 0
    if p.get("use_volatility_filter", True):
        entry &= matrix_feature(market, "annual_vol_20d") < float(p.get("vol_max", 0.30))
    if p.get("require_above_ma20", True):
        entry &= market.close > ma20
    return entry.astype(np.uint8), _ma20_breakdown(market).astype(np.uint8)


def _orig_broken_board_recovery(market, p):
    entry = np.ones(market.shape, dtype=bool)
    if p.get("require_limit_up", True):
        entry &= market.limit_up_locked.astype(bool)
    if p.get("use_volume_filter", True):
        entry &= matrix_feature(market, "vol_ratio_5d") >= float(p.get("vol_ratio_min", 1.5))
    if p.get("use_change_filter", True):
        entry &= matrix_feature(market, "change_pct") > float(p.get("min_change", 3.0)) / 100.0
    return entry.astype(np.uint8), _ma20_breakdown(market).astype(np.uint8)


def _orig_consecutive_limit_ups(market, p):
    entry = np.ones(market.shape, dtype=bool)
    if p.get("require_limit_up", True):
        entry &= market.limit_up_locked.astype(bool)
    if p.get("use_boards_filter", True):
        entry &= matrix_feature(market, "consecutive_limit_ups") >= int(p.get("min_boards", 2))
    return entry.astype(np.uint8), np.zeros(market.shape, dtype=np.uint8)


def _orig_near_limit_up(market, p):
    change = matrix_feature(market, "change_pct")
    entry = np.ones(market.shape, dtype=bool)
    if p.get("use_change_filter", True):
        entry &= change > float(p.get("min_change", 7.0)) / 100.0
    if p.get("use_limit_gap_filter", True):
        limit_pct = matrix_feature(market, "price_limit_pct")
        entry &= change >= limit_pct - float(p.get("limit_gap", 3.0)) / 100.0
    return entry.astype(np.uint8), _ma20_breakdown(market).astype(np.uint8)


def _orig_limit_up_momentum(market, p):
    entry = np.ones(market.shape, dtype=bool)
    if p.get("use_change_filter", True):
        entry &= matrix_feature(market, "change_pct") > float(p.get("min_change", 5.0)) / 100.0
    if p.get("use_boards_filter", True):
        entry &= matrix_feature(market, "consecutive_limit_ups") >= int(p.get("min_boards", 1))
    return entry.astype(np.uint8), np.zeros(market.shape, dtype=np.uint8)


def _orig_pullback_to_support(market, p):
    ma20 = matrix_feature(market, "ma20")
    entry = np.ones(market.shape, dtype=bool)
    if p.get("use_ma20_proximity", True):
        proximity = float(p.get("ma_proximity", 2.0)) / 100.0
        entry &= (market.close > ma20 * (1.0 - proximity)) & (
            market.close < ma20 * (1.0 + proximity)
        )
    if p.get("use_volume_filter", True):
        entry &= matrix_feature(market, "vol_ratio_5d") < float(p.get("vol_ratio_max", 0.8))
    if p.get("require_above_ma60", True):
        entry &= market.close > matrix_feature(market, "ma60")
    if p.get("require_positive_momentum", True):
        entry &= matrix_feature(market, "momentum_20d") > 0
    return entry.astype(np.uint8), _ma20_breakdown(market).astype(np.uint8)


# ===== 对拍测试 =====


def _assert_matches(orig, entry_expr, exit_expr, full_params, entry_ids, exit_ids):
    market = _market()
    expected_entry, expected_exit = orig(market, full_params)
    combo = SignalComboStrategy(
        entry_expr, exit_expr,
        entry_signal_ids=entry_ids, exit_signal_ids=exit_ids,
    )
    actual = combo.compute_signals(market, full_params)
    np.testing.assert_array_equal(actual.entry, expected_entry)
    np.testing.assert_array_equal(actual.exit, expected_exit)


def test_oversold_bounce_matches_original():
    full = {"use_rsi_filter": True, "rsi_max": 30.0, "require_bullish_candle": True,
            "use_volume_filter": True, "vol_ratio_min": 1.2}
    _assert_matches(_orig_oversold_bounce, "all_of(rsi_below, bullish_candle, vol_ratio_ge)",
                    "ma20_breakdown", full, (), ("signal_ma20_breakdown",))


def test_n_day_low_reversal_matches_original():
    full = {"require_n_day_low": True, "require_bullish_candle": True,
            "use_volume_filter": True, "vol_ratio_min": 1.5}
    _assert_matches(_orig_n_day_low_reversal,
                    "all_of(close_at_60d_low, bullish_candle, vol_ratio_ge)",
                    "ma20_breakdown", full, ("signal_n_day_low",), ("signal_ma20_breakdown",))


def test_volume_price_surge_matches_original():
    full = {"require_ma20_breakout": True, "use_volume_filter": True,
            "vol_ratio_min": 2.0, "require_bullish_candle": True}
    _assert_matches(_orig_volume_price_surge,
                    "all_of(ma20_breakout, vol_ratio_ge, bullish_candle)",
                    "ma20_breakdown", full, ("signal_ma20_breakout",), ("signal_ma20_breakdown",))


def test_high_turnover_surge_matches_original():
    full = {"use_turnover_filter": True, "min_turnover": 5.0,
            "use_change_filter": True, "min_change": 3.0}
    _assert_matches(_orig_high_turnover_surge, "all_of(turnover_ge, change_pct_ge)",
                    "ma20_breakdown", full, ("signal_volume_surge",), ("signal_ma20_breakdown",))


def test_bullish_alignment_matches_original():
    full = {"require_ma_alignment": True, "require_positive_momentum": True}
    _assert_matches(_orig_bullish_alignment,
                    "all_of(ma_bullish_alignment, momentum_positive)",
                    "any_of(ma_dead_cross, ma20_breakdown)", full,
                    ("signal_ma_golden_5_20", "signal_ma_golden_20_60"),
                    ("signal_ma_dead_5_20", "signal_ma20_breakdown"))


def test_oversold_reversal_matches_original():
    full = {"use_rsi_filter": True, "rsi_max": 30.0, "use_change_filter": True,
            "min_change": 1.0, "require_above_ma5": True}
    _assert_matches(_orig_oversold_reversal,
                    "all_of(rsi_below, change_pct_ge, close_above_ma5)",
                    "ma20_breakdown", full, (), ("signal_ma20_breakdown",))


def test_pullback_ma20_bounce_matches_original():
    full = {"use_ma20_proximity": True, "ma_proximity": 2.0, "require_ma_alignment": True,
            "require_positive_change": True}
    _assert_matches(_orig_pullback_ma20_bounce,
                    "all_of(close_near_ma20, ma5_20_60_alignment, change_pct_positive)",
                    "any_of(ma20_breakdown, ma_dead_cross)", full,
                    ("signal_ma_golden_5_20",),
                    ("signal_ma20_breakdown", "signal_ma_dead_5_20"))


def test_strong_open_matches_original():
    full = {"use_open_gap_filter": True, "min_open_gap": 3.0, "require_bullish_candle": True,
            "use_change_filter": True, "min_change": 3.0}
    _assert_matches(_orig_strong_open, "all_of(open_gap_up, bullish_candle, change_pct_ge)",
                    "ma20_breakdown", full, (), ("signal_ma20_breakdown",))


def test_trend_breakout_matches_original():
    full = {"require_above_ma60": True, "require_n_day_high": True,
            "use_volume_filter": True, "vol_ratio_min": 2.0}
    _assert_matches(_orig_trend_breakout,
                    "all_of(close_above_ma60, close_at_60d_high, vol_ratio_ge)",
                    "ma20_breakdown", full, ("signal_n_day_high",), ("signal_ma20_breakdown",))


def test_low_volatility_leader_matches_original():
    full = {"require_positive_momentum": True, "use_volatility_filter": True,
            "vol_max": 0.30, "require_above_ma20": True}
    _assert_matches(_orig_low_volatility_leader,
                    "all_of(momentum_positive, annual_vol_below, close_above_ma20)",
                    "ma20_breakdown", full, ("signal_ma20_breakout",), ("signal_ma20_breakdown",))


def test_broken_board_recovery_matches_original():
    full = {"require_limit_up": True, "use_volume_filter": True, "vol_ratio_min": 1.5,
            "use_change_filter": True, "min_change": 3.0}
    _assert_matches(_orig_broken_board_recovery,
                    "all_of(limit_up_locked, vol_ratio_ge, change_pct_ge)",
                    "ma20_breakdown", full, ("signal_limit_up",), ("signal_ma20_breakdown",))


def test_consecutive_limit_ups_matches_original():
    full = {"require_limit_up": True, "use_boards_filter": True, "min_boards": 2}
    _assert_matches(_orig_consecutive_limit_ups,
                    "all_of(limit_up_locked, consecutive_limit_ups_ge)",
                    "", full, ("signal_limit_up",), ())


def test_near_limit_up_matches_original():
    full = {"use_change_filter": True, "min_change": 7.0,
            "use_limit_gap_filter": True, "limit_gap": 3.0}
    _assert_matches(_orig_near_limit_up, "all_of(change_pct_ge, near_limit_up_gap)",
                    "ma20_breakdown", full, (), ("signal_ma20_breakdown",))


def test_limit_up_momentum_matches_original():
    full = {"use_change_filter": True, "min_change": 5.0,
            "use_boards_filter": True, "min_boards": 1}
    _assert_matches(_orig_limit_up_momentum,
                    "all_of(change_pct_ge, consecutive_limit_ups_ge)",
                    "", full, ("signal_limit_up",), ())


def test_pullback_to_support_matches_original():
    full = {"use_ma20_proximity": True, "ma_proximity": 2.0, "use_volume_filter": True,
            "vol_ratio_max": 0.8, "require_above_ma60": True, "require_positive_momentum": True}
    _assert_matches(_orig_pullback_to_support,
                    "all_of(close_near_ma20, vol_ratio_le, close_above_ma60, momentum_positive)",
                    "ma20_breakdown", full, ("signal_ma_golden_5_20",), ("signal_ma20_breakdown",))


def test_switch_off_neutralizes_signal():
    """关掉开关（enable_param=False）时信号中性化，与「无条件入场」一致。"""
    market = _market()
    # oversold_bounce 三个开关全关 → entry 全 True
    full_off = {"use_rsi_filter": False, "require_bullish_candle": False,
                "use_volume_filter": False, "vol_ratio_min": 1.2, "rsi_max": 30.0}
    combo = SignalComboStrategy(
        "all_of(rsi_below, bullish_candle, vol_ratio_ge)", "ma20_breakdown",
        entry_signal_ids=(), exit_signal_ids=("signal_ma20_breakdown",),
    )
    actual = combo.compute_signals(market, full_off)
    assert actual.entry.all()
