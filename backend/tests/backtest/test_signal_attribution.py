"""信号函数级归因 — 位掩码生成与反解测试。

验证 SignalComboStrategy 把 entry/exit 信号码升级为「每个信号函数一位」的位掩码，
以及 SignalLab 的 ``signal_name_column`` 能把位掩码反解成命中信号中文名组合，
从而把归因粒度从「策略级」细化到「信号函数级」。
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl

from app.backtest.matrix import build_market_data_matrix
from app.signallab.lab import (
    beautify_attribution_rows,
    default_attribution_features,
    signal_flag_column_names,
    signal_flag_columns,
    signal_name_column,
)
from app.strategy.signals.backend import SignalComboStrategy


def _build_panel() -> pl.DataFrame:
    rows = []
    start = date(2024, 1, 1)
    for offset in range(140):
        for asset_id, symbol in enumerate(("000001.SZ", "600000.SH", "300001.SZ")):
            base = 10.0 + asset_id * 6.0
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


def _market():
    return build_market_data_matrix(_build_panel())


def test_all_of_entry_code_is_full_mask():
    market = _market()
    combo = SignalComboStrategy(
        "all_of(ma_golden_cross, vol_ratio_ge, close_above_ma60)", "ma_dead_cross"
    )
    sm = combo.compute_signals(market, {
        "require_ma_golden": True, "use_volume_filter": True,
        "require_above_ma60": True, "vol_ratio_min": 1.2,
    })
    assert sm.entry_signal_ids == ("ma_golden_cross", "vol_ratio_ge", "close_above_ma60")
    assert sm.entry_signal_code.dtype == np.int64
    rows, cols = np.nonzero(sm.entry)
    assert rows.size > 0
    for r, c in zip(rows, cols):
        assert int(sm.entry_signal_code[r, c]) == 0b111


def test_any_of_exit_code_marks_hit_bits():
    market = _market()
    combo = SignalComboStrategy(
        "all_of(ma_bullish_alignment, momentum_positive)",
        "any_of(ma_dead_cross, ma20_breakdown)",
    )
    sm = combo.compute_signals(market, {
        "require_ma_alignment": True, "require_positive_momentum": True,
    })
    assert sm.exit_signal_ids == ("ma_dead_cross", "ma20_breakdown")
    rows, cols = np.nonzero(sm.exit)
    for r, c in zip(rows, cols):
        assert int(sm.exit_signal_code[r, c]) in (0b01, 0b10, 0b11)


def test_disabled_signal_bit_stays_zero():
    # 关掉 vol_ratio_ge（use_volume_filter=False），位序仍保留，但 bit1 恒 0。
    market = _market()
    combo = SignalComboStrategy(
        "all_of(ma_golden_cross, vol_ratio_ge, close_above_ma60)", "ma_dead_cross"
    )
    sm = combo.compute_signals(market, {
        "require_ma_golden": True, "use_volume_filter": False,
        "require_above_ma60": True, "vol_ratio_min": 1.2,
    })
    assert sm.entry_signal_ids == ("ma_golden_cross", "vol_ratio_ge", "close_above_ma60")
    rows, cols = np.nonzero(sm.entry)
    for r, c in zip(rows, cols):
        assert (int(sm.entry_signal_code[r, c]) >> 1) & 1 == 0


def test_signal_name_column_decodes_mask_with_label():
    codes = pl.Series("entry_signal_code", [7, 1, 2, 3, -1, 0, None], dtype=pl.Int64)
    ids = ("ma_golden_cross", "vol_ratio_ge", "close_above_ma60")
    out = signal_name_column(codes, ids)
    assert out.to_list() == [
        "MA金叉+放量+站上MA60", "MA金叉", "放量", "MA金叉+放量", None, None, None,
    ]


def test_signal_name_column_falls_back_to_signal_cn():
    # 保持现状策略的 signal_xxx 策略级信号列，查不到信号函数 → 退回 SIGNAL_CN 映射。
    codes = pl.Series("entry_signal_code", [1, 2, 4, 7, -1], dtype=pl.Int64)
    ids = ("signal_chan_1buy", "signal_chan_2buy", "signal_chan_3buy")
    out = signal_name_column(codes, ids)
    assert out.to_list() == [
        "缠论一买", "缠论二买", "缠论三买", "缠论一买+缠论二买+缠论三买", None,
    ]


# ===== 信号函数独立布尔列展开 =====


def test_signal_flag_column_names():
    assert signal_flag_column_names(("ma_golden_cross", "vol_ratio_ge")) == (
        "sig_ma_golden_cross", "sig_vol_ratio_ge",
    )


def test_signal_flag_columns_expands_mask_to_booleans():
    codes = pl.Series("entry_signal_code", [7, 1, 2, 3, 0, -1, None], dtype=pl.Int64)
    ids = ("ma_golden_cross", "vol_ratio_ge", "close_above_ma60")
    frame = signal_flag_columns(codes, ids)
    assert frame.columns == ["sig_ma_golden_cross", "sig_vol_ratio_ge", "sig_close_above_ma60"]
    assert frame.height == 7
    # 7 = 0b111 → 三列全命中；1 = 0b001 → 只有第一位命中；2 = 0b010 → 只有第二位。
    assert frame["sig_ma_golden_cross"].to_list() == [True, True, False, True, None, None, None]
    assert frame["sig_vol_ratio_ge"].to_list() == [True, False, True, True, None, None, None]
    assert frame["sig_close_above_ma60"].to_list() == [True, False, False, False, None, None, None]


def test_signal_flag_columns_empty_ids():
    codes = pl.Series("entry_signal_code", [7], dtype=pl.Int64)
    assert signal_flag_columns(codes, ()).is_empty()


def test_default_attribution_features_discovers_sig_columns():
    frame = pl.DataFrame({
        "entry_signal_name": ["MA金叉"],
        "sig_ma_golden_cross": [True],
        "sig_vol_ratio_ge": [False],
        "ctx_drawdown_from_high": [-0.1],
        "ret_5d": [0.02],
    })
    features = default_attribution_features(frame)
    assert features[:3] == (
        "entry_signal_name", "sig_ma_golden_cross", "sig_vol_ratio_ge",
    )
    # 形态特征跟在布尔列之后。
    assert "ctx_drawdown_from_high" in features
    # 收益列(未来信息)不得混进默认特征。
    assert "ret_5d" not in features


def test_default_attribution_features_without_sig_falls_back():
    frame = pl.DataFrame({"entry_signal_name": ["x"], "ctx_ma_bias": [0.0]})
    features = default_attribution_features(frame)
    # 无 sig_* 列时退回静态兜底: 组合名 + 全部形态特征。
    assert features[0] == "entry_signal_name"
    assert "sig_" not in " ".join(features)
    assert "ctx_ma_bias" in features
    assert "ctx_drawdown_from_high" in features


def test_beautify_attribution_rows_maps_sig_to_chinese():
    rows = [
        {"feature": "sig_ma_golden_cross", "bucket": "true", "ret_n": 40, "ret_mean": 0.05},
        {"feature": "sig_ma_golden_cross", "bucket": "false", "ret_n": 60, "ret_mean": -0.01},
        {"feature": "ctx_ma_bias", "bucket": "Rank 1", "ret_n": 30, "ret_mean": 0.02},
        {"feature": "entry_signal_name", "bucket": "MA金叉", "ret_n": 40, "ret_mean": 0.05},
    ]
    out = beautify_attribution_rows(rows)
    assert out[0]["feature"] == "信号·MA金叉"
    assert out[0]["bucket"] == "命中"
    assert out[1]["feature"] == "信号·MA金叉"
    assert out[1]["bucket"] == "未命中"
    # 非 sig_ 特征原样透传。
    assert out[2]["feature"] == "ctx_ma_bias"
    assert out[2]["bucket"] == "Rank 1"
    assert out[3]["feature"] == "entry_signal_name"
    # 数值不受影响。
    assert out[0]["ret_mean"] == 0.05

