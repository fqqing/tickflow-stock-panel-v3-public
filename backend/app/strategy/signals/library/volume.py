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
    label="放量",
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


@signal(
    name="vol_ratio_le",
    category="量价",
    direction=DIRECTION_ENTRY,
    description="5 日量比 ≤ 阈值（缩量）",
    label="缩量",
    required_fields=("volume",),
    warmup=5,
    enable_param="use_volume_filter",
    params=(
        {"id": "vol_ratio_max", "label": "最大量比", "type": "float", "default": 0.8,
         "min": 0.2, "max": 1.5, "step": 0.1},
    ),
)
def vol_ratio_le(market: MarketDataMatrix, **params) -> np.ndarray:
    threshold = float(params.get("vol_ratio_max", 0.8))
    return matrix_feature(market, "vol_ratio_5d") < threshold


@signal(
    name="turnover_ge",
    category="量价",
    direction=DIRECTION_ENTRY,
    description="换手率 > 阈值（%）",
    label="高换手",
    required_fields=("turnover_rate",),
    warmup=1,
    enable_param="use_turnover_filter",
    params=(
        {"id": "min_turnover", "label": "最低换手率%", "type": "float", "default": 5.0,
         "min": 1.0, "max": 20.0, "step": 0.5},
    ),
)
def turnover_ge(market: MarketDataMatrix, **params) -> np.ndarray:
    threshold = float(params.get("min_turnover", 5.0))
    return matrix_feature(market, "turnover_rate") > threshold
