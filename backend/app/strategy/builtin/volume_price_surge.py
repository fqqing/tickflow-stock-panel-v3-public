"""量价齐升 — 突破MA20 + 放量 + 收阳（信号组合声明式）"""
from __future__ import annotations

META = {
    "id": "volume_price_surge",
    "name": "量价齐升",
    "description": "突破MA20 + 放量 + 收阳",
    "tags": ["量价", "突破"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {"id": "require_ma20_breakout", "label": "要求突破MA20", "type": "bool", "default": True},
        {"id": "use_volume_filter", "label": "启用量比过滤", "type": "bool", "default": True},
        {
            "id": "vol_ratio_min",
            "label": "最低量比",
            "type": "float",
            "default": 2.0,
            "min": 0.5,
            "max": 10.0,
            "step": 0.1,
        },
        {"id": "require_bullish_candle", "label": "要求收阳", "type": "bool", "default": True},
    ],
    "scoring": {"vol_ratio_5d": 0.4, "change_pct": 0.3, "momentum_20d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "signal_combo"
ENTRY_SIGNAL_EXPR = "all_of(ma20_breakout, vol_ratio_ge, bullish_candle)"
EXIT_SIGNAL_EXPR = "ma20_breakdown"
ENTRY_SIGNALS = ["signal_ma20_breakout"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 15
LOOKBACK_DAYS = 60
