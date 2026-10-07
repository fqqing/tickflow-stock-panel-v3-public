"""连板股 — 涨停且连续涨停≥2天（信号组合声明式）"""
from __future__ import annotations

META = {
    "id": "consecutive_limit_ups",
    "name": "连板股",
    "description": "当日涨停且连续涨停≥2天, 强势追涨",
    "tags": ["涨停", "连板"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "require_limit_up", "label": "要求当日涨停", "type": "bool", "default": True},
        {"id": "use_boards_filter", "label": "启用连板数过滤", "type": "bool", "default": True},
        {
            "id": "min_boards",
            "label": "最少连板数",
            "type": "int",
            "default": 2,
            "min": 1,
            "max": 20,
            "step": 1,
        },
    ],
    "scoring": {"consecutive_limit_ups": 0.5, "change_pct": 0.3, "amount": 0.2},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "signal_combo"
ENTRY_SIGNAL_EXPR = "all_of(limit_up_locked, consecutive_limit_ups_ge)"
EXIT_SIGNAL_EXPR = ""
ENTRY_SIGNALS = ["signal_limit_up"]
EXIT_SIGNALS = []
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 5
LOOKBACK_DAYS = 60
