"""MACD 类信号函数。"""
from __future__ import annotations

import numpy as np

from app.backtest.matrix import MarketDataMatrix, matrix_feature, valid_shift
from app.strategy.signals.registry import signal, DIRECTION_ENTRY, DIRECTION_EXIT


@signal(
    name="macd_golden",
    category="MACD",
    direction=DIRECTION_ENTRY,
    description="MACD DIF 上穿 DEA",
    label="MACD金叉",
    required_fields=("close",),
    warmup=60,
    enable_param="require_macd_golden",
)
def macd_golden(market: MarketDataMatrix, **params) -> np.ndarray:
    del params
    dif = matrix_feature(market, "macd_dif")
    dea = matrix_feature(market, "macd_dea")
    prev_dif = valid_shift(dif, 1)
    prev_dea = valid_shift(dea, 1)
    return (dif > dea) & (prev_dif <= prev_dea)


@signal(
    name="macd_dead",
    category="MACD",
    direction=DIRECTION_EXIT,
    description="MACD DIF 下穿 DEA",
    label="MACD死叉",
    required_fields=("close",),
    warmup=60,
)
def macd_dead(market: MarketDataMatrix, **params) -> np.ndarray:
    del params
    dif = matrix_feature(market, "macd_dif")
    dea = matrix_feature(market, "macd_dea")
    prev_dif = valid_shift(dif, 1)
    prev_dea = valid_shift(dea, 1)
    return (dif < dea) & (prev_dif >= prev_dea)
