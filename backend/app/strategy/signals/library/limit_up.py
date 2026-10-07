"""涨停 / 连板类信号函数。"""
from __future__ import annotations

import numpy as np

from app.backtest.matrix import MarketDataMatrix, matrix_feature
from app.strategy.signals.registry import signal, DIRECTION_ENTRY


@signal(
    name="limit_up_locked",
    category="涨停",
    direction=DIRECTION_ENTRY,
    description="当日涨停封板",
    required_fields=(),
    warmup=1,
    enable_param="require_limit_up",
)
def limit_up_locked(market: MarketDataMatrix, **params) -> np.ndarray:
    del params
    return market.limit_up_locked.astype(bool)


@signal(
    name="consecutive_limit_ups_ge",
    category="涨停",
    direction=DIRECTION_ENTRY,
    description="连板数 ≥ 阈值",
    required_fields=("consecutive_limit_ups",),
    warmup=1,
    enable_param="use_boards_filter",
    params=(
        {"id": "min_boards", "label": "最少连板数", "type": "int", "default": 2,
         "min": 1, "max": 20, "step": 1},
    ),
)
def consecutive_limit_ups_ge(market: MarketDataMatrix, **params) -> np.ndarray:
    threshold = int(params.get("min_boards", 2))
    return matrix_feature(market, "consecutive_limit_ups") >= threshold


@signal(
    name="near_limit_up_gap",
    category="涨停",
    direction=DIRECTION_ENTRY,
    description="涨幅距涨停板空间 ≤ 阈值（%）",
    required_fields=("price_limit_pct",),
    warmup=1,
    enable_param="use_limit_gap_filter",
    params=(
        {"id": "limit_gap", "label": "距涨停空间%", "type": "float", "default": 3.0,
         "min": 1.0, "max": 10.0, "step": 0.5},
    ),
)
def near_limit_up_gap(market: MarketDataMatrix, **params) -> np.ndarray:
    gap = float(params.get("limit_gap", 3.0)) / 100.0
    change = matrix_feature(market, "change_pct")
    limit_pct = matrix_feature(market, "price_limit_pct")
    return change >= limit_pct - gap
