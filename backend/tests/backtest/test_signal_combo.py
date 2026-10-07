"""信号函数统一层 — 对拍与单元测试。

对拍基准：把迁移前 3 个策略（ma_golden_cross / macd_golden / boll_breakout）的
原始 numpy 逻辑硬编码为「黄金基准」，与迁移后的 SignalComboStrategy 逐元素对比，
证明声明式信号组合能精确复现手写策略的信号。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import (
    build_market_data_matrix,
    matrix_feature,
    valid_ewm_adjust_false,
    valid_shift,
)
from app.strategy.engine import StrategyEngine
from app.strategy.signals import compile_expr
from app.strategy.signals.backend import SignalComboStrategy


def _builtin_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "backend" / "app" / "strategy" / "builtin"


def _build_panel() -> pl.DataFrame:
    """构造多标的、含趋势+波动+停牌的合成日线，能触发金叉/死叉/突破/量比变化。"""
    rows = []
    start = date(2024, 1, 1)
    for offset in range(140):
        for asset_id, symbol in enumerate(("000001.SZ", "600000.SH", "300001.SZ")):
            if symbol == "300001.SZ" and offset == 60:
                continue  # 制造缺失 bar
            base = 10.0 + asset_id * 6.0
            # 叠加短周期振荡（触发均线金叉/死叉）+ 中周期波动 + 缓慢漂移
            close = (
                base
                + np.sin(offset / 5.0) * 2.0
                + np.sin(offset / 13.0) * 1.0
                + offset * 0.003 * (asset_id + 1)
            )
            volume = 1000.0 + asset_id * 300.0 + (offset % 11) * 120.0 + np.abs(np.sin(offset / 7.0)) * 800.0
            rows.append({
                "symbol": symbol,
                "date": start + timedelta(days=offset),
                "open": close - 0.15,
                "high": close + 0.35,
                "low": close - 0.3,
                "close": close,
                "volume": volume,
                "amount": volume * close,
            })
    return pl.DataFrame(rows)


# ===== 黄金基准（迁移前原策略的 numpy 逻辑） =====


def _orig_ma_golden(market, params):
    ma5 = matrix_feature(market, "ma5")
    ma20 = matrix_feature(market, "ma20")
    golden = (ma5 > ma20) & (valid_shift(ma5, 1) <= valid_shift(ma20, 1))
    dead = (ma5 < ma20) & (valid_shift(ma5, 1) >= valid_shift(ma20, 1))
    entry = np.ones(market.shape, dtype=bool)
    if params.get("require_ma_golden", True):
        entry &= golden
    if params.get("use_volume_filter", True):
        entry &= matrix_feature(market, "vol_ratio_5d") >= float(params.get("vol_ratio_min", 1.2))
    if params.get("require_above_ma60", True):
        entry &= market.close > matrix_feature(market, "ma60")
    return entry.astype(np.uint8), dead.astype(np.uint8)


def _orig_macd_golden(market, params):
    valid = np.isfinite(market.close)
    ema12 = valid_ewm_adjust_false(market.close, valid, span=12)
    ema26 = valid_ewm_adjust_false(market.close, valid, span=26)
    dif = ema12 - ema26
    dif_valid = np.isfinite(dif)
    dea = valid_ewm_adjust_false(dif, dif_valid, span=9)
    prev_dif = valid_shift(dif, 1, dif_valid)
    prev_dea = valid_shift(dea, 1, np.isfinite(dea))
    golden = (dif > dea) & (prev_dif <= prev_dea)
    dead = (dif < dea) & (prev_dif >= prev_dea)
    entry = golden if params.get("require_macd_golden", True) else np.ones(market.shape, dtype=bool)
    if params.get("use_volume_filter", True):
        entry &= matrix_feature(market, "vol_ratio_5d") >= float(params.get("vol_ratio_min", 1.5))
    return entry.astype(np.uint8), dead.astype(np.uint8)


def _orig_boll_breakout(market, params):
    upper = matrix_feature(market, "boll_upper")
    lower = matrix_feature(market, "boll_lower")
    entry = np.ones(market.shape, dtype=bool)
    if params.get("require_boll_breakout", True):
        entry &= market.close > upper
    if params.get("use_volume_filter", True):
        entry &= matrix_feature(market, "vol_ratio_5d") >= float(params.get("vol_ratio_min", 1.5))
    return entry.astype(np.uint8), (market.close < lower).astype(np.uint8)


# ===== 对拍测试 =====


@pytest.mark.parametrize("params", [
    {},  # 默认
    {"require_ma_golden": False},
    {"use_volume_filter": False},
    {"require_above_ma60": False},
    {"require_ma_golden": False, "use_volume_filter": False, "require_above_ma60": False},
])
def test_ma_golden_cross_matches_original(params):
    market = build_market_data_matrix(_build_panel())
    full_params = {**{
        "require_ma_golden": True, "use_volume_filter": True,
        "vol_ratio_min": 1.2, "require_above_ma60": True,
    }, **params}
    expected_entry, expected_exit = _orig_ma_golden(market, full_params)
    combo = SignalComboStrategy(
        "all_of(ma_golden_cross, vol_ratio_ge, close_above_ma60)",
        "ma_dead_cross",
        entry_signal_ids=("signal_ma_golden_5_20",),
        exit_signal_ids=("signal_ma_dead_5_20",),
    )
    actual = combo.compute_signals(market, full_params)
    np.testing.assert_array_equal(actual.entry, expected_entry)
    np.testing.assert_array_equal(actual.exit, expected_exit)
    assert actual.entry.any()


@pytest.mark.parametrize("params", [
    {},
    {"require_macd_golden": False},
    {"use_volume_filter": False},
    {"require_macd_golden": False, "use_volume_filter": False},
])
def test_macd_golden_matches_original(params):
    market = build_market_data_matrix(_build_panel())
    full_params = {**{
        "require_macd_golden": True, "use_volume_filter": True, "vol_ratio_min": 1.5,
    }, **params}
    expected_entry, expected_exit = _orig_macd_golden(market, full_params)
    combo = SignalComboStrategy(
        "all_of(macd_golden, vol_ratio_ge)",
        "macd_dead",
        entry_signal_ids=("signal_macd_golden",),
        exit_signal_ids=("signal_macd_dead",),
    )
    actual = combo.compute_signals(market, full_params)
    np.testing.assert_array_equal(actual.entry, expected_entry)
    np.testing.assert_array_equal(actual.exit, expected_exit)


@pytest.mark.parametrize("params", [
    {},
    {"require_boll_breakout": False},
    {"use_volume_filter": False},
])
def test_boll_breakout_matches_original(params):
    market = build_market_data_matrix(_build_panel())
    full_params = {**{
        "require_boll_breakout": True, "use_volume_filter": True, "vol_ratio_min": 1.5,
    }, **params}
    expected_entry, expected_exit = _orig_boll_breakout(market, full_params)
    combo = SignalComboStrategy(
        "all_of(boll_breakout_upper, vol_ratio_ge)",
        "boll_breakdown_lower",
        entry_signal_ids=("signal_boll_breakout_upper",),
        exit_signal_ids=("signal_boll_breakdown_lower",),
    )
    actual = combo.compute_signals(market, full_params)
    np.testing.assert_array_equal(actual.entry, expected_entry)
    np.testing.assert_array_equal(actual.exit, expected_exit)


# ===== 组合表达式与加载单元测试 =====


def test_combine_expr_supports_all_any_not_and_nesting():
    market = build_market_data_matrix(_build_panel())
    params = {}
    all_result = compile_expr("all_of(ma_golden_cross, vol_ratio_ge)", market, params)
    any_result = compile_expr("any_of(ma_golden_cross, macd_golden)", market, params)
    nested = compile_expr("all_of(ma_golden_cross, any_of(macd_golden, close_above_ma60))", market, params)
    not_result = compile_expr("not_of(ma_golden_cross)", market, params)
    assert all_result.shape == market.shape
    assert any_result.shape == market.shape
    assert nested.shape == market.shape
    assert np.array_equal(not_result, ~compile_expr("ma_golden_cross", market, params))


def test_enable_param_false_neutralizes_in_all_of():
    market = build_market_data_matrix(_build_panel())
    # 全部关闭 → all_of 返回全 True（无条件入场）
    all_off = compile_expr(
        "all_of(ma_golden_cross, vol_ratio_ge, close_above_ma60)",
        market,
        {"require_ma_golden": False, "use_volume_filter": False, "require_above_ma60": False},
    )
    assert all_off.all()


def test_signal_combo_strategies_load_as_matrix_native():
    engine = StrategyEngine(strategy_dirs=[_builtin_dir()])
    for sid in ("ma_golden_cross", "macd_golden", "boll_breakout"):
        strategy = engine.get(sid)
        assert strategy.execution_backend == "matrix_native"
        assert isinstance(strategy.matrix_strategy, SignalComboStrategy)
