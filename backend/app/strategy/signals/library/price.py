"""价格 / 动量 / 波动类信号函数。"""
from __future__ import annotations

import numpy as np

from app.backtest.matrix import MarketDataMatrix, matrix_feature, valid_shift
from app.strategy.signals.registry import signal, DIRECTION_ENTRY


@signal(
    name="rsi_below",
    category="动量",
    direction=DIRECTION_ENTRY,
    description="RSI14 低于阈值（超卖）",
    label="RSI超卖",
    required_fields=("close",),
    warmup=14,
    enable_param="use_rsi_filter",
    params=(
        {"id": "rsi_max", "label": "RSI上限", "type": "float", "default": 30.0,
         "min": 10.0, "max": 50.0, "step": 1.0},
    ),
)
def rsi_below(market: MarketDataMatrix, **params) -> np.ndarray:
    threshold = float(params.get("rsi_max", 30.0))
    return matrix_feature(market, "rsi_14") < threshold


@signal(
    name="bullish_candle",
    category="K线",
    direction=DIRECTION_ENTRY,
    description="当日收阳（收盘 > 开盘）",
    label="收阳",
    required_fields=("open", "close"),
    warmup=1,
    enable_param="require_bullish_candle",
)
def bullish_candle(market: MarketDataMatrix, **params) -> np.ndarray:
    del params
    return market.close > market.open


@signal(
    name="change_pct_ge",
    category="动量",
    direction=DIRECTION_ENTRY,
    description="当日涨幅 > 阈值（%）",
    label="涨幅达标",
    required_fields=("close",),
    warmup=1,
    enable_param="use_change_filter",
    params=(
        {"id": "min_change", "label": "最低涨幅%", "type": "float", "default": 3.0,
         "min": 0.5, "max": 15.0, "step": 0.5},
    ),
)
def change_pct_ge(market: MarketDataMatrix, **params) -> np.ndarray:
    threshold = float(params.get("min_change", 3.0)) / 100.0
    return matrix_feature(market, "change_pct") > threshold


@signal(
    name="change_pct_positive",
    category="动量",
    direction=DIRECTION_ENTRY,
    description="当日涨幅为正",
    label="红盘",
    required_fields=("close",),
    warmup=1,
    enable_param="require_positive_change",
)
def change_pct_positive(market: MarketDataMatrix, **params) -> np.ndarray:
    del params
    return matrix_feature(market, "change_pct") > 0


@signal(
    name="momentum_positive",
    category="动量",
    direction=DIRECTION_ENTRY,
    description="20 日动量为正",
    label="动量正",
    required_fields=("close",),
    warmup=20,
    enable_param="require_positive_momentum",
)
def momentum_positive(market: MarketDataMatrix, **params) -> np.ndarray:
    del params
    return matrix_feature(market, "momentum_20d") > 0


@signal(
    name="annual_vol_below",
    category="波动",
    direction=DIRECTION_ENTRY,
    description="年化波动率低于阈值",
    label="低波动",
    required_fields=("close",),
    warmup=20,
    enable_param="use_volatility_filter",
    params=(
        {"id": "vol_max", "label": "最大年化波动", "type": "float", "default": 0.30,
         "min": 0.05, "max": 1.0, "step": 0.01},
    ),
)
def annual_vol_below(market: MarketDataMatrix, **params) -> np.ndarray:
    threshold = float(params.get("vol_max", 0.30))
    return matrix_feature(market, "annual_vol_20d") < threshold


@signal(
    name="open_gap_up",
    category="K线",
    direction=DIRECTION_ENTRY,
    description="高开幅度 > 阈值（%）",
    label="高开",
    required_fields=("open", "close"),
    warmup=1,
    enable_param="use_open_gap_filter",
    params=(
        {"id": "min_open_gap", "label": "最低高开%", "type": "float", "default": 3.0,
         "min": 1.0, "max": 10.0, "step": 0.5},
    ),
)
def open_gap_up(market: MarketDataMatrix, **params) -> np.ndarray:
    gap = float(params.get("min_open_gap", 3.0)) / 100.0
    prev_close = valid_shift(market.close, 1)
    return market.open > prev_close * (1.0 + gap)


@signal(
    name="close_at_60d_low",
    category="价格",
    direction=DIRECTION_ENTRY,
    description="触及 60 日新低",
    label="60日新低",
    required_fields=("close",),
    warmup=60,
    enable_param="require_n_day_low",
)
def close_at_60d_low(market: MarketDataMatrix, **params) -> np.ndarray:
    del params
    return market.close <= matrix_feature(market, "low_60d")


@signal(
    name="close_at_60d_high",
    category="价格",
    direction=DIRECTION_ENTRY,
    description="触及 60 日新高",
    label="60日新高",
    required_fields=("close",),
    warmup=60,
    enable_param="require_n_day_high",
)
def close_at_60d_high(market: MarketDataMatrix, **params) -> np.ndarray:
    del params
    return market.close >= matrix_feature(market, "high_60d")
