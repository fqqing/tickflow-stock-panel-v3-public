"""强势高开 — 高开 > 3% 且收阳、涨幅达标（信号组合声明式）"""
from __future__ import annotations

META = {
    "id": "strong_open",
    "name": "强势高开",
    "description": "高开 > 3% 且收盘高于开盘价, 集合竞价强势",
    "tags": ["高开", "强势"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "use_open_gap_filter", "label": "启用高开过滤", "type": "bool", "default": True},
        {
            "id": "min_open_gap",
            "label": "最低高开%",
            "type": "float",
            "default": 3.0,
            "min": 1.0,
            "max": 10.0,
            "step": 0.5,
        },
        {
            "id": "require_bullish_candle",
            "label": "要求收盘高于开盘",
            "type": "bool",
            "default": True,
        },
        {"id": "use_change_filter", "label": "启用涨幅过滤", "type": "bool", "default": True},
        {
            "id": "min_change",
            "label": "最低涨幅%",
            "type": "float",
            "default": 3.0,
            "min": 1.0,
            "max": 10.0,
            "step": 0.5,
        },
    ],
    "scoring": {"change_pct": 0.4, "amplitude": 0.2, "amount": 0.4},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

EXECUTION_BACKEND = "signal_combo"
ENTRY_SIGNAL_EXPR = "all_of(open_gap_up, bullish_candle, change_pct_ge)"
EXIT_SIGNAL_EXPR = "ma20_breakdown"
ENTRY_SIGNALS = []
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 10
LOOKBACK_DAYS = 60
