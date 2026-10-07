"""均线类信号函数。"""
from __future__ import annotations

import numpy as np

from app.backtest.matrix import MarketDataMatrix, matrix_feature, valid_shift
from app.strategy.signals.registry import signal, DIRECTION_ENTRY, DIRECTION_EXIT


@signal(
    name="ma_golden_cross",
    category="均线",
    direction=DIRECTION_ENTRY,
    description="MA5 上穿 MA20",
    label="MA金叉",
    required_fields=("close",),
    warmup=20,
    enable_param="require_ma_golden",
)
def ma_golden_cross(market: MarketDataMatrix, **params) -> np.ndarray:
    del params
    ma5 = matrix_feature(market, "ma5")
    ma20 = matrix_feature(market, "ma20")
    prev_ma5 = valid_shift(ma5, 1)
    prev_ma20 = valid_shift(ma20, 1)
    return (ma5 > ma20) & (prev_ma5 <= prev_ma20)


@signal(
    name="ma_dead_cross",
    category="均线",
    direction=DIRECTION_EXIT,
    description="MA5 下穿 MA20",
    label="MA死叉",
    required_fields=("close",),
    warmup=20,
)
def ma_dead_cross(market: MarketDataMatrix, **params) -> np.ndarray:
    del params
    ma5 = matrix_feature(market, "ma5")
    ma20 = matrix_feature(market, "ma20")
    prev_ma5 = valid_shift(ma5, 1)
    prev_ma20 = valid_shift(ma20, 1)
    return (ma5 < ma20) & (prev_ma5 >= prev_ma20)


@signal(
    name="close_above_ma60",
    category="均线",
    direction=DIRECTION_ENTRY,
    description="收盘价站上 MA60",
    label="站上MA60",
    required_fields=("close",),
    warmup=60,
    enable_param="require_above_ma60",
)
def close_above_ma60(market: MarketDataMatrix, **params) -> np.ndarray:
    del params
    ma60 = matrix_feature(market, "ma60")
    return market.close > ma60


@signal(
    name="close_above_ma5",
    category="均线",
    direction=DIRECTION_ENTRY,
    description="收盘价站上 MA5",
    label="站上MA5",
    required_fields=("close",),
    warmup=5,
    enable_param="require_above_ma5",
)
def close_above_ma5(market: MarketDataMatrix, **params) -> np.ndarray:
    del params
    ma5 = matrix_feature(market, "ma5")
    return market.close > ma5


@signal(
    name="close_above_ma20",
    category="均线",
    direction=DIRECTION_ENTRY,
    description="收盘价站上 MA20",
    label="站上MA20",
    required_fields=("close",),
    warmup=20,
    enable_param="require_above_ma20",
)
def close_above_ma20(market: MarketDataMatrix, **params) -> np.ndarray:
    del params
    ma20 = matrix_feature(market, "ma20")
    return market.close > ma20


@signal(
    name="ma20_breakout",
    category="均线",
    direction=DIRECTION_ENTRY,
    description="收盘价上穿 MA20",
    label="突破MA20",
    required_fields=("close",),
    warmup=20,
    enable_param="require_ma20_breakout",
)
def ma20_breakout(market: MarketDataMatrix, **params) -> np.ndarray:
    del params
    ma20 = matrix_feature(market, "ma20")
    prev_close = valid_shift(market.close, 1)
    prev_ma20 = valid_shift(ma20, 1)
    return (market.close > ma20) & (prev_close <= prev_ma20)


@signal(
    name="ma20_breakdown",
    category="均线",
    direction=DIRECTION_EXIT,
    description="收盘价下穿 MA20",
    label="跌破MA20",
    required_fields=("close",),
    warmup=20,
)
def ma20_breakdown(market: MarketDataMatrix, **params) -> np.ndarray:
    del params
    ma20 = matrix_feature(market, "ma20")
    prev_close = valid_shift(market.close, 1)
    prev_ma20 = valid_shift(ma20, 1)
    return (market.close < ma20) & (prev_close >= prev_ma20)


@signal(
    name="ma_bullish_alignment",
    category="均线",
    direction=DIRECTION_ENTRY,
    description="MA5 > MA10 > MA20 > MA60 多头排列",
    label="多头排列",
    required_fields=("close",),
    warmup=60,
    enable_param="require_ma_alignment",
)
def ma_bullish_alignment(market: MarketDataMatrix, **params) -> np.ndarray:
    del params
    ma5 = matrix_feature(market, "ma5")
    ma10 = matrix_feature(market, "ma10")
    ma20 = matrix_feature(market, "ma20")
    ma60 = matrix_feature(market, "ma60")
    return (ma5 > ma10) & (ma10 > ma20) & (ma20 > ma60)


@signal(
    name="ma5_20_60_alignment",
    category="均线",
    direction=DIRECTION_ENTRY,
    description="MA5 > MA20 > MA60 多头排列",
    label="MA5>20>60",
    required_fields=("close",),
    warmup=60,
    enable_param="require_ma_alignment",
)
def ma5_20_60_alignment(market: MarketDataMatrix, **params) -> np.ndarray:
    del params
    ma5 = matrix_feature(market, "ma5")
    ma20 = matrix_feature(market, "ma20")
    ma60 = matrix_feature(market, "ma60")
    return (ma5 > ma20) & (ma20 > ma60)


@signal(
    name="close_near_ma20",
    category="均线",
    direction=DIRECTION_ENTRY,
    description="收盘价在 MA20 附近（偏离度 ±N%）",
    label="回踩MA20",
    required_fields=("close",),
    warmup=20,
    enable_param="use_ma20_proximity",
    params=(
        {"id": "ma_proximity", "label": "MA偏离度%", "type": "float", "default": 2.0,
         "min": 0.5, "max": 5.0, "step": 0.5},
    ),
)
def close_near_ma20(market: MarketDataMatrix, **params) -> np.ndarray:
    proximity = float(params.get("ma_proximity", 2.0)) / 100.0
    ma20 = matrix_feature(market, "ma20")
    return (market.close > ma20 * (1.0 - proximity)) & (
        market.close < ma20 * (1.0 + proximity)
    )
