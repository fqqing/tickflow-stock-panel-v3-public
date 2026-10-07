"""断板反包 — 涨停 + 放量 + 涨幅 >3%（信号组合声明式）"""
from __future__ import annotations

META = {
    "id": "broken_board_recovery",
    "name": "断板反包",
    "description": "连板≥2后断板1-2天, 出现放量反包信号",
    "tags": ["涨停", "反包"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "require_limit_up", "label": "要求当日涨停", "type": "bool", "default": True},
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
        {"id": "use_change_filter", "label": "启用涨幅过滤", "type": "bool", "default": True},
        {
            "id": "min_change",
            "label": "最低涨幅%",
            "type": "float",
            "default": 3.0,
            "min": 1.0,
            "max": 10.0,
            "step": 1.0,
        },
    ],
    "scoring": {"change_pct": 0.4, "vol_ratio_5d": 0.3, "momentum_5d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "signal_combo"
ENTRY_SIGNAL_EXPR = "all_of(limit_up_locked, vol_ratio_ge, change_pct_ge)"
EXIT_SIGNAL_EXPR = "ma20_breakdown"
ENTRY_SIGNALS = ["signal_limit_up"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 10
LOOKBACK_DAYS = 60
