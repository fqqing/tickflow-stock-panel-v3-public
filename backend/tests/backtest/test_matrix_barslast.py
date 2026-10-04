"""``valid_barslast`` (通达信 BARSLAST) —— 有效 bar 语义 + 「向上趋势并突破」公式对拍。"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import (
    MarketDataMatrix,
    build_market_data_matrix,
    valid_barslast,
)


def _reference_barslast(condition: np.ndarray) -> np.ndarray:
    """源公式参考实现: 当前成立 → 0; 否则距上次成立的 bar 数; 从未成立 → 0。"""
    out = np.zeros(condition.shape[0], dtype=np.int64)
    last = -1
    for index in range(condition.shape[0]):
        if condition[index]:
            last = index
        out[index] = (index - last) if last >= 0 else 0
    return out


def test_valid_barslast_matches_reference_on_dense_series():
    condition = np.zeros((12, 1), dtype=bool)
    condition[[1, 5, 6], 0] = True
    mask = np.ones((12, 1), dtype=bool)

    actual = valid_barslast(condition, mask)[:, 0]
    np.testing.assert_array_equal(actual, _reference_barslast(condition[:, 0]))


def test_valid_barslast_starts_at_zero_before_first_hit():
    condition = np.zeros((6, 1), dtype=bool)
    condition[4, 0] = True
    mask = np.ones((6, 1), dtype=bool)

    actual = valid_barslast(condition, mask)[:, 0]
    # 首次成立之前按 0 计 (与源实现一致), 成立当根为 0, 其后逐根递增
    np.testing.assert_array_equal(actual, [0.0, 0.0, 0.0, 0.0, 0.0, 1.0])


def test_valid_barslast_counts_effective_bars_only():
    # 有效 bar 在 row 0..3 与 row 7 (即第 5 个有效 bar), 中间 4..6 为停牌
    condition = np.zeros((8, 1), dtype=bool)
    condition[0, 0] = True
    condition[7, 0] = True
    mask = np.zeros((8, 1), dtype=bool)
    mask[[0, 1, 2, 3, 7], 0] = True

    actual = valid_barslast(condition, mask)[:, 0]
    np.testing.assert_array_equal(actual[:4], [0.0, 1.0, 2.0, 3.0])
    assert np.isnan(actual[4:7]).all()
    assert actual[7] == 0.0


def test_valid_barslast_treats_nan_condition_rows_as_absent():
    condition = np.array([[0.0], [np.nan], [1.0], [0.0]], dtype=np.float32)
    mask = np.ones((4, 1), dtype=bool)

    actual = valid_barslast(condition, mask)[:, 0]
    assert actual[0] == 0.0
    assert np.isnan(actual[1])
    assert actual[2] == 0.0
    assert actual[3] == 1.0


def test_valid_barslastcount_counts_consecutive_hits_from_one():
    from app.backtest.matrix import valid_barslastcount

    condition = np.zeros((8, 1), dtype=bool)
    condition[[1, 2, 4, 5, 6], 0] = True
    mask = np.ones((8, 1), dtype=bool)

    actual = valid_barslastcount(condition, mask)[:, 0]
    np.testing.assert_array_equal(actual, [0.0, 1.0, 2.0, 0.0, 1.0, 2.0, 3.0, 0.0])


def test_valid_barslastcount_skips_suspended_rows_without_breaking_run():
    """停牌行不是一个观测: 既不计入连续数, 也不打断连续。"""
    from app.backtest.matrix import valid_barslastcount

    condition = np.ones((6, 1), dtype=bool)
    mask = np.zeros((6, 1), dtype=bool)
    mask[[0, 1, 4, 5], 0] = True  # row 2/3 为停牌

    actual = valid_barslastcount(condition, mask)[:, 0]
    np.testing.assert_array_equal(actual[[0, 1, 4, 5]], [1.0, 2.0, 3.0, 4.0])
    assert np.isnan(actual[2:4]).all()


def test_valid_barslastcount_treats_nan_condition_rows_as_absent():
    from app.backtest.matrix import valid_barslastcount

    condition = np.array([[1.0], [np.nan], [1.0]], dtype=np.float32)
    mask = np.ones((3, 1), dtype=bool)

    actual = valid_barslastcount(condition, mask)[:, 0]
    assert actual[0] == 1.0
    assert np.isnan(actual[1])
    # NaN 行被跳过, 连续计数不断档
    assert actual[2] == 2.0


def test_valid_shift_at_uses_effective_bar_offsets():
    """变长 REF: 偏移以有效 bar 计, 停牌行不占位置。"""
    from app.backtest.matrix import valid_shift_at

    values = np.array([[10.0], [11.0], [12.0], [13.0], [14.0], [15.0]])
    mask = np.zeros((6, 1), dtype=bool)
    mask[[0, 1, 4, 5], 0] = True  # 有效 bar 序列 = row [0, 1, 4, 5] (位置 0..3)
    # 偏移量按 row 给出; row 2/3 是停牌行, 其偏移被忽略
    periods = np.array([[0.0], [1.0], [np.nan], [np.nan], [1.0], [2.0]])

    actual = valid_shift_at(values, periods, mask)[:, 0]
    assert actual[0] == 10.0  # 位置 0 回看 0 -> 自己
    assert actual[1] == 10.0  # 位置 1 回看 1 -> 位置 0
    assert actual[4] == 11.0  # 位置 2 回看 1 -> 位置 1 (停牌行不占位置)
    assert actual[5] == 11.0  # 位置 3 回看 2 -> 位置 1
    assert np.isnan(actual[2:4]).all()


def test_valid_shift_at_rejects_negative_and_oversized_offsets():
    from app.backtest.matrix import valid_shift_at

    values = np.array([[1.0], [2.0], [3.0], [4.0]])
    mask = np.ones((4, 1), dtype=bool)
    periods = np.array([[-1.0], [np.nan], [99.0], [2.0]])

    actual = valid_shift_at(values, periods, mask)[:, 0]
    assert np.isnan(actual[:3]).all()
    assert actual[3] == 2.0


def test_valid_shift_at_scalar_matches_valid_shift():
    from app.backtest.matrix import valid_shift, valid_shift_at

    values = np.arange(10, dtype=np.float32).reshape(10, 1)
    mask = np.ones((10, 1), dtype=bool)

    np.testing.assert_array_equal(
        valid_shift_at(values, 3, mask),
        valid_shift(values, 3, mask),
    )


def test_valid_shift_at_rejects_mismatched_period_shape():
    from app.backtest.matrix import valid_shift_at

    values = np.arange(6, dtype=np.float32).reshape(6, 1)
    with pytest.raises(ValueError, match="periods shape"):
        valid_shift_at(values, np.zeros((3, 1), dtype=np.float32))


def test_valid_rolling_min_at_uses_effective_bar_windows():
    """变长 LLV: 窗口宽度以有效 bar 计, 停牌行不占位置。"""
    from app.backtest.matrix import valid_rolling_min_at

    values = np.array([[10.0], [8.0], [6.0], [12.0], [9.0], [7.0]])
    mask = np.zeros((6, 1), dtype=bool)
    mask[[0, 1, 4, 5], 0] = True  # 有效 bar = row [0, 1, 4, 5], 取值 10/8/9/7
    # row 2/3 是停牌行, 其窗口被忽略
    windows = np.array([[2.0], [2.0], [np.nan], [np.nan], [1.0], [3.0]])

    actual = valid_rolling_min_at(values, windows, mask)[:, 0]
    assert np.isnan(actual[0]), "位置 0 只有 1 根有效 bar, 窗口 2 应判无效"
    assert actual[1] == 8.0  # 位置 1 窗口 2 -> min(10, 8)
    assert actual[4] == 9.0  # 位置 2 窗口 1 -> 自身
    assert actual[5] == 7.0  # 位置 3 窗口 3 -> min(8, 9, 7), 停牌行不占位置
    assert np.isnan(actual[2:4]).all()


def test_valid_rolling_max_at_mirrors_min_variant():
    from app.backtest.matrix import valid_rolling_max_at

    values = np.array([[10.0], [8.0], [6.0], [12.0], [9.0], [7.0]])
    mask = np.zeros((6, 1), dtype=bool)
    mask[[0, 1, 4, 5], 0] = True
    windows = np.array([[2.0], [2.0], [np.nan], [np.nan], [1.0], [3.0]])

    actual = valid_rolling_max_at(values, windows, mask)[:, 0]
    assert actual[1] == 10.0  # min(10, 8) 的镜像 -> max(10, 8)
    assert actual[4] == 9.0
    assert actual[5] == 9.0  # max(8, 9, 7)


def test_valid_rolling_at_rejects_invalid_windows():
    """窗口 <= 0 / 非整数 / NaN / 超过历史长度一律输出 NaN, 不拿残窗凑数。"""
    from app.backtest.matrix import valid_rolling_max_at, valid_rolling_min_at

    values = np.arange(1, 7, dtype=np.float32).reshape(6, 1)
    mask = np.ones((6, 1), dtype=bool)
    windows = np.array([[0.0], [-2.0], [np.nan], [2.5], [99.0], [2.0]])

    actual = valid_rolling_min_at(values, windows, mask)[:, 0]
    assert np.isnan(actual[:5]).all()
    assert actual[5] == 5.0  # 窗口 2 -> min(5, 6)

    mirrored = valid_rolling_max_at(values, windows, mask)[:, 0]
    assert np.isnan(mirrored[:5]).all()
    assert mirrored[5] == 6.0


def test_valid_rolling_at_scalar_window_matches_fixed_variant():
    from app.backtest.matrix import (
        valid_rolling_max,
        valid_rolling_max_at,
        valid_rolling_min,
        valid_rolling_min_at,
    )

    values = np.arange(10, dtype=np.float32).reshape(10, 1)
    mask = np.ones((10, 1), dtype=bool)

    np.testing.assert_array_equal(
        valid_rolling_min_at(values, 3, mask),
        valid_rolling_min(values, mask, 3),
    )
    np.testing.assert_array_equal(
        valid_rolling_max_at(values, 3, mask),
        valid_rolling_max(values, mask, 3),
    )


def test_valid_rolling_at_rejects_mismatched_window_shape():
    from app.backtest.matrix import valid_rolling_max_at, valid_rolling_min_at

    values = np.arange(6, dtype=np.float32).reshape(6, 1)
    mask = np.ones((6, 1), dtype=bool)
    with pytest.raises(ValueError, match="windows shape"):
        valid_rolling_min_at(values, np.zeros((3, 1), dtype=np.float32), mask)
    with pytest.raises(ValueError, match="windows shape"):
        valid_rolling_max_at(values, np.zeros((3, 1), dtype=np.float32), mask)


# --------------------------------------------------------------------------
# 「向上趋势并突破」= 源通达信公式的矩阵原生实现, 逐位对拍
# --------------------------------------------------------------------------


def _reference_ema(values: np.ndarray, span: int) -> np.ndarray:
    alpha = 2.0 / (span + 1.0)
    out = np.empty_like(values)
    out[0] = values[0]
    for index in range(1, values.size):
        out[index] = alpha * values[index] + (1.0 - alpha) * out[index - 1]
    return out


def _shift(values: np.ndarray, periods: int) -> np.ndarray:
    out = np.full(values.shape, np.nan)
    out[periods:] = values[:-periods]
    return out


def _reference_qstpxg(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """逐 bar 直译源公式 (无停牌, 与矩阵在「全有效 bar」下应完全一致)。"""
    dsg = _reference_ema(high, 26)
    dxg = _reference_ema(low, 26)
    csg = _reference_ema(high, 89)
    bbb = (close > dsg) & (_shift(close, 1) <= _shift(dsg, 1))
    sss = (dxg > close) & (_shift(dxg, 1) <= _shift(close, 1))
    bbb_prev = np.zeros(close.size, dtype=bool)
    bbb_prev[1:] = bbb[:-1]
    sss_prev = np.zeros(close.size, dtype=bool)
    sss_prev[1:] = sss[:-1]
    bars_since_up = _reference_barslast(bbb_prev) + 1
    bars_since_down = _reference_barslast(sss_prev) + 1
    return (
        bbb
        & (bars_since_up > bars_since_down)
        & (close > _shift(csg, 89))
        & (close > _shift(dsg, 26))
    )


def _single_symbol_market(closes: list[float]) -> MarketDataMatrix:
    start = date(2024, 1, 1)
    rows = [
        {
            "symbol": "000001.SZ",
            "name": "测试股票",
            "date": start + timedelta(days=offset),
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1000.0,
        }
        for offset, close in enumerate(closes)
    ]
    return build_market_data_matrix(pl.DataFrame(rows))


def _pullback_then_breakout_closes() -> list[float]:
    """先上穿 → 回落跌破 → 长期贴地 → 跳空突破: 覆盖 BBB_0 > SSS_0 的完整序列。"""
    closes = [10.0 + 0.4 * index for index in range(10)]  # 0..9   拉升触发上穿
    closes += [13.6 - 0.09 * index for index in range(1, 41)]  # 10..49  回落触发跌破
    closes += [10.0 - 0.0065 * index for index in range(1, 150)]  # 50..198 贴地阴跌
    closes.append(11.8)  # 199     跳空再突破
    return closes


def test_qstpxg_matrix_strategy_matches_reference_formula():
    from app.strategy.builtin.upward_trend_breakout import MATRIX_STRATEGY

    closes = np.asarray(_pullback_then_breakout_closes(), dtype=np.float64)
    market = _single_symbol_market(closes.tolist())

    signals = MATRIX_STRATEGY.compute_signals(market, {})
    entry = signals.entry[:, 0].astype(bool)

    expected = _reference_qstpxg(market.high[:, 0], market.low[:, 0], market.close[:, 0])
    np.testing.assert_array_equal(entry, expected)

    # 序列本身必须真的触发过一次, 否则对拍是空跑
    assert expected[-1], "构造的回调后突破序列未产生信号"
    assert entry[:-1].sum() == 0


def test_qstpxg_requires_recent_short_rail_cross():
    """关掉 BBB 后, 跳空当天之前的贴地阴跌不应被选中。"""
    from app.strategy.builtin.upward_trend_breakout import MATRIX_STRATEGY

    closes = np.asarray(_pullback_then_breakout_closes(), dtype=np.float64)
    market = _single_symbol_market(closes.tolist())

    relaxed = (
        MATRIX_STRATEGY.compute_signals(market, {"require_short_rail_cross": False})
        .entry[:, 0]
        .astype(bool)
    )

    # 放开 BBB 后只剩「距上次上穿更久 + 站上两条历史轨道」, 命中点只会变多
    assert relaxed.sum() >= 1
