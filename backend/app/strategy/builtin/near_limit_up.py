"""逼近涨停 — 涨幅 > 7% 且距涨停 < 3%（信号组合声明式）"""
from __future__ import annotations

META = {
    "id": "near_limit_up",
    "name": "逼近涨停",
    "description": "涨幅 > 7% 且距涨停 < 3%, 追涨信号",
    "tags": ["涨停", "追涨"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "use_change_filter", "label": "启用涨幅过滤", "type": "bool", "default": True},
        {
            "id": "min_change",
            "label": "最低涨幅%",
            "type": "float",
            "default": 7.0,
            "min": 3.0,
            "max": 15.0,
            "step": 1.0,
        },
        {
            "id": "use_limit_gap_filter",
            "label": "启用距涨停空间过滤",
            "type": "bool",
            "default": True,
        },
        {
            "id": "limit_gap",
            "label": "距涨停空间%",
            "type": "float",
            "default": 3.0,
            "min": 1.0,
            "max": 10.0,
            "step": 0.5,
        },
    ],
    "scoring": {"change_pct": 0.5, "amount": 0.3, "momentum_5d": 0.2},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

EXECUTION_BACKEND = "signal_combo"
ENTRY_SIGNAL_EXPR = "all_of(change_pct_ge, near_limit_up_gap)"
EXIT_SIGNAL_EXPR = "ma20_breakdown"
ENTRY_SIGNALS = []
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 5
LOOKBACK_DAYS = 60
