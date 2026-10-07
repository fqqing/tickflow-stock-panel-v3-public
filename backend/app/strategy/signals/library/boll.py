"""布林带类信号函数。"""
from __future__ import annotations

import numpy as np

from app.backtest.matrix import MarketDataMatrix, matrix_feature
from app.strategy.signals.registry import signal, DIRECTION_ENTRY, DIRECTION_EXIT


@signal(
    name="boll_breakout_upper",
    category="布林",
    direction=DIRECTION_ENTRY,
    description="收盘价突破布林上轨",
    required_fields=("close",),
    warmup=20,
    enable_param="require_boll_breakout",
)
def boll_breakout_upper(market: MarketDataMatrix, **params) -> np.ndarray:
    del params
    upper = matrix_feature(market, "boll_upper")
    return market.close > upper


@signal(
    name="boll_breakdown_lower",
    category="布林",
    direction=DIRECTION_EXIT,
    description="收盘价跌破布林下轨",
    required_fields=("close",),
    warmup=20,
)
def boll_breakdown_lower(market: MarketDataMatrix, **params) -> np.ndarray:
    del params
    lower = matrix_feature(market, "boll_lower")
    return market.close < lower
