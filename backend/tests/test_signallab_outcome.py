"""app/signallab.outcome 与 app/signallab.summary 的定向测试。

覆盖口径边界: T+1 开盘成交, 涨停封死顺延, 始终不可成交, 尾部截断, 停牌 NaN 不污染极值,
止损止盈先后, 出场信号偏移, 以及统计时 null 不计入分母。
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from app.signallab.outcome import (
    OutcomeConfig,
    build_signal_outcomes,
    compute_market_baseline,
    excess_column,
    ret_column,
)
from app.signallab.summary import add_feature_buckets, summarize_outcomes

SYMBOLS = ("AAA.SZ", "BBB.SZ")
LABELS = ("2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09", "2026-01-12")

_BASE_OPEN = np.full((6, 2), 10.0)
_BASE_HIGH = np.array([[11.0, 11.0], [12.0, 12.0], [13.0, 13.0], [14.0, 14.0], [15.0, 15.0], [16.0, 16.0]])
_BASE_LOW = np.array([[9.0, 9.0], [8.0, 8.0], [7.0, 7.0], [6.0, 6.0], [5.0, 5.0], [4.0, 4.0]])
_BASE_CLOSE = np.array(
    [[10.5, 10.5], [10.2, 10.2], [10.8, 10.8], [10.1, 10.1], [10.9, 10.9], [9.5, 9.5]]
)


def _market(**overrides) -> SimpleNamespace:
    payload = {
        "open": _BASE_OPEN.copy(),
        "high": _BASE_HIGH.copy(),
        "low": _BASE_LOW.copy(),
        "close": _BASE_CLOSE.copy(),
        "tradable": np.ones((6, 2), dtype=np.uint8),
        "limit_up_locked": np.zeros((6, 2), dtype=np.uint8),
        "timestamp_labels": LABELS,
        "symbols": SYMBOLS,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _entry(*cells: tuple[int, int]) -> np.ndarray:
    matrix = np.zeros((6, 2), dtype=bool)
    for row, col in cells:
        matrix[row, col] = True
    return matrix


def _one(frame: pl.DataFrame) -> dict:
    assert frame.height == 1
    return frame.row(0, named=True)


def _horizons(*values: int) -> OutcomeConfig:
    return OutcomeConfig(horizons=values)


def test_fill_price_is_next_session_open() -> None:
    """信号在 t, 默认 T+1 以开盘价成交; 收益以此为分母。"""
    frame = build_signal_outcomes(_market(), _entry((0, 0)), config=_horizons(1, 2, 3))
    row = _one(frame)
    assert row["symbol"] == "AAA.SZ"
    assert row["signal_date"] == "2026-01-05"
    assert row["fill_date"] == "2026-01-06"
    assert row["delay_days"] == 1
    assert row["entry_price"] == pytest.approx(10.0)
    # 成交日收盘 10.2 / 成交价 10.0
    assert row[ret_column(1)] == pytest.approx(0.02)
    assert row[ret_column(2)] == pytest.approx(0.08)
    assert row[ret_column(3)] == pytest.approx(0.01)
    assert row["truncated"] is False


def test_mfe_mae_use_intraday_extremes_of_window() -> None:
    """窗口是 [成交日, 成交日+window-1], MFE/MAE 取窗口内 high/low 极值。"""
    frame = build_signal_outcomes(_market(), _entry((0, 0)), config=_horizons(1, 2, 3))
    row = _one(frame)
    # 成交日 row1, 窗口 row1..row3: high max = 14, low min = 6
    assert row["mfe"] == pytest.approx(0.4)
    assert row["mae"] == pytest.approx(-0.4)
    assert row["mfe_bar"] == 2
    assert row["mae_bar"] == 2
    assert row["window_bars"] == 3


def test_close_only_extremes_when_intraday_disabled() -> None:
    """intraday_extremes=False 时只用收盘价, 极值一定更小。"""
    config = OutcomeConfig(horizons=(1, 2, 3), intraday_extremes=False)
    frame = build_signal_outcomes(_market(), _entry((0, 0)), config=config)
    row = _one(frame)
    # 窗口收盘价 10.2 / 10.8 / 10.1
    assert row["mfe"] == pytest.approx(0.08)
    assert row["mae"] == pytest.approx(0.01)
    assert row["mfe_bar"] == 1
    assert row["mae_bar"] == 2


def test_limit_up_locked_defers_fill() -> None:
    """成交日一字涨停不可买 -> 顺延到下一个可成交日, entry_price 用顺延日的开盘。"""
    locked = np.zeros((6, 2), dtype=np.uint8)
    locked[1, 0] = 1
    frame = build_signal_outcomes(
        _market(limit_up_locked=locked), _entry((0, 0)), config=_horizons(1, 2)
    )
    row = _one(frame)
    assert row["fill_date"] == "2026-01-07"
    assert row["delay_days"] == 2
    assert row["entry_price"] == pytest.approx(10.0)
    assert row[ret_column(1)] == pytest.approx(0.08)


def test_unfilled_when_never_buyable() -> None:
    """连续不可成交超过上限时记 filled=False, 且不产生任何收益观测。"""
    locked = np.ones((6, 2), dtype=np.uint8)
    frame = build_signal_outcomes(
        _market(limit_up_locked=locked), _entry((0, 0)), config=_horizons(1, 2)
    )
    row = _one(frame)
    assert row["filled"] is False
    assert row["fill_date"] is None
    assert row[ret_column(1)] is None


def test_include_unfilled_false_drops_rows() -> None:
    locked = np.ones((6, 2), dtype=np.uint8)
    config = OutcomeConfig(horizons=(1,), include_unfilled=False)
    frame = build_signal_outcomes(_market(limit_up_locked=locked), _entry((0, 0)), config=config)
    assert frame.height == 0


def test_tail_sample_is_truncated_not_zero_filled() -> None:
    """前瞻窗口不足时只给出可得的那部分收益, 其余保持 null(不可按 0 计)。"""
    frame = build_signal_outcomes(_market(), _entry((4, 0)), config=_horizons(1, 2, 3))
    row = _one(frame)
    assert row["truncated"] is True
    assert row["window_bars"] == 1
    assert row[ret_column(1)] == pytest.approx(-0.05)
    assert row[ret_column(2)] is None
    assert row[ret_column(3)] is None


def test_suspended_nan_does_not_poison_extremes() -> None:
    """持有期内停牌(NaN)不能污染极值: 直接 amax/amin 会得到 NaN, 这里必须忽略。"""
    high = _BASE_HIGH.copy()
    low = _BASE_LOW.copy()
    close = _BASE_CLOSE.copy()
    high[:, 0] = np.nan
    low[:, 0] = np.nan
    close[:, 0] = np.nan
    high[1, 0] = 13.0
    low[1, 0] = 9.5
    close[1, 0] = 11.0
    frame = build_signal_outcomes(
        _market(high=high, low=low, close=close), _entry((0, 0)), config=_horizons(1, 2, 3)
    )
    row = _one(frame)
    assert row["mfe"] == pytest.approx(0.3)
    assert row["mae"] == pytest.approx(-0.05)
    assert row[ret_column(1)] == pytest.approx(0.1)
    assert row[ret_column(2)] is None


def test_stop_and_target_order() -> None:
    """同一窗口内先触及止损还是先触及止盈, 必须能判定先后。"""
    high = _BASE_HIGH.copy()
    low = _BASE_LOW.copy()
    # 止损 -5% (低于 9.5) 在第 0 根, 止盈 +15% (高于 11.5) 在第 2 根 -> 先止损
    high[:, 0] = [10.0, 10.0, 12.0, 14.0, 15.0, 16.0]
    low[:, 0] = [10.0, 8.0, 8.0, 6.0, 5.0, 4.0]
    first = build_signal_outcomes(
        _market(high=high, low=low),
        _entry((0, 0)),
        config=OutcomeConfig(horizons=(1, 2, 3), stop_loss=-0.05, take_profit=0.15),
    ).row(0, named=True)
    assert first["touch_stop"] is True
    assert first["touch_target"] is True
    assert first["stop_bar"] == 0
    # 窗口是成交日 row1..row3 (偏移 0..2): high = 10.0 / 12.0 / 14.0, 首次 >= 11.5 在偏移 1
    assert first["target_bar"] == 1
    assert first["stop_before_target"] is True

    # 反过来: 止盈在第 0 根, 止损在第 1 根 -> 先止盈
    high[:, 0] = [10.0, 12.0, 13.0, 14.0, 15.0, 16.0]
    low[:, 0] = [10.0, 10.0, 8.0, 6.0, 5.0, 4.0]
    second = build_signal_outcomes(
        _market(high=high, low=low),
        _entry((0, 0)),
        config=OutcomeConfig(horizons=(1, 2, 3), stop_loss=-0.05, take_profit=0.15),
    ).row(0, named=True)
    assert second["stop_bar"] == 1
    assert second["target_bar"] == 0
    assert second["stop_before_target"] is False


def test_stop_before_target_is_null_when_not_decided() -> None:
    """两者没有同时触及时必须保持 null, 不能退化成 False 混进统计。"""
    frame = build_signal_outcomes(
        _market(),
        _entry((0, 0)),
        config=OutcomeConfig(horizons=(1, 2, 3), stop_loss=-0.05),
    )
    row = _one(frame)
    assert row["touch_target"] is False
    assert row["stop_before_target"] is None


def test_exit_signal_offset_and_returns() -> None:
    """出场信号记录出现偏移与当日收盘收益, 未出现时为 -1/null。"""
    exits = _entry((3, 0), (1, 1))
    frame = build_signal_outcomes(
        _market(), _entry((0, 0), (0, 1)), exit_signals=exits, config=_horizons(1, 2, 3)
    )
    by_symbol = {row["symbol"]: row for row in frame.iter_rows(named=True)}
    # 成交在 row1, 出场信号在 row3 -> 偏移 2
    assert by_symbol["AAA.SZ"]["exit_signal_offset"] == 2
    assert by_symbol["AAA.SZ"]["ret_at_exit_close"] == pytest.approx(0.01)
    # 出场信号在 row1 = 成交当日 -> 偏移 0
    assert by_symbol["BBB.SZ"]["exit_signal_offset"] == 0


def test_entry_signal_code_passthrough() -> None:
    codes = np.zeros((6, 2), dtype=np.int16)
    codes[0, 1] = 2
    frame = build_signal_outcomes(
        _market(), _entry((0, 0), (0, 1)), entry_signal_code=codes, config=_horizons(1)
    )
    values = dict(zip(frame["symbol"], frame["entry_signal_code"], strict=True))
    assert values["AAA.SZ"] == 0
    assert values["BBB.SZ"] == 2


def test_empty_entry_keeps_schema() -> None:
    config = _horizons(1, 5)
    frame = build_signal_outcomes(_market(), _entry(), config=config)
    assert frame.height == 0
    assert ret_column(1) in frame.columns
    assert ret_column(5) in frame.columns
    assert "stop_before_target" in frame.columns


@pytest.mark.parametrize(
    ("horizons", "message"),
    [
        ((0, 5), "正整数"),
        ((-1, 5), "正整数"),
        ((), "不能为空"),
    ],
)
def test_invalid_horizons_rejected(horizons: tuple[int, ...], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        OutcomeConfig(horizons=horizons)


def test_invalid_stop_loss_rejected() -> None:
    with pytest.raises(ValueError, match="stop_loss"):
        OutcomeConfig(horizons=(1,), stop_loss=0.05)
    with pytest.raises(ValueError, match="take_profit"):
        OutcomeConfig(horizons=(1,), take_profit=-0.05)


def test_shape_mismatch_rejected() -> None:
    with pytest.raises(ValueError, match="形状"):
        build_signal_outcomes(_market(), np.zeros((3, 9), dtype=bool))


def test_market_baseline_matches_signal_convention() -> None:
    """基准与信号同口径: 同一天开盘买入, 持有 h 根到收盘的全市场收益中位数。"""
    baseline = compute_market_baseline(_market(), horizons=(1, 2))
    assert np.isnan(baseline[1][5])
    # 与信号口径一致: 第 j 天开盘买入, ret_hd = close[j + h - 1] / open[j] - 1
    # h=1 -> close[0]/open[0] = 10.5/10 ; h=2 -> close[1]/open[0] = 10.2/10
    assert baseline[1][0] == pytest.approx(0.05)
    assert baseline[2][0] == pytest.approx(0.02)


def test_excess_return_uses_fill_day_baseline() -> None:
    baseline = compute_market_baseline(_market(), horizons=(1,))
    frame = build_signal_outcomes(
        _market(), _entry((0, 0)), baseline=baseline, config=_horizons(1)
    )
    row = _one(frame)
    assert row[excess_column(1)] == pytest.approx(row[ret_column(1)] - baseline[1][1])


def test_summary_excludes_null_from_denominator() -> None:
    """收益为 null 的样本既不进胜率分母也不当 0 处理。"""
    frame = build_signal_outcomes(
        _market(), _entry((0, 0), (4, 0), (4, 1)), config=_horizons(1, 3)
    )
    summary = summarize_outcomes(frame, horizons=(1, 3), with_path=False).row(0, named=True)
    assert summary["n_signals"] == 3
    # horizon=3 只有 row0 那条有观测(其余因数据尾部被截断) -> null 不进分母
    assert summary["ret3_n"] == 1
    # horizon=1: 三条都有观测, 分别为 +2% / -5% / -5%
    assert summary["ret1_n"] == 3
    assert summary["ret1_wins"] == 1
    assert summary["ret1_win_rate"] == pytest.approx(1 / 3)


def test_summary_no_loss_returns_null_profit_factor() -> None:
    """无亏损样本时 profit_factor 必须是 null, 不能返回 inf 污染排序。"""
    frame = pl.DataFrame({ret_column(1): [0.01, 0.02, 0.03]})
    summary = summarize_outcomes(frame, horizons=(1,), with_path=False).row(0, named=True)
    assert summary["ret1_profit_factor"] is None
    assert summary["ret1_win_rate"] == pytest.approx(1.0)


def test_feature_buckets_are_ordered_and_null_safe() -> None:
    frame = pl.DataFrame({
        "premium": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0],
        "has_center": [True, False, True, False, True, False, True, False],
    })
    bucketed, specs = add_feature_buckets(frame, ["premium", "has_center"], buckets=2)
    assert set(specs) == {"premium"}
    assert set(bucketed["premium_bucket"].drop_nulls().to_list()) == {"Rank 1", "Rank 2"}
    assert bucketed["premium_bucket"][0] == "Rank 1"
    assert bucketed["premium_bucket"][7] == "Rank 2"
    assert set(bucketed["has_center_bucket"].to_list()) == {"true", "false"}


def test_feature_buckets_skip_degenerate_column() -> None:
    frame = pl.DataFrame({"flat": [1.0, 1.0, 1.0]})
    bucketed, specs = add_feature_buckets(frame, ["flat"], buckets=2)
    assert specs == {}
    assert "flat_bucket" not in bucketed.columns
