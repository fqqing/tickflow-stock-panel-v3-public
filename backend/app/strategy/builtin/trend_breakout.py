"""趋势突破 — MA60上方 + 60日新高 + 放量（信号组合声明式）"""
from __future__ import annotations

META = {
    "id": "trend_breakout",
    "name": "趋势突破",
    "description": "MA60上方 + 60日新高 + 量能 ≥ 2倍均量",
    "tags": ["趋势", "突破", "放量"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "basic_filter": {
        "price_min": 5,
        "price_max": 200,
        "market_cap_min": 20e8,
        "amount_min": 1e8,
        "exclude_st": True,
        "exclude_new_days": 60,
    },
    "params": [
        {
            "id": "require_above_ma60",
            "label": "要求收盘价在MA60上方",
            "type": "bool",
            "default": True,
        },
        {"id": "require_n_day_high", "label": "要求60日新高", "type": "bool", "default": True},
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
    ],
    "scoring": {"momentum_60d": 0.4, "vol_ratio_5d": 0.3, "change_pct": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "signal_combo"
ENTRY_SIGNAL_EXPR = "all_of(close_above_ma60, close_at_60d_high, vol_ratio_ge)"
EXIT_SIGNAL_EXPR = "ma20_breakdown"
ENTRY_SIGNALS = ["signal_n_day_high"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 20
LOOKBACK_DAYS = 60
