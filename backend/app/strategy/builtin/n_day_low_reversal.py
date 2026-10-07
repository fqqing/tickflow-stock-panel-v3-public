"""新低反转 — 60日新低后收阳放量（信号组合声明式）"""
from __future__ import annotations

META = {
    "id": "n_day_low_reversal",
    "name": "新低反转",
    "description": "触及60日新低后当日收阳放量, 反转信号",
    "tags": ["反转", "新低"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {"id": "require_n_day_low", "label": "要求60日新低", "type": "bool", "default": True},
        {"id": "require_bullish_candle", "label": "要求收阳", "type": "bool", "default": True},
        {"id": "use_volume_filter", "label": "启用量比过滤", "type": "bool", "default": True},
        {
            "id": "vol_ratio_min",
            "label": "最低量比",
            "type": "float",
            "default": 1.5,
            "min": 0.5,
            "max": 5.0,
            "step": 0.1,
        },
    ],
    "scoring": {"change_pct": 0.4, "vol_ratio_5d": 0.3, "momentum_5d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "signal_combo"
ENTRY_SIGNAL_EXPR = "all_of(close_at_60d_low, bullish_candle, vol_ratio_ge)"
EXIT_SIGNAL_EXPR = "ma20_breakdown"
ENTRY_SIGNALS = ["signal_n_day_low"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 15
LOOKBACK_DAYS = 60
