"""量价类信号函数。"""
from __future__ import annotations

import numpy as np

from app.backtest.matrix import MarketDataMatrix, matrix_feature
from app.strategy.signals.registry import signal, DIRECTION_ENTRY


@signal(
    name="vol_ratio_ge",
    category="量价",
    direction=DIRECTION_ENTRY,
    description="5 日量比 ≥ 阈值",
    required_fields=("volume",),
    warmup=5,
    enable_param="use_volume_filter",
    params=(
        {"id": "vol_ratio_min", "label": "最低量比", "type": "float", "default": 1.5,
         "min": 0.5, "max": 5.0, "step": 0.1},
    ),
)
def vol_ratio_ge(market: MarketDataMatrix, **params) -> np.ndarray:
    threshold = float(params.get("vol_ratio_min", 1.5))
    return matrix_feature(market, "vol_ratio_5d") >= threshold
