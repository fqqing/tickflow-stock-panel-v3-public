"""均线多头 — MA5>MA10>MA20>MA60 + 短期动量为正（信号组合声明式）"""
from __future__ import annotations

META = {
    "id": "bullish_alignment",
    "name": "均线多头",
    "description": "MA5>MA10>MA20>MA60多头排列 + 短期动量为正",
    "tags": ["均线", "多头"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "require_ma_alignment",
            "label": "要求均线多头排列",
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
ENTRY_SIGNAL_EXPR = "all_of(ma_bullish_alignment, momentum_positive)"
EXIT_SIGNAL_EXPR = "any_of(ma_dead_cross, ma20_breakdown)"
ENTRY_SIGNALS = ["signal_ma_golden_5_20", "signal_ma_golden_20_60"]
EXIT_SIGNALS = ["signal_ma_dead_5_20", "signal_ma20_breakdown"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 20
LOOKBACK_DAYS = 60
