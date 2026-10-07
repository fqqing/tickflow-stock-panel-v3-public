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
    required_fields=("close",),
    warmup=60,
    enable_param="require_above_ma60",
)
def close_above_ma60(market: MarketDataMatrix, **params) -> np.ndarray:
    del params
    ma60 = matrix_feature(market, "ma60")
    return market.close > ma60
