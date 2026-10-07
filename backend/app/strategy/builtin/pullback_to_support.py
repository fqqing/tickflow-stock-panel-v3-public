"""缩量回踩 — 回踩MA20附近 + 缩量 + 中期趋势向上（信号组合声明式）"""
from __future__ import annotations

META = {
    "id": "pullback_to_support",
    "name": "缩量回踩",
    "description": "回踩MA20附近 + 缩量 + 中期趋势向上",
    "tags": ["回踩", "支撑"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {"id": "use_ma20_proximity", "label": "启用MA20附近过滤", "type": "bool", "default": True},
        {
            "id": "ma_proximity",
            "label": "均线偏离度%",
            "type": "float",
            "default": 2.0,
            "min": 1.0,
            "max": 5.0,
            "step": 0.5,
        },
        {"id": "use_volume_filter", "label": "启用缩量过滤", "type": "bool", "default": True},
        {
            "id": "vol_ratio_max",
            "label": "最大量比",
            "type": "float",
            "default": 0.8,
            "min": 0.2,
            "max": 1.5,
            "step": 0.1,
        },
        {
            "id": "require_above_ma60",
            "label": "要求收盘价在MA60上方",
            "type": "bool",
            "default": True,
        },
        {
            "id": "require_positive_momentum",
            "label": "要求20日动量为正",
            "type": "bool",
            "default": True,
        },
    ],
    "scoring": {"momentum_60d": 0.4, "momentum_20d": 0.3, "turnover_rate": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "signal_combo"
ENTRY_SIGNAL_EXPR = "all_of(close_near_ma20, vol_ratio_le, close_above_ma60, momentum_positive)"
EXIT_SIGNAL_EXPR = "ma20_breakdown"
ENTRY_SIGNALS = ["signal_ma_golden_5_20"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 20
LOOKBACK_DAYS = 60
