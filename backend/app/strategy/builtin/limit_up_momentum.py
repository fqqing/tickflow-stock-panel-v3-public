"""连板接力 — 今日涨幅 > 5% 且连板（信号组合声明式）"""
from __future__ import annotations

META = {
    "id": "limit_up_momentum",
    "name": "连板接力",
    "description": "连板股 + 今日涨幅 > 5%, 连板接力追踪",
    "tags": ["涨停", "连板", "接力"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "use_change_filter", "label": "启用涨幅过滤", "type": "bool", "default": True},
        {
            "id": "min_change",
            "label": "最低涨幅%",
            "type": "float",
            "default": 5.0,
            "min": 2.0,
            "max": 15.0,
            "step": 0.5,
        },
        {"id": "use_boards_filter", "label": "启用连板数过滤", "type": "bool", "default": True},
        {
            "id": "min_boards",
            "label": "最少连板",
            "type": "int",
            "default": 1,
            "min": 1,
            "max": 10,
            "step": 1,
        },
    ],
    "scoring": {"consecutive_limit_ups": 0.4, "change_pct": 0.3, "amount": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

EXECUTION_BACKEND = "signal_combo"
ENTRY_SIGNAL_EXPR = "all_of(change_pct_ge, consecutive_limit_ups_ge)"
EXIT_SIGNAL_EXPR = ""
ENTRY_SIGNALS = ["signal_limit_up"]
EXIT_SIGNALS = []
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 5
LOOKBACK_DAYS = 60
